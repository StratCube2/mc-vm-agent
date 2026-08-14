"""
Mod management, scoped per server via ServerPaths.

Two install paths:
  1. Modrinth — search + install by project id/version (preferred)
  2. Raw jar upload — fallback for CurseForge-only mods or manual jars
"""
import httpx
from pathlib import Path
from pydantic import BaseModel

from config import MODRINTH_API, ServerPaths


class ModrinthResult(BaseModel):
    project_id: str
    slug: str
    title: str
    description: str
    icon_url: str | None
    downloads: int
    latest_version_id: str | None = None


async def search_modrinth(query: str, mc_version: str | None, loader: str | None,
                           limit: int = 20) -> list[ModrinthResult]:
    facets = []
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    if loader:
        facets.append([f"categories:{loader}"])
    params = {"query": query, "limit": limit}
    if facets:
        import json
        params["facets"] = json.dumps(facets)

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{MODRINTH_API}/search", params=params)
        r.raise_for_status()
    hits = r.json()["hits"]
    return [
        ModrinthResult(
            project_id=h["project_id"],
            slug=h["slug"],
            title=h["title"],
            description=h["description"],
            icon_url=h.get("icon_url"),
            downloads=h.get("downloads", 0),
        )
        for h in hits
    ]


async def get_compatible_version(project_id: str, mc_version: str, loader: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{MODRINTH_API}/project/{project_id}/version",
            params={"game_versions": f'["{mc_version}"]', "loaders": f'["{loader}"]'},
        )
        r.raise_for_status()
    versions = r.json()
    if not versions:
        raise ValueError(
            f"No version of {project_id} compatible with {loader} {mc_version}"
        )
    return versions[0]  # Modrinth returns newest-first


async def install_from_modrinth(paths: ServerPaths, project_id: str, mc_version: str, loader: str) -> str:
    version = await get_compatible_version(project_id, mc_version, loader)
    primary_file = next(f for f in version["files"] if f.get("primary", True))
    paths.ensure_dirs()
    dest = paths.mods_dir / primary_file["filename"]

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        r = await client.get(primary_file["url"])
        r.raise_for_status()
        dest.write_bytes(r.content)

    return primary_file["filename"]


def save_uploaded_jar(paths: ServerPaths, filename: str, content: bytes) -> str:
    if not filename.lower().endswith(".jar"):
        raise ValueError("Only .jar files are accepted")
    paths.ensure_dirs()
    safe_name = Path(filename).name  # strip any path traversal
    dest = paths.mods_dir / safe_name
    dest.write_bytes(content)
    return safe_name


def list_installed_mods(paths: ServerPaths) -> list[dict]:
    if not paths.mods_dir.exists():
        return []
    return [
        {"filename": p.name, "size_bytes": p.stat().st_size}
        for p in sorted(paths.mods_dir.glob("*.jar"))
    ]


def remove_mod(paths: ServerPaths, filename: str) -> bool:
    safe_name = Path(filename).name
    target = paths.mods_dir / safe_name
    if target.exists() and target.suffix == ".jar":
        target.unlink()
        return True
    return False
