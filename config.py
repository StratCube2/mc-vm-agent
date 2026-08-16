"""
Central config for the VM agent.

Multi-server layout: each server gets its own subdirectory under
SERVERS_ROOT, keyed by server_id. All the path helpers here are
per-server so the rest of the agent never has to think about disk
layout directly.

    /opt/mcserver/
      servers/
        <server_id>/
          mods/
          world/
          logs/latest.log
          server.properties
          eula.txt
          .downloads/
          run.sh | server.jar
          meta.json
"""
import json
import os
from pathlib import Path

# Root directory for everything this agent manages.
SERVER_ROOT = Path(os.environ.get("MC_SERVER_ROOT", "/opt/mcserver"))
SERVERS_ROOT = SERVER_ROOT / "servers"

# This VM's fixed capacity, provisioned at VM-create time. The agent
# enforces slot/concurrency limits itself as a second line of defense —
# the backend also enforces them before ever calling here.
VM_VCPUS = int(os.environ.get("MC_VM_VCPUS", "2"))
VM_STORAGE_GB = int(os.environ.get("MC_VM_STORAGE_GB", "32"))
GB_PER_SLOT = 8

MAX_SERVER_SLOTS = max(1, VM_STORAGE_GB // GB_PER_SLOT)
MAX_CONCURRENT_SERVERS = max(1, VM_VCPUS)

# Shared secret the Render backend must send as a Bearer token.
AGENT_TOKEN = os.environ.get("MC_AGENT_TOKEN", "")

# Default JVM memory args — overridable per-start-request.
DEFAULT_XMX = os.environ.get("MC_DEFAULT_XMX", "6G")
DEFAULT_XMS = os.environ.get("MC_DEFAULT_XMS", "2G")

MODRINTH_API = "https://api.modrinth.com/v2"

SERVERS_ROOT.mkdir(parents=True, exist_ok=True)


class ServerPaths:
    """Resolved on-disk paths for a single server instance."""

    def __init__(self, server_id: str):
        self.id = server_id
        self.root = SERVERS_ROOT / server_id
        self.mods_dir = self.root / "mods"
        # Paper (and Bukkit/Spigot-family servers generally) load jars
        # from plugins/, not mods/ — Fabric/Forge/NeoForge and Paper are
        # never the same loader on the same server, so this is always
        # unambiguous per-server. mods_dir stays the modding path for
        # the JVM mod loaders; plugins_dir is Paper-only.
        self.plugins_dir = self.root / "plugins"
        self.world_dir = self.root / "world"
        self.logs_dir = self.root / "logs"
        self.latest_log = self.logs_dir / "latest.log"
        self.properties_file = self.root / "server.properties"
        self.eula_file = self.root / "eula.txt"
        self.downloads_dir = self.root / ".downloads"
        self.run_script = self.root / "run.sh"
        self.server_jar = self.root / "server.jar"
        self.pumpkin_bin = self.root / "pumpkin_server"
        self.meta_file = self.root / "meta.json"  # name, loader, mc_version

    def ensure_dirs(self):
        for d in (self.root, self.mods_dir, self.plugins_dir, self.world_dir, self.logs_dir, self.downloads_dir):
            d.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.root.exists()

    def storage_used_gb(self) -> float:
        if not self.root.exists():
            return 0.0
        total = 0
        for p in self.root.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
        return round(total / (1024 ** 3), 2)

    def read_meta(self) -> dict:
        if not self.meta_file.exists():
            return {}
        return json.loads(self.meta_file.read_text())


# Minecraft's required JDK jumped over time. Cutoffs below are the
# earliest MC version (inclusive) that needs the given JDK; a version
# uses the highest cutoff it meets. cloud-init installs all of these
# side by side (see infra) — the agent just points `java` at the right
# one per server, keyed off that server's own mc_version, since one VM
# can host servers spanning multiple MC versions at once.
JAVA_VERSION_CUTOFFS: list[tuple[tuple[int, ...], int]] = [
    ((1, 21, 8), 25),
    ((1, 20, 5), 21),
    ((1, 18), 17),
    ((0, 0, 0), 8),
]
DEFAULT_JAVA_VERSION = 21


def _parse_mc_version(mc_version: str) -> tuple[int, ...] | None:
    # Release versions only ("1.21.8"); snapshots/pre-releases etc. fall
    # back to the default rather than guessing.
    parts = mc_version.split(".")
    if not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def java_binary_for(mc_version: str | None) -> str:
    """Resolves to a java executable path for the given MC version.
    Falls back to the default JDK's `java` on PATH if mc_version is
    missing/unparseable or a matching install can't be found on disk."""
    if mc_version:
        parsed = _parse_mc_version(mc_version)
        if parsed is not None:
            for cutoff, jdk in JAVA_VERSION_CUTOFFS:
                if parsed >= cutoff:
                    candidate = f"/usr/lib/jvm/java-{jdk}-openjdk-amd64/bin/java"
                    if Path(candidate).exists():
                        return candidate
                    break
    return "java"
