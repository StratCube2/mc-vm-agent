"""
File explorer + editor backend, scoped per server via ServerPaths.

Every path the frontend sends is a POSIX-style relative path *within*
a server's root directory (e.g. "mods/foo.jar", "world/level.dat",
"" / "." for root). All of them are resolved and validated through
_resolve() before touching disk, which is the single choke point that
prevents path traversal (../, absolute paths, symlink escapes) from
ever reaching outside paths.root.

Text editing (read_text_file/write_text_file) is UTF-8 only, matching
the Monaco editor on the frontend — binary files are rejected on read
with a clear error rather than being mangled through errors="replace",
and oversized files are rejected before ever being loaded into memory.
Binary files can still be downloaded as raw bytes (resolve_for_download)
even though they can't be opened in the text editor.
"""
import shutil
from pathlib import Path
from pydantic import BaseModel

from config import ServerPaths

# Above this, the file is treated as "binary/too large to edit" — the
# frontend's Monaco editor has no business loading a multi-hundred-MB
# world file into a browser tab, and this also bounds agent memory use
# per request.
MAX_TEXT_FILE_BYTES = 5 * 1024 * 1024  # 5 MiB

# Directories nobody should be able to rm -rf or rename via the file
# explorer — they're structural, not "files" in the user's mental model,
# and process_manager/loader_installer assume they exist.
PROTECTED_ROOT_NAMES = {"world", "mods", "plugins", "logs"}


class PathEscapeError(Exception):
    """Raised when a client-supplied path would resolve outside the
    server's root directory."""


class NotFoundError(Exception):
    pass


class NotADirectoryErr(Exception):
    pass


class IsADirectoryErr(Exception):
    pass


class BinaryFileError(Exception):
    pass


class FileTooLargeError(Exception):
    pass


class ProtectedPathError(Exception):
    pass


class AlreadyExistsError(Exception):
    pass


def _resolve(paths: ServerPaths, rel_path: str, must_exist: bool = True) -> Path:
    """Resolves a client-supplied relative path against the server
    root, raising PathEscapeError if it would land outside it. Uses
    Path.resolve()'s normalization so "../etc/passwd",
    "mods/../../other-server/meta.json", and absolute paths are all
    caught the same way — resolve() collapses ".." segments and
    symlinks before the containment check runs, so a symlink planted
    inside a server dir can't be used to escape it either."""
    rel_path = (rel_path or "").strip()
    # Treat "", ".", "/" all as root — the frontend's initial listing
    # and breadcrumb-to-root both send these interchangeably.
    if rel_path in ("", ".", "/"):
        rel_path = "."

    # A leading "/" would make Path(root, rel) ignore root entirely
    # (PurePath joins to an absolute component by discarding everything
    # before it) — strip it so every path is treated as root-relative.
    rel_path = rel_path.lstrip("/")

    root = paths.root.resolve()
    candidate = (root / rel_path).resolve()

    if candidate != root and root not in candidate.parents:
        raise PathEscapeError(f"Path escapes server root: {rel_path}")

    if must_exist and not candidate.exists():
        raise NotFoundError(rel_path)

    return candidate


def _rel(root: Path, p: Path) -> str:
    return str(p.relative_to(root)).replace("\\", "/")


class FileEntry(BaseModel):
    name: str
    path: str  # relative to server root, POSIX-style
    type: str  # "file" | "dir"
    sizeBytes: int | None = None
    modifiedAt: float | None = None


def list_dir(paths: ServerPaths, rel_path: str) -> list[FileEntry]:
    root = paths.root.resolve()
    target = _resolve(paths, rel_path)
    if not target.is_dir():
        raise NotADirectoryErr(rel_path)

    entries = []
    # Dirs first, then files, both alphabetical (case-insensitive) —
    # matches how most file explorers group entries.
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        try:
            st = child.stat()
        except OSError:
            # Broken symlink or a file that vanished mid-listing —
            # skip it rather than 500ing the whole directory listing.
            continue
        entries.append(
            FileEntry(
                name=child.name,
                path=_rel(root, child),
                type="dir" if child.is_dir() else "file",
                sizeBytes=None if child.is_dir() else st.st_size,
                modifiedAt=st.st_mtime,
            )
        )
    return entries


