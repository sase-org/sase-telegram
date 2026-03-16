"""Core outbound logic: detect inactivity, load unsent notifications, track sent."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from sase.ace.tui_activity import get_tui_last_activity
from sase.notifications.models import Notification
from sase.notifications.store import load_notifications

LAST_SENT_FILE = Path.home() / ".sase" / "telegram" / "last_sent_ts"
OUTBOUND_LOCK_FILE = Path.home() / ".sase" / "telegram" / "outbound.lock"


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
    """Atomically write a timestamp to the high-water mark file."""
    LAST_SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=LAST_SENT_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(str(ts))
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
