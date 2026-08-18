"""
VM Agent — runs on the Azure VM itself as a systemd service.
Only ever called by the Render backend (bearer-token authed), never
directly by a browser.

Multi-server aware: every server-scoped route is namespaced under
/servers/{server_id}/... A VM can host multiple servers up to
MAX_SERVER_SLOTS (storage_gb / 8), and run up to MAX_CONCURRENT_SERVERS
(= vcpus) of them at once — both enforced here as well as by the backend.

Run: uvicorn main:app --host 0.0.0.0 --port 8443
"""
from fastapi import FastAPI, APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

from auth import require_agent_token
from config import DEFAULT_XMX, DEFAULT_XMS, MAX_SERVER_SLOTS, MAX_CONCURRENT_SERVERS
from process_manager import manager, ConcurrencyLimitError
import servers as servers_module
from servers import SlotLimitError, ServerNotFoundError
import loader_installer
import mods as mods_module
import properties as props_module
import files as files_module
import mrpack_installer

app = FastAPI(title="MC VM Agent")
authed = APIRouter(dependencies=[Depends(require_agent_token)])


@app.get("/health")
def health():
    """Unauthenticated so Render/uptime checks can confirm the agent is
    reachable even before/independent of token setup."""
    return {"ok": True}


def get_server_or_404(server_id: str) -> dict:
    try:
        return servers_module.get_server(server_id)
    except ServerNotFoundError:
        raise HTTPException(404, f"No server with id {server_id}")


# ---------- VM-level capacity ----------

@authed.get("/capacity")
def capacity():
    return {
        "maxServerSlots": MAX_SERVER_SLOTS,
        "maxConcurrentServers": MAX_CONCURRENT_SERVERS,
        "usedSlots": len(servers_module.list_server_ids()),
        "runningServers": manager.running_count(),
    }


# ---------- Server CRUD ----------

class CreateServerRequest(BaseModel):
    name: str
    loader: str
    mc_version: str | None = None  # not required for pumpkin (nightly targets the newest protocol automatically)
    loader_version: str | None = None  # fabric loader version / forge & neoforge build; ignored for vanilla/pumpkin


@authed.get("/servers")
def list_servers():
    return {"servers": servers_module.list_servers()}


async def _run_install(server_id: str, loader: str, mc_version: str, loader_version: str | None):
    """Runs the binary install for a freshly created server and records
    the outcome on its meta.json. Called synchronously from
    create_server (the frontend/backend already treat server creation as
    a longer-running op — see e.g. VM provisioning's fire-and-forget
    shape — but unlike that case, the caller here is directly waiting on
    the HTTP response to know whether the server is actually usable, so
    this is awaited rather than fired-and-forgotten)."""
    paths = manager.paths(server_id)
    servers_module.update_server_meta(server_id, install_state="installing")
    try:
        installer = loader_installer.LOADERS.get(loader)
        if installer is None:
            raise loader_installer.InstallError(f"Unknown loader: {loader}")
        if loader == "vanilla":
            await installer(paths, mc_version)
        elif loader == "paper":
            await installer(paths, mc_version)
        elif loader == "fabric":
            await installer(paths, mc_version, loader_version)
        elif loader == "forge":
            if not loader_version:
                raise loader_installer.InstallError("loader_version (forge build) required")
            await installer(paths, mc_version, loader_version)
        elif loader == "neoforge":
            if not loader_version:
                raise loader_installer.InstallError("loader_version (neoforge build) required")
            await installer(paths, loader_version)
        elif loader == "pumpkin":
            await installer(paths)
        servers_module.update_server_meta(server_id, install_state="ready", install_error=None)
    except Exception as e:
        servers_module.update_server_meta(
            server_id, install_state="failed", install_error=str(e)
        )


