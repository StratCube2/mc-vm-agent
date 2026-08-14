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
        self.world_dir = self.root / "world"
        self.logs_dir = self.root / "logs"
        self.latest_log = self.logs_dir / "latest.log"
        self.properties_file = self.root / "server.properties"
        self.eula_file = self.root / "eula.txt"
        self.downloads_dir = self.root / ".downloads"
        self.run_script = self.root / "run.sh"
        self.server_jar = self.root / "server.jar"
        self.meta_file = self.root / "meta.json"  # name, loader, mc_version

    def ensure_dirs(self):
        for d in (self.root, self.mods_dir, self.world_dir, self.logs_dir, self.downloads_dir):
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
