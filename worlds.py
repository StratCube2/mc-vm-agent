"""
World/dimension reset for a single server.

Java Edition layout: every dimension lives under ONE level directory
(server.properties `level-name`, default "world"):
    <root>/world/               <- overworld data (region/, entities/, ...)
    <root>/world/DIM-1/         <- nether
    <root>/world/DIM1/          <- end

So a selective reset isn't just "delete a folder":
- nether   -> delete <level>/DIM-1
- end      -> delete <level>/DIM1
- overworld-> wipe <level> but PRESERVE DIM-1/DIM1 if they're not also being
  reset (temporarily moved aside, level removed, restored after).

The new seed goes into server.properties `level-seed`: with the old level
directory gone, the server regenerates the world(s) from that seed on next
start (or a brand-new random seed if the key is cleared). Nothing here
runs silently — callers must have confirmed, and this module refuses to
touch anything while a process is up.
"""
import re
import shutil

from config import ServerPaths
from process_manager import manager, ServerState
import properties as props_module

# level-name comes from server.properties (user-editable) — never let it
# escape the server's own directory.
_SAFE_LEVEL_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

VALID_DIMENSIONS = ("overworld", "nether", "end")
_DIMENSION_DIRS = {"nether": "DIM-1", "end": "DIM1"}


class WorldResetError(Exception):
    pass


def _level_dir(paths: ServerPaths):
    name = props_module.read_properties(paths).get("level-name", "world")
    if not _SAFE_LEVEL_NAME.match(name):
        name = "world"
    return paths.root / name


def get_world_info(server_id: str) -> dict:
    """Current world layout + seed, for prefilling the reset panel."""
    paths = manager.paths(server_id)
    level = _level_dir(paths)
    props = props_module.read_properties(paths)
    return {
        "levelName": level.name,
        "dimensions": {
            "overworld": level.is_dir(),
            "nether": (level / "DIM-1").is_dir(),
            "end": (level / "DIM1").is_dir(),
        },
        "seed": props.get("level-seed") or None,
    }


def _require_stopped(server_id: str):
    state = manager.state(server_id)
    if state not in (ServerState.STOPPED, ServerState.CRASHED):
        raise WorldResetError(
            "Stop the server before resetting worlds"
        )


def reset_worlds(server_id: str, dimensions: list[str], seed: str | None) -> dict:
    """Deletes the selected dimension data and applies the seed for the
    next start. Returns what was actually done."""
    _require_stopped(server_id)
    if not dimensions:
        raise ValueError("Select at least one dimension to reset")
    invalid = [d for d in dimensions if d not in VALID_DIMENSIONS]
    if invalid:
        raise ValueError(f"Unknown dimensions: {', '.join(invalid)}")
    if len(dimensions) != len(set(dimensions)):
        raise ValueError("Duplicate dimensions in request")
    if seed is not None:
        seed = seed.strip()
        if len(seed) > 64:
            raise ValueError("Seed too long (max 64 characters)")

    paths = manager.paths(server_id)
    level = _level_dir(paths)
    deleted: list[str] = []

    # --- Nether / End: plain subdirectory deletions -------------------
    for dim in ("nether", "end"):
        if dim not in dimensions:
            continue
        dim_dir = level / _DIMENSION_DIRS[dim]
        if dim_dir.is_dir():
            shutil.rmtree(dim_dir, ignore_errors=True)
        deleted.append(dim)

    # --- Overworld: wipe the level dir, keeping unselected dimensions --
    if "overworld" in dimensions:
        keep = {_DIMENSION_DIRS[d] for d in ("nether", "end") if d not in dimensions}
        if level.is_dir():
            kept_backups: dict[str, any] = {}
            try:
                # Move survivors out first — rmtree would take them with it.
                for name in keep:
                    src = level / name
                    if src.is_dir():
                        dst = paths.root / f".keep_{name}"
                        shutil.move(str(src), str(dst))
                        kept_backups[name] = dst
                shutil.rmtree(level, ignore_errors=True)
                level.mkdir(parents=True, exist_ok=True)
                for name, dst in kept_backups.items():
                    shutil.move(str(dst), str(level / name))
            except Exception:
                # Best effort to restore survivors even on failure
                for name, dst in (
                    (n, paths.root / f".keep_{n}") for n in keep
                ):
                    if dst.is_dir():
                        target = level / n
                        if not target.exists():
                            shutil.move(str(dst), str(target))
                raise
        else:
            level.mkdir(parents=True, exist_ok=True)
        deleted.append("overworld")

    # --- Seed for the regenerated world --------------------------------
    # Only touch level-seed when the Overworld is being regenerated.
    # Resetting just Nether/End leaves the Overworld (and its seed) intact.
    if "overworld" in dimensions:
        _apply_seed(paths, seed)

    return {
        "ok": True,
        "deletedDimensions": deleted,
        "seed": seed or None,
        "message": (
            "Worlds deleted. New world(s) will be generated "
            f"{'with seed ' + seed if seed else 'with a random seed'} on next start."
        ),
    }


def _apply_seed(paths: ServerPaths, seed: str | None):
    if seed:
        props_module.write_properties(paths, {"level-seed": seed})
        return
    # Clearing the key lets Minecraft pick a fresh random seed itself.
    props_module.remove_property(paths, "level-seed")