@authed.post("/servers")
async def create_server(req: CreateServerRequest):
    try:
        meta = servers_module.create_server(req.name, req.loader, req.mc_version)
    except SlotLimitError as e:
        raise HTTPException(409, str(e))

    # Install synchronously as part of server creation — previously
    # create_server only wrote meta.json + directories and never
    # downloaded a server jar at all, so every server's first start
    # failed with FileNotFoundError. This makes creation take as long as
    # the download+install (matches how the frontend already shows
    # "Creating..." while awaiting this call), and the server comes back
    # from this endpoint actually startable.
    await _run_install(meta["id"], req.loader, req.mc_version, req.loader_version)

    return servers_module.get_server(meta["id"])


@authed.get("/servers/{server_id}")
def get_server(server_id: str):
    return get_server_or_404(server_id)


@authed.delete("/servers/{server_id}")
def delete_server(server_id: str):
    get_server_or_404(server_id)
    try:
        servers_module.delete_server(server_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


# ---------- Server process control ----------

class StartRequest(BaseModel):
    xmx: str = DEFAULT_XMX
    xms: str = DEFAULT_XMS


@authed.post("/servers/{server_id}/start")
def start_server(server_id: str, req: StartRequest):
    get_server_or_404(server_id)
    try:
        manager.start(server_id, req.xmx, req.xms)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except ConcurrencyLimitError as e:
        raise HTTPException(409, str(e))
    return {"state": manager.state(server_id).value}


@authed.post("/servers/{server_id}/stop")
def stop_server(server_id: str):
    get_server_or_404(server_id)
    manager.stop(server_id)
    return {"state": manager.state(server_id).value}


@authed.post("/servers/{server_id}/command")
def send_command(server_id: str, command: str = Form(...)):
    get_server_or_404(server_id)
    try:
        manager.send_command(server_id, command)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@authed.get("/servers/{server_id}/status")
def server_status(server_id: str):
    get_server_or_404(server_id)
    return {
        "state": manager.state(server_id).value,
        "uptime_seconds": manager.uptime_seconds(server_id),
    }


@authed.get("/servers/{server_id}/logs")
def get_logs(server_id: str, lines: int = 200):
    get_server_or_404(server_id)
    return {"lines": manager.tail_log(server_id, lines)}


# ---------- Loader / MC version install ----------

class InstallRequest(BaseModel):
    loader: str            # "fabric" | "forge" | "neoforge"
    mc_version: str | None = None   # required for fabric/forge; forge also needs forge_version
    loader_version: str | None = None  # fabric loader version, or forge/neoforge build


@authed.get("/install/mc-versions")
async def mc_versions():
    return {"versions": await loader_installer.list_mc_versions()}


@authed.get("/install/fabric-loader-versions")
async def fabric_versions():
    return {"versions": await loader_installer.list_fabric_loader_versions()}


@authed.post("/servers/{server_id}/install/loader")
async def install_loader(server_id: str, req: InstallRequest):
    get_server_or_404(server_id)
    if manager.state(server_id).value == "running":
        raise HTTPException(409, "Stop the server before changing loader/version")

    paths = manager.paths(server_id)
    servers_module.update_server_meta(server_id, install_state="installing")

    try:
        if req.loader == "vanilla":
            if not req.mc_version:
                raise HTTPException(400, "mc_version required for vanilla")
            await loader_installer.install_vanilla(paths, req.mc_version)
        elif req.loader == "paper":
            if not req.mc_version:
                raise HTTPException(400, "mc_version required for paper")
            await loader_installer.install_paper(paths, req.mc_version)
        elif req.loader == "fabric":
            if not req.mc_version:
                raise HTTPException(400, "mc_version required for fabric")
            await loader_installer.install_fabric(paths, req.mc_version, req.loader_version)
        elif req.loader == "forge":
            if not req.mc_version or not req.loader_version:
                raise HTTPException(400, "mc_version and loader_version (forge build) required")
            await loader_installer.install_forge(paths, req.mc_version, req.loader_version)
        elif req.loader == "neoforge":
            if not req.loader_version:
                raise HTTPException(400, "loader_version (neoforge build) required")
            await loader_installer.install_neoforge(paths, req.loader_version)
        elif req.loader == "pumpkin":
            await loader_installer.install_pumpkin(paths)
        else:
            raise HTTPException(400, f"Unknown loader: {req.loader}")
    except loader_installer.InstallError as e:
        servers_module.update_server_meta(server_id, install_state="failed", install_error=str(e))
        raise HTTPException(500, str(e))
    except HTTPException:
        servers_module.update_server_meta(server_id, install_state="failed", install_error="Invalid install request")
        raise

    servers_module.update_server_meta(
        server_id,
        loader=req.loader,
        mc_version=req.mc_version or ("nightly" if req.loader == "pumpkin" else req.mc_version),
        install_state="ready",
        install_error=None,
    )
    return {"ok": True, "loader": req.loader}


# ---------- Server metadata (rename) ----------

class UpdateServerRequest(BaseModel):
    name: str


@authed.patch("/servers/{server_id}")
def rename_server(server_id: str, req: UpdateServerRequest):
    get_server_or_404(server_id)
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "name cannot be empty")
    servers_module.update_server_meta(server_id, name=name)
    return servers_module.get_server(server_id)


