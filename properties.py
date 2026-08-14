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
    current = read_properties(paths)
    current.update(updates)
    lines = [f"{k}={v}" for k, v in current.items()]
    paths.properties_file.write_text("\n".join(lines) + "\n")
    return current
