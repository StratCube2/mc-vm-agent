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
import hashlib
import httpx
import logging
import re
import subprocess
import stat
from pathlib import Path

from config import ServerPaths, is_real_server_jar as _is_real_server_jar

logger = logging.getLogger(__name__)

FABRIC_META = "https://meta.fabricmc.net/v2"
FABRIC_INSTALLER_MAVEN = "https://maven.fabricmc.net/net/fabricmc/fabric-installer"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
MOJANG_MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
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


# Vanilla server jar URLs are looked up first in this JSON gist:
#   [{"version": "26.3", "server": "<jar url>", "client": "<jar url>"}, ...]
# The URL has no revision hash on purpose, so GitHub always serves the
# gist's latest revision — new versions added to the gist are picked up
# without redeploying the agent to every VM. (GitHub's raw CDN caches for
# a few minutes.)
VANILLA_VERSIONS_JSON_URL = (
    "https://gist.githubusercontent.com/StratCube2/"
    "ba249a1a6de1d0058f8b7aaacdec9ced/raw/Minecraft-versions-serverjar.json"
)
# Only jars hosted by Mojang are ever downloaded from a gist-provided URL
# (guards against a bad edit pointing the VM at an arbitrary host). Newer
# versions live on piston-data.mojang.com, older ones on launcher.mojang.com.
_MOJANG_JAR_URL_RE = re.compile(
    r"^https://(?:piston-data|launcher)\.mojang\.com/v1/objects/([0-9a-f]{40})/server\.jar$"
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
            # CDN error bodies (e.g. S3 "Access Denied" XML) are often
            # served with a 200/206 and a small content-length, but never
            # claim to be a jar/binary — checking content-type here catches
            # them before we even write bytes to disk. Some CDNs omit this
            # header entirely, so a missing header is not itself an error;
            # only an explicit non-binary type (text/html, text/xml,
            # application/json, application/xml) is treated as a red flag.
            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type in (
                "text/html", "text/xml", "text/plain",
                "application/json", "application/xml",
            ):
                # Drain a small amount for a more useful error message,
                # then bail without writing a bad file to disk.
                body_preview = b""
                async for chunk in resp.aiter_bytes():
                    body_preview += chunk
                    if len(body_preview) >= 500:
                        break
                raise InstallError(
                    f"Download returned {content_type} instead of a binary file "
                    f"(likely an error page): {url}\n{body_preview[:500]!r}"
                )
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
    size = dest.stat().st_size
    if size == 0:
        dest.unlink(missing_ok=True)
        raise InstallError(f"Download produced an empty file: {url}")
    # A real server jar/installer/binary is always well over a few KB.
    # Error bodies (rate-limit pages, CDN "Access Denied" XML, etc.) are
    # almost always well under that, and can slip past the status-code
    # and content-type checks above (e.g. served as 200 with a generic
    # or missing content-type). Catching this here, before the
    # per-format signature checks, gives a much clearer error message
    # than "not a valid jar" and covers formats with no signature check
    # at all.
    if size < 10_000:
        with open(dest, "rb") as f:
            preview = f.read(500)
        dest.unlink(missing_ok=True)
        raise InstallError(
            f"Download suspiciously small ({size} bytes), likely an error "
            f"response, not a real file: {url}\n{preview!r}"
        )
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


def _parse_versions_json(data) -> dict[str, str]:
    """Parses the gist's JSON list into {mc_version: server_jar_url}.
    Entries without a Mojang-hosted server.jar URL are dropped."""
    if not isinstance(data, list):
        raise InstallError("Vanilla versions JSON must be a list of entries")
    out: dict[str, str] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        version, url = entry.get("version"), entry.get("server")
        if isinstance(version, str) and isinstance(url, str) and _MOJANG_JAR_URL_RE.match(url):
            out[version] = url
    return out


async def _fetch_gist_versions() -> dict[str, str]:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(VANILLA_VERSIONS_JSON_URL)
        r.raise_for_status()
    try:
        table = _parse_versions_json(r.json())
    except ValueError as e:  # malformed JSON
        raise InstallError(f"Vanilla versions JSON is not valid JSON: {e}")
    if not table:
        raise InstallError("Vanilla versions JSON contained no usable server.jar entries")
    return table


def _sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def _install_vanilla_from_gist(paths: ServerPaths, mc_version: str) -> None:
    table = await _fetch_gist_versions()
    url = table.get(mc_version)
    if url is None:
        raise InstallError(f"Minecraft {mc_version} is not listed in the vanilla jar gist")
    expected_sha1 = _MOJANG_JAR_URL_RE.match(url).group(1)
    logger.info("vanilla install (gist): %s -> %s", mc_version, url)

    paths.server_jar.unlink(missing_ok=True)
    await _download(url, paths.server_jar)

    # Mojang's object URLs are content-addressed: the path segment IS the
    # jar's SHA-1, so the download can be verified with no extra metadata.
    actual_sha1 = _sha1_of(paths.server_jar)
    if actual_sha1 != expected_sha1:
        paths.server_jar.unlink(missing_ok=True)
        raise InstallError(
            f"server.jar SHA-1 mismatch for {mc_version}: got {actual_sha1}, "
            f"expected {expected_sha1} (corrupt or intercepted download)"
        )
    _assert_valid_jar(paths.server_jar)


async def install_vanilla(paths: ServerPaths, mc_version: str) -> None:
    """Installs the vanilla server jar for mc_version into server.jar on
    this VM (the agent runs on the VM, so downloading here IS the upload).
    Resolves the URL from the gist first; if the gist is unreachable or
    doesn't list the version, falls back to Mojang's own version manifest."""
    paths.ensure_dirs()
    # A leftover Fabric launcher would take priority over server.jar at
    # launch time (process_manager), so switching to vanilla must drop it.
    paths.fabric_launch_jar.unlink(missing_ok=True)
    await _install_vanilla_jar(paths, mc_version)


async def _install_vanilla_jar(paths: ServerPaths, mc_version: str) -> None:
    """Puts the real vanilla server jar at paths.server_jar (gist first,
    Mojang manifest fallback). Shared by vanilla and Fabric installs."""
    try:
        await _install_vanilla_from_gist(paths, mc_version)
        return
    except (InstallError, httpx.HTTPError) as e:
        logger.warning(
            "vanilla install via gist failed for %s (%s) — falling back to Mojang manifest",
            mc_version, e,
        )
    await _install_vanilla_from_manifest(paths, mc_version)


async def _install_vanilla_from_manifest(paths: ServerPaths, mc_version: str) -> None:
    """Downloads the official Mojang server jar for mc_version straight
    into server.jar — no installer step needed for vanilla."""
    paths.ensure_dirs()
    version_entry = await _get_version_manifest_entry(mc_version)
    logger.info("vanilla install: per-version metadata URL = %s", version_entry.get("url"))

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(version_entry["url"])
        r.raise_for_status()
        version_meta = r.json()

    server_download = version_meta.get("downloads", {}).get("server")
    if not server_download:
        raise InstallError(f"No server jar published for Minecraft {mc_version}")

    logger.info(
        "vanilla install: server jar URL = %s (expected size %s bytes)",
        server_download.get("url"), server_download.get("size"),
    )

    paths.server_jar.unlink(missing_ok=True)
    await _download(server_download["url"], paths.server_jar)

    actual_size = paths.server_jar.stat().st_size
    expected_size = server_download.get("size")
    if expected_size and actual_size != expected_size:
        bad_bytes = paths.server_jar.read_bytes()[:500]
        paths.server_jar.unlink(missing_ok=True)
        raise InstallError(
            f"server.jar size mismatch: got {actual_size} bytes, "
            f"Mojang manifest says it should be {expected_size} bytes. "
            f"This means the download was intercepted or truncated "
            f"(e.g. blocked by the VM's outbound network/firewall/DNS, "
            f"or a captive proxy returning an error page). "
            f"Response preview: {bad_bytes!r}"
        )

    _assert_valid_jar(paths.server_jar)


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

    # Clear anything a previous (buggy or different-loader) install left at
    # server.jar — e.g. a symlink to the launcher stub — so the installer
    # downloads a fresh vanilla jar instead of trusting a bad file.
    paths.server_jar.unlink(missing_ok=True)
    paths.run_script.unlink(missing_ok=True)

    result = subprocess.run(
        [
            "java", "-jar", str(installer_jar),
            "server",
            "-mcversion", mc_version,
            "-loader", loader_version,
            "-dir", str(paths.root),
        ],
        cwd=str(paths.root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise InstallError(f"Fabric install failed:\n{result.stderr[-2000:]}")

    launch_jar = paths.fabric_launch_jar
    if not launch_jar.exists():
        raise InstallError("Fabric install completed but launch jar not found")

    # fabric-server-launch.jar is only a ~600-byte stub. It looks for the
    # REAL vanilla server.jar next to it plus libraries/, and the server is
    # started by running the stub directly (see process_manager). The old
    # code symlinked server.jar -> the stub, which overwrote the vanilla
    # jar the installer had just downloaded and made Fabric find "itself"
    # as the game: "Minecraft game provider couldn't locate the game!".
    libraries_dir = paths.root / "libraries"
    if not libraries_dir.exists() or not any(libraries_dir.rglob("*.jar")):
        raise InstallError(
            "Fabric install completed but libraries/ is missing or empty — "
            "the installer likely failed to download its libraries "
            "(check network access to maven.fabricmc.net from the VM)"
        )

    # Fabric's launcher needs the real vanilla server.jar beside it. Fetch
    # it from the gist (Mojang manifest as fallback) rather than letting
    # the installer download it (-downloadMinecraft is deliberately not
    # passed). SHA-1 verified against the Mojang URL.
    await _install_vanilla_jar(paths, mc_version)
    if not _is_real_server_jar(paths.server_jar):
        raise InstallError(
            "Fabric install finished but server.jar is missing or is not the "
            "vanilla Minecraft jar"
        )


async def install_paper(paths: ServerPaths, mc_version: str) -> None:
    """Downloads the latest STABLE Paper build for mc_version straight
    into server.jar via PaperMC's Fill v3 API — no installer step,
    same launch shape as vanilla (plain `java -jar server.jar`)."""
    paths.ensure_dirs()
    paths.fabric_launch_jar.unlink(missing_ok=True)
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
