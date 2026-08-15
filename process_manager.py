"""
Owns Minecraft server (Java) subprocesses — one per server_id.

Multi-server aware: each server on the VM gets its own entry in the
in-memory registry, its own subprocess, and its own directory
(config.ServerPaths). The manager also enforces the VM's concurrency
cap (MAX_CONCURRENT_SERVERS) so at most vcpus-worth of servers can be
RUNNING/STARTING at once — mirrors the rule the backend enforces before
ever calling here, kept here too as defense in depth.

This is still a plain in-memory singleton at the *registry* level (the
agent process only lives as long as the VM does), it just now fans out
per server_id instead of assuming a single server.
"""
import os
import subprocess
import time
from enum import Enum
from pathlib import Path
from typing import Optional

from config import ServerPaths, MAX_CONCURRENT_SERVERS, java_binary_for


class ServerState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    CRASHED = "crashed"


class ConcurrencyLimitError(Exception):
    pass


class _ServerProcess:
    """Tracks one server's subprocess + lifecycle state."""

    def __init__(self, server_id: str):
        self.paths = ServerPaths(server_id)
        self._proc: Optional[subprocess.Popen] = None
        self._state = ServerState.STOPPED
        self._started_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._log_file = None

    @property
    def state(self) -> ServerState:
        if self._state == ServerState.RUNNING and self._proc is not None:
            if self._proc.poll() is not None:
                self._state = ServerState.CRASHED
                if self._log_file is not None:
                    self._log_file.close()
                    self._log_file = None
        return self._state

    @property
    def uptime_seconds(self) -> Optional[int]:
        if self._state != ServerState.RUNNING or self._started_at is None:
            return None
        return int(time.time() - self._started_at)

    def _write_user_jvm_args(self, xmx: str, xms: str):
        """Forge/NeoForge's run.sh reads JVM flags from user_jvm_args.txt
        in the server directory — it does NOT forward its own trailing
        CLI args to the JVM. Passing -Xmx/-Xms as args to run.sh places
        them after run.sh's internal `java @args.txt ... ` invocation,
        so Java treats them as program arguments for Minecraft itself
        rather than heap flags, leaving JVM memory unconfigured. Writing
        them into user_jvm_args.txt is the mechanism Forge/NeoForge
        actually support for this."""
        args_file = self.paths.root / "user_jvm_args.txt"
        args_file.write_text(f"-Xmx{xmx}\n-Xms{xms}\n")

    def _launch_command(self, xmx: str, xms: str) -> list[str]:
        p = self.paths
        mc_version = p.read_meta().get("mcVersion")
        java = java_binary_for(mc_version)
        if p.run_script.exists():
            self._write_user_jvm_args(xmx, xms)
            # Forge/NeoForge's run.sh invokes its own bundled `java` call
            # internally (via args.txt), so it doesn't take a java path
            # as an argument — point it at the right JDK via PATH instead.
            env_prefix = []
            if java != "java":
                env_prefix = ["env", f"PATH={Path(java).parent}:" + os.environ.get("PATH", "")]
            return env_prefix + ["bash", str(p.run_script), "nogui"]
        if p.server_jar.exists():
            return [java, f"-Xmx{xmx}", f"-Xms{xms}", "-jar", str(p.server_jar), "nogui"]
        raise FileNotFoundError(
            "No launchable server found. Install a loader for this server "
            "first (POST /servers/{id}/install/loader)."
        )

    def ensure_eula(self):
        self.paths.eula_file.write_text("eula=true\n")

    def start(self, xmx: str, xms: str):
        if self.state == ServerState.RUNNING:
            return
        self.paths.ensure_dirs()
        self.ensure_eula()
        cmd = self._launch_command(xmx, xms)
        self._state = ServerState.STARTING
        self._last_error = None
        # Captures stdout/stderr to latest.log so launch-time failures
        # (bad Java version, JVM crash before MC's own logger spins up,
        # missing jar) are visible instead of silently discarded. MC's
        # own logging (once JVM is up) will also write into logs/ via
        # log4j, but that only starts *after* the JVM boots — this
        # catches everything before and after in one place.
        log_file = open(self.paths.latest_log, "a")
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(self.paths.root),
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception as e:
            self._state = ServerState.CRASHED
            self._last_error = str(e)
            log_file.close()
            raise
        self._log_file = log_file
        self._started_at = time.time()
        self._state = ServerState.RUNNING

    def stop(self, timeout: float = 30.0):
        if self._proc is None or self.state not in (ServerState.RUNNING, ServerState.STARTING):
            self._state = ServerState.STOPPED
            return
        self._state = ServerState.STOPPING
        try:
            if self._proc.stdin:
                self._proc.stdin.write("stop\n")
                self._proc.stdin.flush()
            self._proc.wait(timeout=timeout)
        except Exception:
            self._proc.kill()
        finally:
            self._state = ServerState.STOPPED
            self._proc = None
            self._started_at = None
            if self._log_file is not None:
                self._log_file.close()
                self._log_file = None

    def send_command(self, command: str):
        if self.state != ServerState.RUNNING or self._proc is None or self._proc.stdin is None:
            raise RuntimeError("Server is not running")
        self._proc.stdin.write(command.strip() + "\n")
        self._proc.stdin.flush()

    def tail_log(self, lines: int = 200) -> list[str]:
        if not self.paths.latest_log.exists():
            return []
        with open(self.paths.latest_log, "r", errors="ignore") as f:
            return f.readlines()[-lines:]


