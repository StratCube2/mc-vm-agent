"""
Full-server and whole-VM backups as streamed .zip downloads.

Design notes:
- Zips are written incrementally to a temp file inside the server's own
  .downloads staging dir (excluded from the archive itself), then served
  with FileResponse + a BackgroundTask that deletes the temp file once
  the response finishes — no GB-sized buffers held in RAM.
- Exclusions skip the agent's own staging dirs and previously generated
  backup zips so backups never nest themselves.
- Running servers ARE allowed to be backed up (hot backups are genuinely
  useful mid-session) — the caller surfaces the consistency caveat.
"""
import os
import re
import tempfile
import zipfile
from pathlib import Path

from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from config import SERVER_ROOT, ServerPaths

# Never include these path segments in an archive: our own staging areas
# and old backup artifacts would bloat (and recursively nest) archives.
_EXCLUDED_DIRS = {".downloads"}
_EXCLUDED_SUFFIXES = (".zip.part",)
_EXCLUDED_FILES = set()


def _included(path: Path) -> bool:
    return (
        not any(part in _EXCLUDED_DIRS for part in path.parts[:-1])
        and not path.name.endswith(_EXCLUDED_SUFFIXES)
        and path.name not in _EXCLUDED_FILES
    )


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_")
    return cleaned[:48] or "server"


def build_server_backup(server_id: str, paths: ServerPaths) -> Path:
    """Zips one server directory; returns the temp file path."""
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=str(paths.downloads_dir))
    os.close(fd)
    tmp_path = Path(tmp)

    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for f in sorted(paths.root.rglob("*")):
            if f.is_file() and _included(f.relative_to(paths.root)):
                zf.write(f, arcname=str(f.relative_to(paths.root)))
    return tmp_path


def backup_response(server_id: str, display_name: str) -> FileResponse:
    """One-server backup, cleaned up after the download completes."""
    paths = ServerPaths(server_id)
    paths.ensure_dirs()
    tmp_path = build_server_backup(server_id, paths)
    filename = f"{_safe_name(display_name)}-backup.zip"
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(os.unlink, str(tmp_path)),
    )


def build_vm_backup() -> tuple[Path, list[str]]:
    """Zips EVERY server on this VM into one archive, each under its own
    top-level folder (<safe-name>-<id>/...). Returns (tempfile, names).
    Temp file lives outside any server dir, so nothing excludes itself."""
    import servers as servers_module

    servers_root = SERVER_ROOT / ".downloads"
    servers_root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=str(servers_root))
    os.close(fd)
    tmp_path = Path(tmp)

    included_servers: list[str] = []
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for meta in servers_module.list_servers():
            sid = meta["id"]
            top = f"{_safe_name(meta['name'])}-{sid}"
            root = ServerPaths(sid).root
            if not root.is_dir():
                continue
            included_servers.append(meta["name"])
            for f in sorted(root.rglob("*")):
                rel = f.relative_to(root)
                if f.is_file() and _included(rel):
                    zf.write(f, arcname=f"{top}/{rel}")
    return tmp_path, included_servers


def vm_backup_response() -> FileResponse:
    tmp_path, _names = build_vm_backup()
    import time

    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"vm-backup-{stamp}.zip"
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(os.unlink, str(tmp_path)),
    )
