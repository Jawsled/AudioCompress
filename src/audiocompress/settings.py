"""Persist user settings cross-platform (stdlib only).

Stored as JSON in ``~/.audiocompress/config.json`` — ``Path.home()`` works on
Windows, macOS and Linux, so no platform-specific code or dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def config_path() -> Path:
    return Path.home() / ".audiocompress" / "config.json"


def load() -> dict[str, Any]:
    """Return saved settings (empty dict if none yet). Never raises."""
    try:
        raw = config_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into saved settings and write back. Never raises."""
    try:
        data = load()
        data.update({k: v for k, v in patch.items() if v is not None})
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        return data
    except OSError:
        return load()


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)