def _looks_binary(sample: bytes) -> bool:
    if b"\x00" in sample:
        return True
    # Cheap heuristic: try strict UTF-8 decoding on a sample; anything
    # that isn't valid UTF-8 is treated as binary/non-editable rather
    # than silently corrupted via a lossy decode.
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def read_text_file(paths: ServerPaths, rel_path: str) -> dict:
    target = _resolve(paths, rel_path)
    if target.is_dir():
        raise IsADirectoryErr(rel_path)

    size = target.stat().st_size
    if size > MAX_TEXT_FILE_BYTES:
        raise FileTooLargeError(
            f"{rel_path} is {size} bytes, over the {MAX_TEXT_FILE_BYTES}-byte "
            "editor limit"
        )

    raw = target.read_bytes()
    if _looks_binary(raw[:8192]):
        raise BinaryFileError(f"{rel_path} does not look like a UTF-8 text file")

    return {"path": rel_path, "content": raw.decode("utf-8"), "sizeBytes": size}


def write_text_file(paths: ServerPaths, rel_path: str, content: str) -> dict:
    # must_exist=False: writing a brand-new file (created from the
    # explorer's "New File" action, or just saving a file the editor
    # created client-side) is a legitimate call shape, not just editing
    # an existing one.
    target = _resolve(paths, rel_path, must_exist=False)
    if target.exists() and target.is_dir():
        raise IsADirectoryErr(rel_path)

    encoded = content.encode("utf-8")
    if len(encoded) > MAX_TEXT_FILE_BYTES:
        raise FileTooLargeError(
            f"Content is {len(encoded)} bytes, over the {MAX_TEXT_FILE_BYTES}-byte editor limit"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encoded)
    return {"path": rel_path, "sizeBytes": len(encoded)}


def mkdir(paths: ServerPaths, rel_path: str) -> dict:
    target = _resolve(paths, rel_path, must_exist=False)
    if target.exists():
        raise AlreadyExistsError(rel_path)
    target.mkdir(parents=True, exist_ok=False)
    return {"path": rel_path}


def _check_not_protected(paths: ServerPaths, target: Path):
    root = paths.root.resolve()
    if target == root:
        raise ProtectedPathError("Cannot delete or rename the server root")
    if target.parent == root and target.name in PROTECTED_ROOT_NAMES:
        raise ProtectedPathError(
            f"'{target.name}' is a structural directory and can't be deleted or "
            "renamed from the file explorer"
        )


def delete_path(paths: ServerPaths, rel_path: str) -> None:
    target = _resolve(paths, rel_path)
    _check_not_protected(paths, target)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def rename_path(paths: ServerPaths, rel_path: str, new_rel_path: str) -> dict:
    src = _resolve(paths, rel_path)
    _check_not_protected(paths, src)
    dest = _resolve(paths, new_rel_path, must_exist=False)
    if dest.exists():
        raise AlreadyExistsError(new_rel_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)
    root = paths.root.resolve()
    return {"path": _rel(root, dest)}


def save_uploaded_file(paths: ServerPaths, rel_dir: str, filename: str, content: bytes) -> dict:
    """Uploads a raw file into rel_dir (an existing or new directory
    within the server root). filename is basename-only — any directory
    component the client sent is stripped so this can't be used to
    escape rel_dir the way a raw path could."""
    safe_name = Path(filename).name
    if not safe_name:
        raise ValueError("Missing filename")
    dir_target = _resolve(paths, rel_dir, must_exist=False)
    if dir_target.exists() and not dir_target.is_dir():
        raise NotADirectoryErr(rel_dir)
    dir_target.mkdir(parents=True, exist_ok=True)
    dest = dir_target / safe_name
    dest.write_bytes(content)
    root = paths.root.resolve()
    return {"path": _rel(root, dest), "sizeBytes": len(content)}


def resolve_for_download(paths: ServerPaths, rel_path: str) -> Path:
    target = _resolve(paths, rel_path)
    if target.is_dir():
        raise IsADirectoryErr(rel_path)
    return target
