"""Core outbound logic: load unsent notifications and track sent ones."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sase.notifications import store as notification_store
from sase.notifications.models import Notification

LAST_SENT_FILE = Path.home() / ".sase" / "telegram" / "last_sent_ts"
OUTBOUND_LOCK_FILE = Path.home() / ".sase" / "telegram" / "outbound.lock"
_CURSOR_VERSION = 2
_LEGACY_EQUAL_TIMESTAMP_ID = "\U0010ffff"


@dataclass(frozen=True, order=True)
class _DeliveryCursor:
    activity_at: datetime
    notification_id: str


def get_unsent_notifications() -> list[Notification]:
    """Return notifications that haven't been sent to Telegram yet.

    Uses a versioned ``(activity_at, id)`` high-water cursor to track what's
    already been sent.
    The high-water mark is only advanced by ``mark_sent()`` after a
    notification is actually delivered to Telegram.  We deliberately do
    NOT advance it based on anything other than successful delivery,
    because doing so can silently drop notifications when the outbound
    chop was offline.  The ``n.read`` filter suppresses notifications
    the user has already read in the TUI.  We intentionally do NOT
    filter on ``n.dismissed`` — TUI agent-dismissal is a UI cleanup
    action, not a notification-read signal.

    On first run (no file), initializes the file to now and returns empty
    to avoid dumping backlog.
    """
    if not LAST_SENT_FILE.exists():
        # First run — initialize high-water mark, don't dump backlog
        _write_high_water_mark(
            _DeliveryCursor(datetime.now(UTC), _LEGACY_EQUAL_TIMESTAMP_ID)
        )
        return []

    last_sent = _read_high_water_mark()

    snapshot = _read_current_notification_snapshot()
    all_notifs = getattr(snapshot, "notifications", snapshot)
    unsent = []
    for n in all_notifs:
        if n.read or n.silent or n.muted:
            continue
        try:
            cursor = _notification_cursor(n)
        except ValueError:
            continue
        if cursor > last_sent:
            unsent.append(n)
    return sorted(unsent, key=_notification_cursor)


def mark_sent(notifications: list[Notification]) -> None:
    """Update the high-water mark to the latest delivered activity cursor."""
    if not notifications:
        return
    latest = max(_notification_cursor(notification) for notification in notifications)
    _write_high_water_mark(latest)


def _notification_cursor(notification: Notification) -> _DeliveryCursor:
    value = getattr(notification, "resurfaced_at", None) or notification.timestamp
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("notification activity timestamp must include a timezone")
    return _DeliveryCursor(parsed.astimezone(UTC), notification.id)


def _read_current_notification_snapshot() -> Any:
    current_reader = getattr(
        notification_store, "read_current_notification_snapshot", None
    )
    if current_reader is not None:
        return current_reader(include_dismissed=True)
    return notification_store.read_notification_snapshot(
        include_dismissed=True,
        expire_due_snoozes=True,
    )


def _read_high_water_mark() -> _DeliveryCursor:
    raw = LAST_SENT_FILE.read_text(encoding="utf-8").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        if payload.get("version") != _CURSOR_VERSION:
            raise ValueError("unsupported Telegram notification cursor version")
        activity_at = payload.get("activity_at")
        notification_id = payload.get("id")
        if not isinstance(activity_at, str) or not isinstance(notification_id, str):
            raise ValueError("malformed Telegram notification cursor")
        parsed = datetime.fromisoformat(activity_at)
        if parsed.tzinfo is None:
            raise ValueError("Telegram notification cursor must include a timezone")
        return _DeliveryCursor(parsed.astimezone(UTC), notification_id)

    # Legacy files contain one epoch timestamp. Give them a maximal ID so the
    # old strict timestamp comparison remains intact, then migrate atomically.
    legacy = _DeliveryCursor(
        datetime.fromtimestamp(float(raw), tz=UTC),
        _LEGACY_EQUAL_TIMESTAMP_ID,
    )
    _write_high_water_mark(legacy)
    return legacy


def _write_high_water_mark(cursor: _DeliveryCursor) -> None:
    """Atomically write a versioned activity cursor to the marker file."""
    LAST_SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=LAST_SENT_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": _CURSOR_VERSION,
                    "activity_at": cursor.activity_at.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "id": cursor.notification_id,
                },
                f,
                separators=(",", ":"),
            )
            f.write("\n")
        os.replace(tmp_path, LAST_SENT_FILE)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def try_acquire_outbound_lock() -> int | None:
    """Try to acquire an exclusive lock for the outbound process.

    Returns a file descriptor on success, or None if another instance holds
    the lock.  The caller must call :func:`release_outbound_lock` when done.
    """
    import fcntl

    OUTBOUND_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(OUTBOUND_LOCK_FILE), os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_outbound_lock(fd: int) -> None:
    """Release the outbound lock acquired by :func:`try_acquire_outbound_lock`."""
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
