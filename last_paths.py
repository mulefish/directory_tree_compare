"""Per-user storage for the last pair of compared folders."""

from __future__ import annotations

import json
import os
from pathlib import Path


def default_storage_file() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "TreeCompare" / "last_paths.json"
    return Path.home() / ".tree_compare" / "last_paths.json"


def save_last_paths(left: str, right: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"left": left, "right": right}, indent=2), encoding="utf-8")


def load_last_paths(path: Path) -> tuple[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    left, right = data.get("left"), data.get("right")
    if not isinstance(left, str) or not isinstance(right, str) or not left or not right:
        return None
    return left, right
