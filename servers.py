"""
Server metadata + lifecycle CRUD (create / list / get / delete).

Distinct from process_manager.py, which owns the *running Java process*
per server. This module owns the *existence* of a server: its id, name,
loader, mc_version, and disk footprint — persisted as a small meta.json
per server directory so it survives agent restarts (the running process
state does not need to survive a restart, since a VM restart kills the
Java processes anyway).
"""
import json
import shutil
import uuid
from pathlib import Path

from config import SERVERS_ROOT, MAX_SERVER_SLOTS, ServerPaths
from process_manager import manager, ServerState


class SlotLimitError(Exception):
    pass


class ServerNotFoundError(Exception):
    pass


def _meta_path(server_id: str) -> Path:
    return ServerPaths(server_id).meta_file


def _read_meta(server_id: str) -> dict:
    mp = _meta_path(server_id)
    if not mp.exists():
        raise ServerNotFoundError(server_id)
    return json.loads(mp.read_text())


def _write_meta(server_id: str, meta: dict):
    ServerPaths(server_id).ensure_dirs()
    _meta_path(server_id).write_text(json.dumps(meta, indent=2))


def list_server_ids() -> list[str]:
    if not SERVERS_ROOT.exists():
        return []
    return sorted(
        p.name for p in SERVERS_ROOT.iterdir()
        if p.is_dir() and (p / "meta.json").exists()
    )


def create_server(name: str, loader: str, mc_version: str) -> dict:
    existing = list_server_ids()
    if len(existing) >= MAX_SERVER_SLOTS:
        raise SlotLimitError(
            f"This VM has room for {MAX_SERVER_SLOTS} server(s) "
            f"(storage_gb / 8). Delete one first to make room."
        )

    server_id = uuid.uuid4().hex[:12]
    meta = {
        "id": server_id,
        "name": name,
        "loader": loader,
        "mcVersion": mc_version,
    }
    _write_meta(server_id, meta)
    manager.register(server_id)
    return meta


def get_server(server_id: str) -> dict:
    meta = _read_meta(server_id)
    paths = manager.paths(server_id)
    state = manager.state(server_id)
    return {
        **meta,
        "state": state.value,
        "uptimeSeconds": manager.uptime_seconds(server_id),
        "storageUsedGb": paths.storage_used_gb(),
    }


def list_servers() -> list[dict]:
    return [get_server(sid) for sid in list_server_ids()]


def update_server_meta(server_id: str, loader: str | None = None, mc_version: str | None = None) -> dict:
    meta = _read_meta(server_id)
    if loader is not None:
        meta["loader"] = loader
    if mc_version is not None:
        meta["mcVersion"] = mc_version
    _write_meta(server_id, meta)
    return meta


def delete_server(server_id: str):
    if server_id not in list_server_ids():
        raise ServerNotFoundError(server_id)
    state = manager.state(server_id)
    if state in (ServerState.RUNNING, ServerState.STARTING, ServerState.STOPPING):
        raise RuntimeError("Stop the server before deleting it")
    manager.forget(server_id)
    shutil.rmtree(ServerPaths(server_id).root, ignore_errors=True)
