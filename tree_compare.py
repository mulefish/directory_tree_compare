"""Read-only directory tree comparison.

The scanner keeps relative paths as keys, does not follow directory symlinks,
and compares regular files in chunks so large files do not fill memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
import os
import stat


CHUNK_SIZE = 1024 * 1024
DEFAULT_IGNORE_FILE = Path(__file__).with_name("treecompareignore")
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def load_ignore_patterns(path: Path = DEFAULT_IGNORE_FILE) -> tuple[str, ...]:
    """Read one glob per line; blank lines and comments are ignored."""
    try:
        return tuple(line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
                     if line.strip() and not line.lstrip().startswith("#"))
    except FileNotFoundError:
        return ()


def is_ignored(relative_path: str, is_folder: bool, patterns: tuple[str, ...]) -> bool:
    """Match a basename anywhere, or a slash-containing path from the root."""
    name = relative_path.rsplit("/", 1)[-1].casefold()
    relative = relative_path.casefold()
    for raw_pattern in patterns:
        folder_only = raw_pattern.endswith("/")
        if folder_only and not is_folder:
            continue
        pattern = raw_pattern.strip("/").replace("\\", "/").casefold()
        if not pattern:
            continue
        if fnmatchcase(relative if "/" in pattern else name, pattern):
            return True
    return False


@dataclass(frozen=True)
class Entry:
    relative_path: str
    kind: str
    status: str
    left: Path | None
    right: Path | None
    detail: str = ""


@dataclass(frozen=True)
class _Node:
    path: Path
    kind: str
    size: int = 0
    link_target: str = ""


def _scan(root: Path, ignore_patterns: tuple[str, ...]) -> tuple[dict[str, _Node], dict[str, str]]:
    nodes: dict[str, _Node] = {}
    errors: dict[str, str] = {}

    def visit(folder: Path) -> None:
        try:
            with os.scandir(folder) as iterator:
                children = list(iterator)
        except OSError as exc:
            relative = folder.relative_to(root).as_posix() or "."
            errors[relative] = str(exc)
            return

        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            try:
                info = child.stat(follow_symlinks=False)
                mode = info.st_mode
                if is_ignored(relative, stat.S_ISDIR(mode), ignore_patterns):
                    continue
                if stat.S_ISLNK(mode):
                    nodes[relative] = _Node(path, "link", link_target=os.readlink(path))
                elif stat.S_ISDIR(mode):
                    nodes[relative] = _Node(path, "folder")
                    visit(path)
                elif stat.S_ISREG(mode):
                    nodes[relative] = _Node(path, "file", info.st_size)
                else:
                    nodes[relative] = _Node(path, "other")
            except OSError as exc:
                errors[relative] = str(exc)
    visit(root)
    return nodes, errors


def _files_equal(left: Path, right: Path) -> bool:
    with left.open("rb") as a, right.open("rb") as b:
        while True:
            x = a.read(CHUNK_SIZE)
            y = b.read(CHUNK_SIZE)
            if x != y:
                break
            if not x:
                return True
    left_kind = _text_kind(left)
    if left_kind is None or left_kind != _text_kind(right):
        return False
    try:
        a = _NormalizedReader(_normalized_chunks(left, left_kind))
        b = _NormalizedReader(_normalized_chunks(right, left_kind))
        while True:
            x, y = a.read(CHUNK_SIZE), b.read(CHUNK_SIZE)
            if x != y:
                return False
            if not x:
                return True
    except UnicodeError:
        return False


def _text_kind(path: Path) -> str | None:
    with path.open("rb") as stream:
        sample = stream.read(4096)
    if sample.startswith(UTF16_BOMS):
        return "utf16"
    if b"\x00" in sample or any(byte < 32 and byte not in (9, 10, 12, 13) for byte in sample):
        return None
    return "bytes"


def _normalized_chunks(path: Path, kind: str):
    """Normalize CRLF/CR to LF and drop one optional final line ending."""
    if kind == "utf16":
        with path.open("r", encoding="utf-16", newline=None) as stream:
            yield from _without_final_newline(iter(lambda: stream.read(CHUNK_SIZE), ""), "\n")
        return

    def chunks():
        pending_cr = b""
        with path.open("rb") as stream:
            while chunk := stream.read(CHUNK_SIZE):
                chunk = pending_cr + chunk
                pending_cr = b"\r" if chunk.endswith(b"\r") else b""
                if pending_cr:
                    chunk = chunk[:-1]
                yield chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            if pending_cr:
                yield b"\n"

    yield from _without_final_newline(chunks(), b"\n")


def _without_final_newline(chunks, newline):
    tail = newline[:0]
    for chunk in chunks:
        combined = tail + chunk
        if len(combined) > 1:
            yield combined[:-1]
            tail = combined[-1:]
        else:
            tail = combined
    if tail and tail != newline:
        yield tail


class _NormalizedReader:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.buffer = None
        self.done = False

    def read(self, size):
        while not self.done and (self.buffer is None or len(self.buffer) < size):
            try:
                chunk = next(self.chunks)
            except StopIteration:
                self.done = True
                break
            self.buffer = chunk if self.buffer is None else self.buffer + chunk
        if self.buffer is None:
            return b""
        output, self.buffer = self.buffer[:size], self.buffer[size:]
        return output


def compare_trees(left: Path | str, right: Path | str,
                  ignore_file: Path = DEFAULT_IGNORE_FILE) -> list[Entry]:
    """Return one entry per relative file or folder, including empty folders."""
    left = Path(left).expanduser().resolve()
    right = Path(right).expanduser().resolve()
    for side, root in (("Left", left), ("Right", right)):
        if not root.is_dir():
            raise ValueError(f"{side} folder does not exist or cannot be read: {root}")
    if left == right:
        raise ValueError("Choose two different folders.")

    ignore_patterns = load_ignore_patterns(ignore_file)
    left_nodes, left_errors = _scan(left, ignore_patterns)
    right_nodes, right_errors = _scan(right, ignore_patterns)
    result: dict[str, Entry] = {}
    all_paths = left_nodes.keys() | right_nodes.keys() | left_errors.keys() | right_errors.keys()
    for relative in sorted(all_paths, key=lambda p: (p.casefold(), p)):
        a = left_nodes.get(relative)
        b = right_nodes.get(relative)
        a_error = left_errors.get(relative)
        b_error = right_errors.get(relative)
        if a_error or b_error:
            result[relative] = Entry(relative, a.kind if a else b.kind if b else "unknown",
                                     "Error", a.path if a else None, b.path if b else None,
                                     "; ".join(f"{side}: {error}" for side, error in
                                               (("Left", a_error), ("Right", b_error)) if error))
        elif a is None or b is None:
            node = a or b
            result[relative] = Entry(relative, node.kind, "Left only" if a else "Right only",
                                     a.path if a else None, b.path if b else None)
        elif a.kind != b.kind:
            result[relative] = Entry(relative, "mixed", "Different type", a.path, b.path,
                                     f"Left: {a.kind}; right: {b.kind}")
        elif a.kind == "folder":
            result[relative] = Entry(relative, "folder", "Same", a.path, b.path)
        elif a.kind == "file":
            try:
                same = _files_equal(a.path, b.path)
                result[relative] = Entry(relative, "file", "Same" if same else "Different", a.path, b.path)
            except OSError as exc:
                result[relative] = Entry(relative, "file", "Error", a.path, b.path, str(exc))
        elif a.kind == "link":
            status = "Same" if a.link_target == b.link_target else "Different"
            result[relative] = Entry(relative, "link", status, a.path, b.path,
                                     f"Left: {a.link_target}; right: {b.link_target}")
        else:
            result[relative] = Entry(relative, "other", "Different type", a.path, b.path,
                                     "Unsupported file type")

    # An existing folder is different when any item beneath it differs.
    for relative in sorted(result, key=lambda p: p.count("/"), reverse=True):
        entry = result[relative]
        if entry.status == "Same":
            continue
        parent = Path(relative).parent.as_posix()
        while parent != ".":
            ancestor = result.get(parent)
            if ancestor is not None and ancestor.kind == "folder" and ancestor.status == "Same":
                result[parent] = Entry(parent, "folder", "Different", ancestor.left, ancestor.right)
            parent = Path(parent).parent.as_posix()
    return list(result.values())