class MultiServerManager:
    """Registry of _ServerProcess instances, keyed by server_id."""

    def __init__(self):
        self._servers: dict[str, _ServerProcess] = {}

    def _get_or_create(self, server_id: str) -> _ServerProcess:
        if server_id not in self._servers:
            self._servers[server_id] = _ServerProcess(server_id)
        return self._servers[server_id]

    def register(self, server_id: str) -> _ServerProcess:
        """Called when a server is created — sets up its process tracker
        and on-disk dirs without starting anything."""
        sp = self._get_or_create(server_id)
        sp.paths.ensure_dirs()
        return sp

    def forget(self, server_id: str):
        """Called when a server is deleted. Refuses if still running —
        caller should stop() first."""
        sp = self._servers.get(server_id)
        if sp and sp.state in (ServerState.RUNNING, ServerState.STARTING, ServerState.STOPPING):
            raise RuntimeError("Stop the server before deleting it")
        self._servers.pop(server_id, None)

    def running_count(self) -> int:
        return sum(
            1
            for sp in self._servers.values()
            if sp.state in (ServerState.RUNNING, ServerState.STARTING)
        )

    def start(self, server_id: str, xmx: str, xms: str):
        sp = self._get_or_create(server_id)
        if sp.state not in (ServerState.RUNNING, ServerState.STARTING):
            if self.running_count() >= MAX_CONCURRENT_SERVERS:
                raise ConcurrencyLimitError(
                    f"This VM can only run {MAX_CONCURRENT_SERVERS} server(s) "
                    "at once. Stop another server first."
                )
        sp.start(xmx, xms)

    def stop(self, server_id: str, timeout: float = 30.0):
        self._get_or_create(server_id).stop(timeout)

    def send_command(self, server_id: str, command: str):
        self._get_or_create(server_id).send_command(command)

    def tail_log(self, server_id: str, lines: int = 200) -> list[str]:
        return self._get_or_create(server_id).tail_log(lines)

    def state(self, server_id: str) -> ServerState:
        return self._get_or_create(server_id).state

    def uptime_seconds(self, server_id: str) -> Optional[int]:
        return self._get_or_create(server_id).uptime_seconds

    def paths(self, server_id: str) -> ServerPaths:
        return self._get_or_create(server_id).paths


manager = MultiServerManager()
