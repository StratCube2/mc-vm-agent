"""
Downloads and installs the actual server jar for a given loader + MC
version combo, scoped to a single server's directory (ServerPaths).
Each loader has a different install shape:

  - Fabric:    download the fabric installer, run it with --server,
               produces a plain server.jar equivalent (fabric-server-launch.jar)
  - Forge:     download the forge installer for the exact forge build,
               run it with --installServer, produces run.sh/run.bat
  - NeoForge:  same shape as Forge (NeoForge is a Forge fork), produces run.sh
  - Pumpkin:   download the prebuilt nightly binary straight from GitHub
               Releases — no installer step, no JVM at all (Pumpkin is a
               native Rust server implementation of the vanilla protocol).

All JVM-based installers are run with the VM's system Java, so Java
itself must already be present (handled by the VM provisioning
cloud-init, not here). Pumpkin needs no Java at all.
"""
import httpx
import subprocess
import stat
from pathlib import Path

from config import ServerPaths

FABRIC_META = "https://meta.fabricmc.net/v2"
FABRIC_INSTALLER_MAVEN = "https://maven.fabricmc.net/net/fabricmc/fabric-installer"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
MOJANG_MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
PAPER_FILL_API = "https://fill.papermc.io/v3/projects/paper"

# Fill (PaperMC's downloads API) requires every request to send a
# non-generic User-Agent identifying the calling software, or requests
# are rejected outright — see https://docs.papermc.io/misc/downloads-service/
PAPER_USER_AGENT = "PulseHost-mc-vm-agent/1.0 (+https://pulshost.netlify.app)"

# Pumpkin ships prebuilt nightly binaries as GitHub release assets under
# the fixed "nightly" tag (rolling — always the latest build). All Azure
# VM sizes this platform provisions (Das_v4 / als_v2 / ats_v2) are x64,
# so the Linux x64 asset is the only one ever relevant here.
PUMPKIN_NIGHTLY_ASSET_URL = (
    "https://github.com/Pumpkin-MC/Pumpkin/releases/download/nightly/pumpkin-X64-Linux"
)


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


