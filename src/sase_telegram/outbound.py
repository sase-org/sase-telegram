"""Core outbound logic: detect inactivity, load unsent notifications, track sent."""

from __future__ import annotations

import time
from pathlib import Path

from sase.ace.tui_activity import get_tui_last_activity
from sase.notifications.models import Notification
from sase.notifications.store import load_notifications

LAST_SENT_FILE = Path.home() / ".sase" / "telegram" / "last_sent_ts"


def get_unsent_notifications() -> list[Notification]:
    """Return notifications that haven't been sent to Telegram yet.

    Uses a high-water mark timestamp file to track what's already been sent.
    On first run (no file), initializes the file to now and returns empty
    to avoid dumping backlog.
    """
    if not LAST_SENT_FILE.exists():
        # First run — initialize high-water mark, don't dump backlog
        _write_high_water_mark(time.time())
        return []

    last_sent_ts = float(LAST_SENT_FILE.read_text().strip())

    # Advance the high-water mark to the TUI's last activity time so
    # notifications the user already saw during active TUI use are not
    # re-sent via Telegram when the user later becomes idle.
    # epoch=0 (manual idle via I key) is excluded so accumulated
    # notifications are still delivered.
    activity_ts = get_tui_last_activity()
    if activity_ts is not None and activity_ts > 0 and activity_ts > last_sent_ts:
        last_sent_ts = activity_ts
        _write_high_water_mark(activity_ts)

    all_notifs = load_notifications()
    unsent = []
    for n in all_notifs:
        if n.read or n.dismissed:
            continue
        from datetime import datetime

        try:
            ts = datetime.fromisoformat(n.timestamp).timestamp()
        except ValueError:
            continue
        if ts > last_sent_ts:
            unsent.append(n)
    return unsent


def mark_sent(notifications: list[Notification]) -> None:
    """Update the high-water mark to the latest notification timestamp."""
    if not notifications:
        return
    from datetime import datetime

    latest = max(datetime.fromisoformat(n.timestamp).timestamp() for n in notifications)
    _write_high_water_mark(latest)


def _write_high_water_mark(ts: float) -> None:
    """Write a timestamp to the high-water mark file."""
    LAST_SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_SENT_FILE.write_text(str(ts))
