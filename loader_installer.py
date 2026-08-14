"""
Downloads and installs the actual server jar for a given loader + MC
version combo, scoped to a single server's directory (ServerPaths).
Each loader has a different install shape:

  - Fabric:    download the fabric installer, run it with --server,
               produces a plain server.jar equivalent (fabric-server-launch.jar)
  - Forge:     download the forge installer for the exact forge build,
               run it with --installServer, produces run.sh/run.bat
  - NeoForge:  same shape as Forge (NeoForge is a Forge fork), produces run.sh

All installers are run with the VM's system Java, so Java itself must
already be present (handled by the VM provisioning cloud-init, not here).
"""
import httpx
import subprocess
from pathlib import Path

from config import ServerPaths

FABRIC_META = "https://meta.fabricmc.net/v2"
FABRIC_INSTALLER_MAVEN = "https://maven.fabricmc.net/net/fabricmc/fabric-installer"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
MOJANG_MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"


class InstallError(Exception):
    pass


async def list_mc_versions(release_only: bool = True) -> list[str]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(MOJANG_MANIFEST)
        r.raise_for_status()
        data = r.json()
    versions = data["versions"]
    if release_only:
        versions = [v for v in versions if v["type"] == "release"]
    return [v["id"] for v in versions]


async def list_fabric_loader_versions() -> list[str]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{FABRIC_META}/versions/loader")
        r.raise_for_status()
    return [v["version"] for v in r.json()]


async def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise InstallError(f"Download failed ({resp.status_code}): {url}")
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
    return dest


async def install_fabric(paths: ServerPaths, mc_version: str, loader_version: str | None = None) -> None:
    paths.ensure_dirs()
    if loader_version is None:
        versions = await list_fabric_loader_versions()
        if not versions:
            raise InstallError("Could not fetch Fabric loader versions")
        loader_version = versions[0]  # latest stable is first

    # Fabric installer jar version — use a known-recent pin; agent doesn't
    # need to track this closely since the installer itself pulls the
    # actual loader/mc artifacts.
    installer_version = "1.0.1"
    installer_url = (
        f"{FABRIC_INSTALLER_MAVEN}/{installer_version}/"
        f"fabric-installer-{installer_version}.jar"
    )
    installer_jar = await _download(installer_url, paths.downloads_dir / "fabric-installer.jar")

    result = subprocess.run(
        [
            "java", "-jar", str(installer_jar),
            "server",
            "-mcversion", mc_version,
            "-loader", loader_version,
            "-dir", str(paths.root),
            "-downloadMinecraft",
        ],
        cwd=str(paths.root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise InstallError(f"Fabric install failed:\n{result.stderr[-2000:]}")

    launch_jar = paths.root / "fabric-server-launch.jar"
    if not launch_jar.exists():
        raise InstallError("Fabric install completed but launch jar not found")
    if paths.server_jar.exists():
        paths.server_jar.unlink()
    paths.server_jar.symlink_to(launch_jar)


async def install_forge(paths: ServerPaths, mc_version: str, forge_version: str) -> None:
    """forge_version is the FULL forge build string, e.g. '20.1.0'
    (as published under the mc_version-forge_version maven path)."""
    paths.ensure_dirs()
    full = f"{mc_version}-{forge_version}"
    installer_url = f"{FORGE_MAVEN}/{full}/forge-{full}-installer.jar"
    installer_jar = await _download(installer_url, paths.downloads_dir / "forge-installer.jar")

    result = subprocess.run(
        ["java", "-jar", str(installer_jar), "--installServer"],
        cwd=str(paths.root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise InstallError(f"Forge install failed:\n{result.stderr[-2000:]}")
    # Forge installer generates run.sh / run.bat — process_manager picks
    # run_script up automatically if present.


async def install_neoforge(paths: ServerPaths, neoforge_version: str) -> None:
    """neoforge_version e.g. '21.1.57' — NeoForge versions encode the MC
    version implicitly (21.1.x == MC 1.21.1), so no separate mc_version arg."""
    paths.ensure_dirs()
    installer_url = (
        f"{NEOFORGE_MAVEN}/{neoforge_version}/"
        f"neoforge-{neoforge_version}-installer.jar"
    )
    installer_jar = await _download(installer_url, paths.downloads_dir / "neoforge-installer.jar")

    result = subprocess.run(
        ["java", "-jar", str(installer_jar), "--installServer"],
        cwd=str(paths.root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise InstallError(f"NeoForge install failed:\n{result.stderr[-2000:]}")


LOADERS = {
    "fabric": install_fabric,
    "forge": install_forge,
    "neoforge": install_neoforge,
}