async def _get_version_manifest_entry(mc_version: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(MOJANG_MANIFEST)
        r.raise_for_status()
        data = r.json()
    entry = next((v for v in data["versions"] if v["id"] == mc_version), None)
    if entry is None:
        raise InstallError(f"Unknown Minecraft version: {mc_version}")
    return entry


async def list_fabric_loader_versions() -> list[str]:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{FABRIC_META}/versions/loader")
        r.raise_for_status()
    return [v["version"] for v in r.json()]


async def _latest_fabric_installer_version() -> str:
    """Fabric installer versions are unrelated to loader versions and
    change independently — pinning a literal string here goes stale
    (or may never have been valid). Ask the meta API instead."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{FABRIC_META}/versions/installer")
        r.raise_for_status()
    versions = r.json()
    if not versions:
        raise InstallError("Could not fetch Fabric installer versions")
    return versions[0]["version"]  # latest stable is first


async def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise InstallError(f"Download failed ({resp.status_code}): {url}")
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
    if dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise InstallError(f"Download produced an empty file: {url}")
    return dest


def _assert_valid_jar(path: Path) -> None:
    """A failed/partial/HTML-error download can pass the HTTP status
    check and still not be a real jar. Jars are zip files, which always
    start with the 'PK' local-file-header signature — cheap way to catch
    a corrupt/wrong download before wasting time running it."""
    with open(path, "rb") as f:
        header = f.read(2)
    if header != b"PK":
        raise InstallError(
            f"{path.name} does not look like a valid jar (bad download?)"
        )


async def install_vanilla(paths: ServerPaths, mc_version: str) -> None:
    """Downloads the official Mojang server jar for mc_version straight
    into server.jar — no installer step needed for vanilla."""
    paths.ensure_dirs()
    version_entry = await _get_version_manifest_entry(mc_version)

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(version_entry["url"])
        r.raise_for_status()
        version_meta = r.json()

    server_download = version_meta.get("downloads", {}).get("server")
    if not server_download:
        raise InstallError(f"No server jar published for Minecraft {mc_version}")

    paths.server_jar.unlink(missing_ok=True)
    await _download(server_download["url"], paths.server_jar)


async def install_fabric(paths: ServerPaths, mc_version: str, loader_version: str | None = None) -> None:
    paths.ensure_dirs()
    if loader_version is None:
        versions = await list_fabric_loader_versions()
        if not versions:
            raise InstallError("Could not fetch Fabric loader versions")
        loader_version = versions[0]  # latest stable is first

    # Fabric installer jar version — fetched from the meta API rather than
    # pinned, since installer releases are independent of loader/mc
    # versions and a stale/invalid pin here silently breaks the install
    # (installer "succeeds" but produces a launch jar that can't find the
    # game — the exact symptom of a bad/corrupt installer jar).
    installer_version = await _latest_fabric_installer_version()
    installer_url = (
        f"{FABRIC_INSTALLER_MAVEN}/{installer_version}/"
        f"fabric-installer-{installer_version}.jar"
    )
    installer_jar = await _download(installer_url, paths.downloads_dir / "fabric-installer.jar")
    _assert_valid_jar(installer_jar)

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

    # The launch jar is a thin wrapper — it needs the actual vanilla
    # server jar (fetched via -downloadMinecraft) plus libraries/ next
    # to it. If those are missing, the launch jar runs but throws
    # exactly the "game provider couldn't locate the game" error you're
    # debugging, with a successful-looking installer exit code.
    libraries_dir = paths.root / "libraries"
    if not libraries_dir.exists() or not any(libraries_dir.rglob("*.jar")):
        raise InstallError(
            "Fabric install completed but libraries/ is missing or empty — "
            "the installer likely failed to download Minecraft itself "
            "(check network access to launchermeta.mojang.com / "
            "piston-data.mojang.com from the VM)"
        )
    # unlink(missing_ok=True) removes a regular file OR a broken symlink;
    # server_jar.exists() would return False for a broken symlink (it
    # follows the link to check the target), silently skipping removal
    # and causing symlink_to() below to raise FileExistsError.
    paths.server_jar.unlink(missing_ok=True)
    paths.server_jar.symlink_to(launch_jar)


async def install_paper(paths: ServerPaths, mc_version: str) -> None:
    """Downloads the latest STABLE Paper build for mc_version straight
    into server.jar via PaperMC's Fill v3 API — no installer step,
    same launch shape as vanilla (plain `java -jar server.jar`)."""
    paths.ensure_dirs()
    headers = {"User-Agent": PAPER_USER_AGENT}

    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        r = await client.get(f"{PAPER_FILL_API}/versions/{mc_version}/builds")
        if r.status_code == 404:
            raise InstallError(f"Paper does not publish builds for Minecraft {mc_version}")
        r.raise_for_status()
        builds = r.json()

    stable = next((b for b in builds if b.get("channel") == "STABLE"), None)
    if stable is None:
        raise InstallError(
            f"No stable Paper build available for Minecraft {mc_version} yet "
            "(Paper builds usually lag a few days behind a new MC release)"
        )
    download = stable.get("downloads", {}).get("server:default")
    if not download or not download.get("url"):
        raise InstallError(f"Paper build {stable.get('id')} has no server download listed")

    paths.server_jar.unlink(missing_ok=True)
    await _download(download["url"], paths.server_jar)
    _assert_valid_jar(paths.server_jar)


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


async def install_pumpkin(paths: ServerPaths) -> None:
    """Downloads the latest Pumpkin nightly binary straight into
    pumpkin_server (no installer, no Java, no mc_version — the nightly
    tag is a rolling release and always targets the newest supported
    protocol version). process_manager launches this directly as a
    native executable instead of via `java -jar`."""
    paths.ensure_dirs()
    binary_path = paths.root / "pumpkin_server"
    binary_path.unlink(missing_ok=True)
    await _download(PUMPKIN_NIGHTLY_ASSET_URL, binary_path)

    # Unlike jars, this is a raw ELF binary — sanity-check the ELF magic
    # instead of the "PK" zip signature, and make sure it's executable
    # (downloads land with default, non-executable permissions).
    with open(binary_path, "rb") as f:
        header = f.read(4)
    if header != b"\x7fELF":
        raise InstallError(
            f"{binary_path.name} does not look like a valid binary (bad download?)"
        )
    binary_path.chmod(binary_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # Clear out any stale server_jar/run.sh from a previous loader so
    # process_manager's launch-command detection doesn't pick the wrong
    # one — Pumpkin has its own dedicated launch path (paths.pumpkin_bin).
    paths.server_jar.unlink(missing_ok=True)
    if paths.run_script.exists():
        paths.run_script.unlink()


LOADERS = {
    "vanilla": install_vanilla,
    "fabric": install_fabric,
    "forge": install_forge,
    "neoforge": install_neoforge,
    "pumpkin": install_pumpkin,
    "paper": install_paper,
}
