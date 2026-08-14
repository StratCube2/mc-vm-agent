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
from pydantic import BaseModel

from auth import require_agent_token
from config import DEFAULT_XMX, DEFAULT_XMS, MAX_SERVER_SLOTS, MAX_CONCURRENT_SERVERS
from process_manager import manager, ConcurrencyLimitError
import servers as servers_module
from servers import SlotLimitError, ServerNotFoundError
import loader_installer
import mods as mods_module
import properties as props_module

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
    mc_version: str


@authed.get("/servers")
def list_servers():
    return {"servers": servers_module.list_servers()}


@authed.post("/servers")
def create_server(req: CreateServerRequest):
    try:
        meta = servers_module.create_server(req.name, req.loader, req.mc_version)
    except SlotLimitError as e:
        raise HTTPException(409, str(e))
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

    if req.loader == "fabric":
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
    else:
        raise HTTPException(400, f"Unknown loader: {req.loader}")

    servers_module.update_server_meta(server_id, loader=req.loader, mc_version=req.mc_version)
    return {"ok": True, "loader": req.loader}


# ---------- Mods ----------

@authed.get("/servers/{server_id}/mods")
def list_mods(server_id: str):
    get_server_or_404(server_id)
    return {"mods": mods_module.list_installed_mods(manager.paths(server_id))}


@authed.get("/servers/{server_id}/mods/search")
async def search_mods(server_id: str, q: str, mc_version: str | None = None, loader: str | None = None):
    get_server_or_404(server_id)
    results = await mods_module.search_modrinth(q, mc_version, loader)
    return {"results": [r.model_dump() for r in results]}


class ModInstallRequest(BaseModel):
    project_id: str
    mc_version: str
    loader: str


@authed.post("/servers/{server_id}/mods/install")
async def install_mod(server_id: str, req: ModInstallRequest):
    get_server_or_404(server_id)
    try:
        filename = await mods_module.install_from_modrinth(
            manager.paths(server_id), req.project_id, req.mc_version, req.loader
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "filename": filename}


@authed.post("/servers/{server_id}/mods/upload")
async def upload_mod(server_id: str, file: UploadFile = File(...)):
    get_server_or_404(server_id)
    content = await file.read()
    try:
        filename = mods_module.save_uploaded_jar(manager.paths(server_id), file.filename, content)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "filename": filename}


@authed.delete("/servers/{server_id}/mods/{filename}")
def delete_mod(server_id: str, filename: str):
    get_server_or_404(server_id)
    if mods_module.remove_mod(manager.paths(server_id), filename):
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


app.include_router(authed)
