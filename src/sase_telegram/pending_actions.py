"""Manage Telegram pending actions through the shared host store."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from sase.notifications.pending_actions import cleanup_transport_actions
from sase.notifications.pending_actions import get_transport_action
from sase.notifications.pending_actions import list_transport_actions
from sase.notifications.pending_actions import remove_transport_action
from sase.notifications.pending_actions import upsert_transport_action

PENDING_ACTIONS_PATH: Path | str | None = None
STALE_THRESHOLD_SECONDS = 24 * 60 * 60
_TRANSPORT = "telegram"


def add(action_id: str, action_data: dict[str, Any]) -> None:
    """Add or replace a pending Telegram action."""
    created_at = time.time()
    action_data.setdefault("created_at", created_at)
    upsert_transport_action(
        action_id,
        action_data,
        transport=_TRANSPORT,
        path=PENDING_ACTIONS_PATH,
        now=created_at,
    )


def get(action_id: str) -> dict[str, Any] | None:
    """Get a pending Telegram action by ID, or None if not found."""
    return get_transport_action(
        action_id,
        transport=_TRANSPORT,
        path=PENDING_ACTIONS_PATH,
        include_legacy=True,
    )


def remove(action_id: str) -> bool:
    """Remove a pending Telegram action. Returns True if it existed."""
    return bool(
        remove_transport_action(
            action_id,
            transport=_TRANSPORT,
            path=PENDING_ACTIONS_PATH,
        )
    )


def list_all() -> dict[str, Any]:
    """Return all pending Telegram actions."""
    return list_transport_actions(
        transport=_TRANSPORT,
        path=PENDING_ACTIONS_PATH,
        include_legacy=True,
    )


def cleanup_stale() -> list[str]:
    """Remove Telegram actions older than 24 hours. Returns removed IDs."""
    return cleanup_transport_actions(
        transport=_TRANSPORT,
        path=PENDING_ACTIONS_PATH,
    )
