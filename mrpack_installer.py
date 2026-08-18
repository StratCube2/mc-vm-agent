"""
.mrpack (Modrinth modpack) installer, scoped per server via ServerPaths.

A .mrpack file is a zip containing:
  - modrinth.index.json — pack metadata + a `files` list of mod/resource
    jars to download, each with a per-env "required"/"optional"/
    "unsupported" flag and one or more mirror download URLs.
  - overrides/           — raw files to copy as-is into the server dir
    (configs, etc.) — applies to both client and server installs per
    the mrpack spec.
  - server-overrides/    — same idea, but server-only; layered on top
    of overrides/ when both are present.

This only ever installs the *server* side of a pack: entries whose
`env.server` is "unsupported" are skipped (client-only resource packs,
shaders, etc.), and only overrides/ + server-overrides/ are extracted.

Loader-aware like mods.py: mod files declared at "mods/..." in the
index are redirected to plugins/ instead when the server's loader is
Paper, since Paper never reads mods/. Pumpkin has no jar-mod ecosystem
at all, so pack installs are rejected for it at the route layer in
main.py, same as mods.py's routes.
"""
import asyncio
import io
import json
import zipfile
from pathlib import Path

import httpx
from pydantic import BaseModel

from config import ServerPaths
from mods import PAPER_LIKE_LOADERS

# Sane ceiling for a modpack upload — packs can be large (resource
# packs bundled in overrides/) but this still bounds memory since the
# whole upload is buffered before being opened as a zip.
MAX_MRPACK_BYTES = 500 * 1024 * 1024  # 500 MiB

# Bound concurrency — packs can list 100+ files, and firing them all at
# Modrinth's CDN + this VM's own disk I/O at once is a good way to get
# rate-limited and to briefly starve a running Minecraft process of
# disk/network.
MAX_CONCURRENT_DOWNLOADS = 6


class MrpackError(Exception):
    pass


class MrpackFileResult(BaseModel):
    path: str
    status: str  # "installed" | "skipped_unsupported" | "failed"
    error: str | None = None


class MrpackInstallResult(BaseModel):
    name: str
    versionId: str
    dependencies: dict[str, str]
    files: list[MrpackFileResult]
    overridesApplied: int


def _remap_for_loader(rel_path: str, loader: str) -> str:
    """The index's file paths are written against the client's usual
    mods/ layout. Paper-family servers load jars from plugins/, not
    mods/ — remap the top-level directory so installed jars actually
    get picked up by the running server."""
    if loader in PAPER_LIKE_LOADERS and rel_path.startswith("mods/"):
        return "plugins/" + rel_path[len("mods/"):]
    return rel_path


def _safe_dest(paths: ServerPaths, target_rel: str) -> Path | None:
    """Resolves target_rel against the server root, returning None
    (rather than raising) if it would escape — callers treat that as a
    per-file failure so one malicious/malformed entry doesn't abort
    the whole pack install."""
    dest = (paths.root / target_rel).resolve()
    root = paths.root.resolve()
    if dest != root and root not in dest.parents:
        return None
    return dest


async def _download_one(client: httpx.AsyncClient, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with client.stream("GET", url) as resp:
        if resp.status_code != 200:
            raise MrpackError(f"Download failed ({resp.status_code}): {url}")
        with open(dest, "wb") as f:
            async for chunk in resp.aiter_bytes():
                f.write(chunk)


def _extract_overrides(zf: zipfile.ZipFile, folder: str, dest_root: Path) -> int:
    prefix = folder.rstrip("/") + "/"
    root = dest_root.resolve()
    count = 0
    for info in zf.infolist():
        if info.is_dir() or not info.filename.startswith(prefix):
            continue
        rel = info.filename[len(prefix):]
        if not rel:
            continue
        # Same containment guard as files.py._resolve — a maliciously
        # crafted zip entry name (e.g. "../../etc/cron.d/x") must not
        # be able to write outside the server root.
        target = (dest_root / rel).resolve()
        if target != root and root not in target.parents:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            out.write(src.read())
        count += 1
    return count


async def install_mrpack(paths: ServerPaths, mrpack_bytes: bytes, loader: str) -> MrpackInstallResult:
    if len(mrpack_bytes) > MAX_MRPACK_BYTES:
        raise MrpackError(
            f".mrpack is {len(mrpack_bytes)} bytes, over the {MAX_MRPACK_BYTES}-byte limit"
        )

    paths.ensure_dirs()

    try:
        zf = zipfile.ZipFile(io.BytesIO(mrpack_bytes))
    except zipfile.BadZipFile:
        raise MrpackError("Not a valid .mrpack (bad zip file)")

    try:
        index_raw = zf.read("modrinth.index.json")
    except KeyError:
        raise MrpackError("Not a valid .mrpack: missing modrinth.index.json")

    try:
        index = json.loads(index_raw)
    except json.JSONDecodeError as e:
        raise MrpackError(f"modrinth.index.json is not valid JSON: {e}")

    if index.get("formatVersion") != 1:
        raise MrpackError(
            f"Unsupported mrpack formatVersion: {index.get('formatVersion')!r}"
        )

    files = index.get("files", [])
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:

        async def install_one(entry: dict) -> MrpackFileResult:
            rel_path = entry.get("path", "")
            env = entry.get("env", {})
            if env.get("server") == "unsupported":
                return MrpackFileResult(path=rel_path, status="skipped_unsupported")

            downloads = entry.get("downloads") or []
            if not downloads:
                return MrpackFileResult(
                    path=rel_path, status="failed", error="No download URLs listed"
                )

            target_rel = _remap_for_loader(rel_path, loader)
            dest = _safe_dest(paths, target_rel)
            if dest is None:
                return MrpackFileResult(
                    path=rel_path, status="failed", error="Path escapes server directory"
                )

            async with semaphore:
                last_err = None
                for url in downloads:
                    try:
                        await _download_one(client, url, dest)
                        return MrpackFileResult(path=target_rel, status="installed")
                    except Exception as e:  # try the next mirror, if any
                        last_err = str(e)
                return MrpackFileResult(path=rel_path, status="failed", error=last_err)

        results = await asyncio.gather(*(install_one(f) for f in files))

    overrides_count = 0
    for folder in ("overrides", "server-overrides"):
        overrides_count += _extract_overrides(zf, folder, paths.root)

    return MrpackInstallResult(
        name=index.get("name", "Unknown Pack"),
        versionId=index.get("versionId", ""),
        dependencies=index.get("dependencies", {}),
        files=list(results),
        overridesApplied=overrides_count,
    )
