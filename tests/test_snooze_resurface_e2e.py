"""End-to-end snooze resurfacing tests against the real SASE notification store.

Unlike the rest of the outbound tests, these drive the actual JSONL store so the
delivery cursor, the store's atomic expiry, and Telegram's eligibility rules are
verified together. Elapsed wall-clock time is simulated by moving persisted
deadlines earlier, which is equivalent to advancing "now" without sleeping.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from sase.notifications import store as notification_store
from sase.notifications.models import Notification
from sase_telegram.outbound import get_unsent_notifications, mark_sent

# The current-state read and bulk snooze mutation ship with the SASE release
# that introduced canonical snooze expiry. Until the installed `sase` provides
# them, outbound still works through its documented compatibility fallbacks and
# the mocked unit tests in `test_outbound.py` cover the cursor rules.
pytestmark = pytest.mark.skipif(
    not hasattr(notification_store, "read_current_notification_snapshot")
    or not hasattr(notification_store, "mark_many_snoozed"),
    reason="installed sase predates the canonical snooze-expiry store API",
)


@pytest.fixture()
def store_and_cursor(tmp_path: Path) -> Iterator[Path]:
    """Point the notification store and the delivery cursor at ``tmp_path``."""
    notifications_dir = tmp_path / "notifications"
    cursor_file = tmp_path / "telegram" / "last_sent_ts"
    with (
        patch.object(notification_store, "NOTIFICATIONS_DIR", str(notifications_dir)),
        patch.object(
            notification_store,
            "NOTIFICATIONS_FILE",
            str(notifications_dir / "notifications.jsonl"),
        ),
        patch("sase_telegram.outbound.LAST_SENT_FILE", cursor_file),
    ):
        yield cursor_file


def _append(notification_id: str, *, minutes_ago: float = 0.0) -> Notification:
    notification = Notification(
        id=notification_id,
        timestamp=(datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(),
        sender="test",
        notes=[f"note {notification_id}"],
    )
    notification_store.append_notification(notification)
    return notification


def _advance_clock(seconds: float) -> None:
    rows = notification_store.load_notifications(include_dismissed=True)
    for row in rows:
        if row.snooze_until:
            deadline = datetime.fromisoformat(row.snooze_until)
            row.snooze_until = (
                (deadline - timedelta(seconds=seconds)).astimezone(UTC).isoformat()
            )
    notification_store.rewrite_notifications(rows)


def _expire_due_snoozes() -> list[str]:
    return list(notification_store.read_current_notification_snapshot().expired_ids)


def _future(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _initialize_cursor(cursor_file: Path) -> None:
    """First run suppresses the backlog and writes a versioned cursor."""
    assert get_unsent_notifications() == []
    assert json.loads(cursor_file.read_text(encoding="utf-8"))["version"] == 2


def test_snoozed_before_first_delivery_is_delivered_once_after_resurfacing(
    store_and_cursor: Path,
) -> None:
    _initialize_cursor(store_and_cursor)

    notification = _append(str(uuid.uuid4()))
    assert notification_store.mark_snoozed(notification.id, _future(600)) is True

    # A still-muted snooze is never delivered, even though it is unread and new.
    assert get_unsent_notifications() == []

    _advance_clock(601)
    assert _expire_due_snoozes() == [notification.id]

    unsent = get_unsent_notifications()
    assert [row.id for row in unsent] == [notification.id]
    # The original creation time survives the resurface.
    assert unsent[0].timestamp == notification.timestamp
    assert unsent[0].resurfaced_at is not None

    mark_sent(unsent)
    assert get_unsent_notifications() == []


def test_previously_delivered_row_crosses_the_migrated_cursor_once(
    store_and_cursor: Path,
) -> None:
    _initialize_cursor(store_and_cursor)

    notification = _append(str(uuid.uuid4()))
    delivered = get_unsent_notifications()
    assert [row.id for row in delivered] == [notification.id]
    mark_sent(delivered)
    assert get_unsent_notifications() == []

    # Downgrade to a legacy timestamp-only marker to exercise the migration.
    store_and_cursor.write_text(str(datetime.now(UTC).timestamp()), encoding="utf-8")

    assert notification_store.mark_snoozed(notification.id, _future(600)) is True
    _advance_clock(601)
    assert _expire_due_snoozes() == [notification.id]

    resurfaced = get_unsent_notifications()
    assert [row.id for row in resurfaced] == [notification.id]
    payload = json.loads(store_and_cursor.read_text(encoding="utf-8"))
    assert payload["version"] == 2

    mark_sent(resurfaced)
    assert get_unsent_notifications() == []


def test_dismissed_and_unmuted_snoozes_never_produce_a_new_generation(
    store_and_cursor: Path,
) -> None:
    _initialize_cursor(store_and_cursor)

    dismissed = _append(str(uuid.uuid4()))
    unmuted = _append(str(uuid.uuid4()))
    for notification in (dismissed, unmuted):
        assert notification_store.mark_snoozed(notification.id, _future(600)) is True
    assert notification_store.mark_dismissed(dismissed.id) is True
    assert notification_store.mark_muted(unmuted.id, False) is True

    _advance_clock(601)
    assert _expire_due_snoozes() == []

    # The explicitly unmuted row is deliverable as its original generation only.
    delivered = get_unsent_notifications()
    assert unmuted.id in {row.id for row in delivered}
    mark_sent(delivered)
    assert get_unsent_notifications() == []


def test_simultaneous_resurface_events_are_each_delivered_oldest_first(
    store_and_cursor: Path,
) -> None:
    _initialize_cursor(store_and_cursor)

    first = _append("aaaa1111-0000-0000-0000-000000000000", minutes_ago=60)
    second = _append("bbbb2222-0000-0000-0000-000000000000", minutes_ago=30)
    assert (
        notification_store.mark_many_snoozed([first.id, second.id], _future(600)) == 2
    )
    _advance_clock(601)
    assert sorted(_expire_due_snoozes()) == sorted([first.id, second.id])

    unsent = get_unsent_notifications()
    assert [row.id for row in unsent] == [first.id, second.id]

    # A failed send must not advance the cursor past the undelivered sibling.
    mark_sent([unsent[0]])
    assert [row.id for row in get_unsent_notifications()] == [second.id]

    mark_sent([unsent[1]])
    assert get_unsent_notifications() == []
