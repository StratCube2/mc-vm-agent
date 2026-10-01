"""
Minimal server.properties parser/writer, scoped per server via
ServerPaths. Keeps unknown keys intact and preserves ordering so we
don't clobber anything a loader/mod adds.
"""
from config import ServerPaths

# The subset we expose as "simple mode" fields in the UI — everything else
# is still editable via the raw/advanced editor.
SIMPLE_KEYS = {
    "motd": "A Minecraft Server",
    "difficulty": "normal",
    "gamemode": "survival",
    "max-players": "20",
    "pvp": "true",
    "white-list": "false",
    "online-mode": "true",
    "view-distance": "10",
}


def read_properties(paths: ServerPaths) -> dict[str, str]:
    if not paths.properties_file.exists():
        return dict(SIMPLE_KEYS)
    props = {}
    for line in paths.properties_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        props[k.strip()] = v.strip()
    return props


def write_properties(paths: ServerPaths, updates: dict[str, str]) -> dict[str, str]:
    paths.ensure_dirs()
    if paths.properties_file.exists():
        current = read_properties(paths)
    else:
        # File doesn't exist yet (server never booted). Write ONLY the
        # requested keys rather than materializing the whole SIMPLE_KEYS
        # defaults block — Minecraft fills in every unset key with its own
        # default on first boot anyway, and pre-writing them would pin
        # this panel's guesses (e.g. difficulty) over MC's actual defaults.
        current = {}
    current.update(updates)
    lines = [f"{k}={v}" for k, v in current.items()]
    paths.properties_file.write_text("\n".join(lines) + "\n")
    return current


def remove_property(paths: ServerPaths, key: str) -> None:
    """Drops a single key from server.properties (used to clear
    level-seed so Minecraft generates a fresh random seed). No-op when
    the file or key doesn't exist."""
    if not paths.properties_file.exists():
        return
    current = read_properties(paths)
    if key not in current:
        return
    del current[key]
    lines = [f"{k}={v}" for k, v in current.items()]
    paths.properties_file.write_text("\n".join(lines) + "\n")
