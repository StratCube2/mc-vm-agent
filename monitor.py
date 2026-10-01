"""
VM-wide monitoring: collects metrics for this VM and every Minecraft
server on it, and pushes reports to the PulseHost backend every 10s.

ONE collector for the WHOLE VM — deliberately not one monitoring process
per server. Runs as a background thread inside the agent itself, so it
starts whenever the agent starts (systemd brings the agent up at VM boot,
Restart=always covers crashes) and needs no extra supervision.

Push model per Features.md §8 ("Send reports to PulseHost. Repeat every
10 seconds"): POSTs to {MC_REPORT_URL}/internal/vm-metrics/{vm_ref} with
the same shared Bearer token the backend already uses to talk to us.
If MC_REPORT_URL isn't configured (e.g. older cloud-init), the thread
doesn't start and the backend falls back to pulling GET /metrics itself.

Player counts come from a real Minecraft status ping against
127.0.0.1:<server port> — accurate for vanilla/Paper/Fabric/Forge alike,
unlike parsing join/leave lines out of latest.log. TPS is reported as
null for now: there's no loader-independent way to read it without
driving the console (Paper's /tps is async fire-and-forget); the field
exists so it can light up later without an API change.
"""
import json
import os
import socket
import struct
import threading
import time

import httpx
import psutil

from config import SERVER_ROOT, AGENT_TOKEN, ServerPaths
from process_manager import manager, ServerState
import properties as props_module

# Where metric reports get pushed. Empty/unset disables the push loop.
REPORT_URL = os.environ.get("MC_REPORT_URL", "").rstrip("/")
# How this VM identifies itself to the backend. The internal id is ideal;
# hostname (= the Azure VM name under our cloud-init) also resolves.
VM_REF = os.environ.get("MC_VM_ID") or socket.gethostname()
REPORT_INTERVAL_SECONDS = 10

# Primed psutil handles so cpu_percent(interval=None) has a previous tick
# to diff against — the very first reading after Process() is otherwise
# meaningless 0.0.
_proc_cache: dict[str, psutil.Process] = {}


def _prime_process(server_id: str) -> psutil.Process | None:
    proc = _proc_cache.get(server_id)
    raw = manager.raw_process(server_id)
    if raw is None:
        _proc_cache.pop(server_id, None)
        return None
    if proc is None or proc.pid != raw.pid:
        proc = psutil.Process(raw.pid)
        proc.cpu_percent(interval=None)  # prime
        _proc_cache[server_id] = proc
    return proc


def _process_usage(server_id: str) -> tuple[float | None, float | None]:
    """(cpuPercent, ramUsedMb) for a server's whole process tree."""
    proc = _prime_process(server_id)
    if proc is None:
        return None, None
    try:
        procs = [proc, *proc.children(recursive=True)]
        cpu = sum(p.cpu_percent(interval=None) for p in procs)
        rss = sum(p.memory_info().rss for p in procs)
        return round(cpu, 1), round(rss / (1024 * 1024), 1)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None, None


def _server_port(server_id: str) -> int | None:
    """The port this server listens on: explicit assignment from meta.json,
    falling back to whatever server.properties says."""
    paths = ServerPaths(server_id)
    try:
        meta_port = paths.read_meta().get("serverPort")
        if meta_port:
            return int(meta_port)
    except (ValueError, TypeError, OSError):
        pass
    try:
        prop = props_module.read_properties(paths).get("server-port")
        if prop:
            return int(prop)
    except (ValueError, OSError):
        pass
    return None


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            out += bytes([b])
            return out


def _read_varint(sock: socket.socket) -> int | None:
    num = 0
    for i in range(5):
        data = sock.recv(1)
        if not data:
            return None
        num |= (data[0] & 0x7F) << (7 * i)
        if not data[0] & 0x80:
            return num
    return None


def _read_varint_bytes(buf: bytes, offset: int = 0) -> tuple[int | None, int]:
    """Read a varint from *buf* starting at *offset*.
    Returns (value, bytes_consumed) — mirrors _read_varint but from a buffer."""
    num = 0
    for i in range(5):
        if offset + i >= len(buf):
            return None, 0
        b = buf[offset + i]
        num |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            return num, i + 1
    return None, 0