# ---------- Mods / Plugins ----------
# "mods" here covers Fabric/Forge/NeoForge mods AND Paper plugins —
# mods_module.target_dir() picks mods/ vs plugins/ based on loader.
# Pumpkin has no Modrinth-distributed jar ecosystem, so its routes 409.

def _require_moddable(server_id: str) -> dict:
    server = get_server_or_404(server_id)
    if server["loader"] == "pumpkin":
        raise HTTPException(409, "Pumpkin does not support Modrinth mods/plugins yet")
    return server


@authed.get("/servers/{server_id}/mods")
def list_mods(server_id: str):
    server = _require_moddable(server_id)
    return {"mods": mods_module.list_installed_mods(manager.paths(server_id), server["loader"])}


@authed.get("/servers/{server_id}/mods/search")
async def search_mods(server_id: str, q: str, mc_version: str | None = None, loader: str | None = None):
    _require_moddable(server_id)
    results = await mods_module.search_modrinth(q, mc_version, loader)
    return {"results": [r.model_dump() for r in results]}


class ModInstallRequest(BaseModel):
    project_id: str
    mc_version: str
    loader: str


@authed.post("/servers/{server_id}/mods/install")
async def install_mod(server_id: str, req: ModInstallRequest):
    _require_moddable(server_id)
    try:
        filename = await mods_module.install_from_modrinth(
            manager.paths(server_id), req.project_id, req.mc_version, req.loader
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "filename": filename}


