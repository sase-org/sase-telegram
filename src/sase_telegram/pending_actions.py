"""Persist and manage pending actions awaiting user response via Telegram."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

PENDING_ACTIONS_PATH = Path.home() / ".sase" / "telegram" / "pending_actions.json"
STALE_THRESHOLD_SECONDS = 24 * 60 * 60  # 24 hours


def _load() -> dict[str, Any]:
    """Load pending actions from disk."""
    if not PENDING_ACTIONS_PATH.exists():
        return {}
    with open(PENDING_ACTIONS_PATH) as f:
        return json.load(f)


def _save(data: dict[str, Any]) -> None:
    """Atomically write pending actions to disk."""
    PENDING_ACTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=PENDING_ACTIONS_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, PENDING_ACTIONS_PATH)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def add(action_id: str, action_data: dict[str, Any]) -> None:
    """Add a pending action."""
    data = _load()
    action_data["created_at"] = time.time()
    data[action_id] = action_data
    _save(data)


def get(action_id: str) -> dict[str, Any] | None:
    """Get a pending action by ID, or None if not found."""
    data = _load()
    return data.get(action_id)


def remove(action_id: str) -> bool:
    """Remove a pending action. Returns True if it existed."""
    data = _load()
    if action_id not in data:
        return False
    del data[action_id]
    _save(data)
    return True


def list_all() -> dict[str, Any]:
    """Return all pending actions."""
    return _load()


def cleanup_stale() -> list[str]:
    """Remove pending actions older than 24 hours. Returns removed IDs."""
    data = _load()
    now = time.time()
    stale_ids = [
        aid
        for aid, adata in data.items()
        if now - adata.get("created_at", 0) > STALE_THRESHOLD_SECONDS
    ]
    for aid in stale_ids:
        del data[aid]
    if stale_ids:
        _save(data)
    return stale_ids