def query_status_ping(port: int, timeout: float = 1.5) -> dict | None:
    """Minimal Minecraft status-ping (handshake + request) against
    localhost. Returns the parsed JSON status or None."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            handshake = (
                _varint(0x00)
                + _varint(767)  # protocol version; ignored for status pings
                + _varint(9) + b"127.0.0.1"
                + struct.pack(">H", port)
                + _varint(0x01)  # next state: status
            )
            sock.sendall(_varint(len(handshake)) + handshake)
            request = _varint(0x00)
            sock.sendall(_varint(len(request)) + request)

            length = _read_varint(sock)
            if not length or length > 1_048_576:
                return None
            body = b""
            while len(body) < length:
                chunk = sock.recv(length - len(body))
                if not chunk:
                    return None
                body += chunk

            # body = [packetId varint][stringLength varint][jsonBytes]
            # Parse entirely from the already-buffered body — the socket has
            # no more data to read here (previous bug re-read the socket).
            offset = 0
            packet_id, n = _read_varint_bytes(body, offset)
            if packet_id is None:
                return None
            offset += n
            str_len, n = _read_varint_bytes(body, offset)
            if str_len is None or str_len > len(body) - offset - n:
                return None
            offset += n
            json_bytes = body[offset : offset + str_len]
            return json.loads(json_bytes.decode("utf-8", errors="replace"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _player_count(server_id: str, state: ServerState) -> tuple[int | None, int | None]:
    """(playersOnline, maxPlayers) via a localhost status ping; max falls
    back to server.properties when the ping fails."""
    if state != ServerState.RUNNING:
        return None, None
    port = _server_port(server_id)
    if port is None:
        return None, None
    status = query_status_ping(port)
    online = max_players = None
    if status and isinstance(status.get("players"), dict):
        online = status["players"].get("online")
        max_players = status["players"].get("max")
    if max_players is None:
        try:
            prop = props_module.read_properties(ServerPaths(server_id)).get("max-players")
            max_players = int(prop) if prop else None
        except (ValueError, OSError):
            pass
    return online, max_players


def collect() -> dict:
    """Builds the full report payload for this VM and all its servers."""
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(str(SERVER_ROOT))

    servers = []
    for server_id in sorted_servers():
        state = manager.state(server_id)
        cpu_pct, ram_mb = (
            _process_usage(server_id)
            if state in (ServerState.RUNNING, ServerState.STARTING)
            else (None, None)
        )
        players, max_players = _player_count(server_id, state)
        servers.append(
            {
                "id": server_id,
                "state": state.value,
                "players": players,
                "maxPlayers": max_players,
                "cpu": cpu_pct,
                "ramUsedMb": ram_mb,
                "tps": None,  # no loader-independent source yet; reserved
            }
        )

    return {
        "vm": {
            "cpu": round(cpu, 1),
            "ramUsedGb": round(mem.used / 1024**3, 2),
            "ramTotalGb": round(mem.total / 1024**3, 2),
            "diskUsedGb": round(disk.used / 1024**3, 2),
            "diskTotalGb": round(disk.total / 1024**3, 2),
            "uptimeSeconds": int(time.time() - psutil.boot_time()),
        },
        "servers": servers,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def sorted_servers() -> list[str]:
    from servers import list_server_ids  # local import avoids a cycle

    return list_server_ids()


def push_report(client: httpx.Client) -> int:
    """Pushes one report; returns the HTTP status code the backend gave."""
    payload = collect()
    resp = client.post(
        f"{REPORT_URL}/internal/vm-metrics/{VM_REF}",
        json=payload,
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
        timeout=8.0,
    )
    return resp.status_code


def start_report_loop() -> bool:
    """Launches the 10-second report loop as a daemon thread. Called from
    the FastAPI startup hook, so it runs every time the agent process
    comes up — VM boot (systemd), manual restart, and crash-recovery
    (Restart=always) alike. Returns whether pushing is configured."""
    if not REPORT_URL:
        return False

    def loop():
        client = httpx.Client()
        while True:
            try:
                status = push_report(client)
                if status >= 400:
                    print(f"[monitor] report rejected by backend ({status})")
            except Exception as e:  # never let the loop die
                print(f"[monitor] report push failed: {e}")
            time.sleep(REPORT_INTERVAL_SECONDS)

    threading.Thread(target=loop, name="metric-reporter", daemon=True).start()
    return True