@authed.post("/servers/{server_id}/mods/upload")
async def upload_mod(server_id: str, file: UploadFile = File(...)):
    server = _require_moddable(server_id)
    content = await file.read()
    try:
        filename = mods_module.save_uploaded_jar(manager.paths(server_id), file.filename, content, server["loader"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "filename": filename}


@authed.delete("/servers/{server_id}/mods/{filename}")
def delete_mod(server_id: str, filename: str):
    server = _require_moddable(server_id)
    if mods_module.remove_mod(manager.paths(server_id), filename, server["loader"]):
        return {"ok": True}
    raise HTTPException(404, "Mod not found")


# ---------- server.properties ----------

@authed.get("/servers/{server_id}/properties")
def get_properties(server_id: str):
    get_server_or_404(server_id)
    return props_module.read_properties(manager.paths(server_id))


@authed.post("/servers/{server_id}/properties")
def update_properties(server_id: str, updates: dict[str, str]):
    get_server_or_404(server_id)
    return props_module.write_properties(manager.paths(server_id), updates)


# ---------- File explorer / editor ----------
# Every route here is scoped to a single server's own directory
# (ServerPaths.root) via files_module._resolve — a client-supplied
# path can never reach outside it. See files.py's module docstring.

def _files_error(server_id: str, e: Exception):
    if isinstance(e, files_module.NotFoundError):
        raise HTTPException(404, f"Not found: {e}")
    if isinstance(e, files_module.PathEscapeError):
        raise HTTPException(400, str(e))
    if isinstance(e, files_module.NotADirectoryErr):
        raise HTTPException(400, f"Not a directory: {e}")
    if isinstance(e, files_module.IsADirectoryErr):
        raise HTTPException(400, f"Is a directory: {e}")
    if isinstance(e, files_module.BinaryFileError):
        raise HTTPException(415, str(e))
    if isinstance(e, files_module.FileTooLargeError):
        raise HTTPException(413, str(e))
    if isinstance(e, files_module.ProtectedPathError):
        raise HTTPException(409, str(e))
    if isinstance(e, files_module.AlreadyExistsError):
        raise HTTPException(409, f"Already exists: {e}")
    raise HTTPException(500, str(e))


@authed.get("/servers/{server_id}/files")
def list_files(server_id: str, path: str = ""):
    get_server_or_404(server_id)
    try:
        entries = files_module.list_dir(manager.paths(server_id), path)
    except Exception as e:
        _files_error(server_id, e)
    return {"entries": [e.model_dump() for e in entries]}


@authed.get("/servers/{server_id}/files/content")
def read_file(server_id: str, path: str):
    get_server_or_404(server_id)
    try:
        return files_module.read_text_file(manager.paths(server_id), path)
    except Exception as e:
        _files_error(server_id, e)


class WriteFileRequest(BaseModel):
    path: str
    content: str


@authed.put("/servers/{server_id}/files/content")
def write_file(server_id: str, req: WriteFileRequest):
    get_server_or_404(server_id)
    try:
        return files_module.write_text_file(manager.paths(server_id), req.path, req.content)
    except Exception as e:
        _files_error(server_id, e)


class MkdirRequest(BaseModel):
    path: str


@authed.post("/servers/{server_id}/files/mkdir")
def make_dir(server_id: str, req: MkdirRequest):
    get_server_or_404(server_id)
    try:
        return files_module.mkdir(manager.paths(server_id), req.path)
    except Exception as e:
        _files_error(server_id, e)


class RenameFileRequest(BaseModel):
    path: str
    newPath: str


@authed.post("/servers/{server_id}/files/rename")
def rename_file(server_id: str, req: RenameFileRequest):
    get_server_or_404(server_id)
    try:
        return files_module.rename_path(manager.paths(server_id), req.path, req.newPath)
    except Exception as e:
        _files_error(server_id, e)


@authed.delete("/servers/{server_id}/files")
def delete_file(server_id: str, path: str):
    get_server_or_404(server_id)
    try:
        files_module.delete_path(manager.paths(server_id), path)
    except Exception as e:
        _files_error(server_id, e)
    return {"ok": True}


@authed.post("/servers/{server_id}/files/upload")
async def upload_file(server_id: str, path: str = Form(default=""), file: UploadFile = File(...)):
    get_server_or_404(server_id)
    content = await file.read()
    try:
        return files_module.save_uploaded_file(manager.paths(server_id), path, file.filename, content)
    except Exception as e:
        _files_error(server_id, e)


@authed.get("/servers/{server_id}/files/download")
def download_file(server_id: str, path: str):
    get_server_or_404(server_id)
    try:
        target = files_module.resolve_for_download(manager.paths(server_id), path)
    except Exception as e:
        _files_error(server_id, e)
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


# ---------- .mrpack (Modrinth modpack) install ----------

@authed.post("/servers/{server_id}/mrpack/install")
async def install_mrpack(server_id: str, file: UploadFile = File(...)):
    server = get_server_or_404(server_id)
    if server["loader"] == "pumpkin":
        raise HTTPException(409, "Pumpkin does not support Modrinth modpacks yet")
    if manager.state(server_id).value == "running":
        raise HTTPException(409, "Stop the server before installing a modpack")

    content = await file.read()
    try:
        result = await mrpack_installer.install_mrpack(
            manager.paths(server_id), content, server["loader"]
        )
    except mrpack_installer.MrpackError as e:
        raise HTTPException(400, str(e))
    return result.model_dump()


app.include_router(authed)
