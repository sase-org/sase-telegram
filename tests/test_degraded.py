"""Tests for persisted Telegram degraded-state tracking."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from sase_telegram import degraded
from sase_telegram.credentials import TelegramCredentialError
from sase_telegram.degraded import (
    CREDENTIAL_UNAVAILABLE,
    NETWORK_DEGRADED,
    OUTBOUND,
    RECEIVER,
    SUMMARY_WINDOW_SECONDS,
    DegradedTracker,
    classify_failure,
    format_duration,
    notice_dedup_key,
    skip_outbound_delivery,
)

# Captured at import, before the autouse conftest fixture swaps in recorders.
_REAL_RAISE_NOTICE = degraded._raise_notice
_REAL_SETTLE_NOTICE = degraded._settle_notice

_T0 = 1_763_000_000.0


def _tracker_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == degraded.__name__]


class TestClassifyFailure:
    @pytest.mark.parametrize(
        ("exc", "state"),
        [
            (TelegramCredentialError("no token"), CREDENTIAL_UNAVAILABLE),
            (TimedOut(), NETWORK_DEGRADED),
            (NetworkError("Bad Gateway"), NETWORK_DEGRADED),
            (NetworkError("Service Unavailable (503)"), NETWORK_DEGRADED),
        ],
    )
    def test_known_outages(self, exc: BaseException, state: str) -> None:
        assert classify_failure(exc) == state

    @pytest.mark.parametrize(
        "exc",
        [
            BadRequest("Can't parse entities"),
            BadRequest("Chat not found"),
            Forbidden("bot was blocked by the user"),
            RetryAfter(timedelta(seconds=5)),
            RuntimeError("bug"),
        ],
    )
    def test_everything_else_is_unexpected(self, exc: BaseException) -> None:
        # BadRequest subclasses NetworkError but is not a network outage.
        assert classify_failure(exc) is None


class TestDegradedTracker:
    def test_many_credential_failures_raise_one_notice_and_bounded_logs(
        self,
        degraded_notices: list[tuple[str, ...]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        exc = TelegramCredentialError("gpg: public key decryption failed")
        hours = 2
        ticks = hours * 3600 // 5  # the outbound job runs every 5 s
        with caplog.at_level(logging.INFO, logger=degraded.__name__):
            transitions = [
                # A fresh tracker per tick, like a fresh outbound process.
                DegradedTracker(OUTBOUND).record_failure(
                    CREDENTIAL_UNAVAILABLE, exc, now=_T0 + 5 * tick
                )
                for tick in range(ticks)
            ]

        assert transitions[0] == "entered"
        assert [c[0] for c in degraded_notices] == ["raise"]
        records = _tracker_records(caplog)
        # One entry line plus at most one summary per 15-minute window.
        assert len(records) <= 1 + hours * 3600 // SUMMARY_WINDOW_SECONDS
        assert transitions.count("summarized") == len(records) - 1
        assert all(r.exc_info is None for r in records)
        assert "entered credential_unavailable" in records[0].getMessage()
        assert "gpg: public key decryption failed" in records[0].getMessage()
        summary = records[1].getMessage()
        assert "still credential_unavailable after 15m:" in summary
        assert "180 failures since the last report" in summary

        record = DegradedTracker(OUTBOUND).current()
        assert record is not None
        assert record.count == ticks
        assert record.entered_at == _T0
        assert record.last_seen == _T0 + 5 * (ticks - 1)

    def test_state_file_records_state_entered_at_last_seen_and_count(self) -> None:
        tracker = DegradedTracker(RECEIVER)
        tracker.record_failure(NETWORK_DEGRADED, TimedOut(), now=_T0)
        tracker.record_failure(NETWORK_DEGRADED, TimedOut(), now=_T0 + 30)

        payload = json.loads(tracker.path.read_text(encoding="utf-8"))
        assert payload["component"] == RECEIVER
        assert payload["state"] == NETWORK_DEGRADED
        assert payload["count"] == 2
        assert payload["entered_at"].startswith("2025-11-13T")
        assert payload["last_seen"] != payload["entered_at"]
        assert payload["notice_id"] == "notice-0"

    def test_recovery_logs_once_and_settles_the_notice(
        self,
        degraded_notices: list[tuple[str, ...]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tracker = DegradedTracker(OUTBOUND)
        for tick in range(10):
            tracker.record_failure(NETWORK_DEGRADED, TimedOut(), now=_T0 + 60 * tick)
        caplog.clear()

        with caplog.at_level(logging.INFO, logger=degraded.__name__):
            cleared = tracker.record_success(now=_T0 + 750)
            assert tracker.record_success(now=_T0 + 755) is None

        assert cleared is not None and cleared.count == 10
        records = _tracker_records(caplog)
        assert [r.levelno for r in records] == [logging.INFO]
        assert records[0].getMessage() == (
            "Telegram outbound recovered from network_degraded after 12m 30s "
            "(10 failures)"
        )
        assert degraded_notices[-1] == (
            "settle",
            "notice-0",
            "recovered after 12m 30s (10 failures)",
        )
        assert tracker.current() is None
        assert not tracker.path.exists()

    def test_success_when_healthy_is_silent(
        self,
        degraded_notices: list[tuple[str, ...]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=degraded.__name__):
            assert DegradedTracker(OUTBOUND).record_success() is None
        assert _tracker_records(caplog) == []
        assert degraded_notices == []

    def test_reentry_after_recovery_raises_a_fresh_notice(
        self, degraded_notices: list[tuple[str, ...]]
    ) -> None:
        tracker = DegradedTracker(OUTBOUND)
        assert tracker.record_failure(NETWORK_DEGRADED, TimedOut(), now=_T0) == (
            "entered"
        )
        tracker.record_success(now=_T0 + 60)
        assert tracker.record_failure(NETWORK_DEGRADED, TimedOut(), now=_T0 + 120) == (
            "entered"
        )

        assert [c[0] for c in degraded_notices] == ["raise", "settle", "raise"]

    def test_changing_state_settles_the_old_notice_and_raises_a_new_one(
        self,
        degraded_notices: list[tuple[str, ...]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tracker = DegradedTracker(OUTBOUND)
        tracker.record_failure(CREDENTIAL_UNAVAILABLE, now=_T0)
        tracker.record_failure(CREDENTIAL_UNAVAILABLE, now=_T0 + 5)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=degraded.__name__):
            transition = tracker.record_failure(
                NETWORK_DEGRADED, NetworkError("Bad Gateway"), now=_T0 + 65
            )

        assert transition == "changed"
        assert degraded_notices == [
            ("raise", OUTBOUND, CREDENTIAL_UNAVAILABLE, "notice-0"),
            (
                "settle",
                "notice-0",
                "changed to network_degraded after 1m 05s (2 failures)",
            ),
            ("raise", OUTBOUND, NETWORK_DEGRADED, "notice-2"),
        ]
        (record,) = _tracker_records(caplog)
        assert record.levelno == logging.WARNING
        assert "changed from credential_unavailable to network_degraded" in (
            record.getMessage()
        )
        current = tracker.current()
        assert current is not None
        assert (current.state, current.count) == (NETWORK_DEGRADED, 1)

    def test_scoped_success_does_not_clear_other_states(
        self, degraded_notices: list[tuple[str, ...]]
    ) -> None:
        tracker = DegradedTracker(RECEIVER)
        tracker.record_failure(NETWORK_DEGRADED, TimedOut(), now=_T0)

        # A token lookup proves credentials, not the network.
        assert tracker.record_success(states=(CREDENTIAL_UNAVAILABLE,)) is None
        assert tracker.current() is not None
        assert [c[0] for c in degraded_notices] == ["raise"]

    def test_components_are_tracked_independently(self) -> None:
        DegradedTracker(OUTBOUND).record_failure(CREDENTIAL_UNAVAILABLE, now=_T0)
        assert DegradedTracker(RECEIVER).current() is None

    def test_unreadable_state_counts_as_healthy(
        self, degraded_notices: list[tuple[str, ...]]
    ) -> None:
        tracker = DegradedTracker(OUTBOUND)
        tracker.path.parent.mkdir(parents=True, exist_ok=True)
        tracker.path.write_text("{not json", encoding="utf-8")

        assert tracker.current() is None
        assert tracker.record_failure(NETWORK_DEGRADED, TimedOut(), now=_T0) == (
            "entered"
        )
        assert tracker.current() is not None

    def test_notice_store_failure_is_logged_and_tracking_continues(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _boom(*_args: object) -> str:
            raise OSError("store is read-only")

        monkeypatch.setattr(degraded, "_raise_notice", _boom)
        tracker = DegradedTracker(OUTBOUND)
        with caplog.at_level(logging.WARNING, logger=degraded.__name__):
            tracker.record_failure(CREDENTIAL_UNAVAILABLE, now=_T0)

        assert (
            "Could not store the Telegram outbound degraded notice: "
            "OSError: store is read-only"
        ) in caplog.text
        assert all(r.exc_info is None for r in caplog.records)
        record = tracker.current()
        assert record is not None and record.notice_id is None
        # Recovery without a notice id has nothing to settle.
        assert tracker.record_success(now=_T0 + 1) is not None

    def test_unknown_component_or_state_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            DegradedTracker("sideways")
        with pytest.raises(ValueError):
            DegradedTracker(OUTBOUND).record_failure("sad", now=_T0)


class TestStoreBackedNotices:
    def _record(self) -> degraded.DegradedRecord:
        return degraded.DegradedRecord(
            state=CREDENTIAL_UNAVAILABLE,
            entered_at=_T0,
            last_seen=_T0,
            count=1,
            last_summary_at=_T0,
            suppressed=0,
            detail="TelegramCredentialError: no token",
        )

    def test_raise_creates_a_deduped_telegram_notice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        upsert = MagicMock(
            side_effect=lambda n, **_kw: SimpleNamespace(action="created", id=n.id)
        )
        append = MagicMock()
        monkeypatch.setattr("sase.notifications.store.upsert_notification", upsert)
        monkeypatch.setattr("sase.notifications.store.append_notification", append)

        notice_id = _REAL_RAISE_NOTICE(OUTBOUND, "Telegram outbound", self._record())

        notification = upsert.call_args.args[0]
        assert notice_id == notification.id
        assert notification.sender == "telegram"
        assert notification.dedup_key == notice_dedup_key(
            OUTBOUND, CREDENTIAL_UNAVAILABLE
        )
        assert "Telegram outbound is degraded" in notification.notes[0]
        assert notification.notes[1] == "TelegramCredentialError: no token"
        assert "error" in notification.tags
        append.assert_not_called()

    def test_raise_after_a_settled_episode_creates_a_new_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        upsert = MagicMock(
            return_value=SimpleNamespace(action="skipped_dismissed", id="old")
        )
        append = MagicMock()
        monkeypatch.setattr("sase.notifications.store.upsert_notification", upsert)
        monkeypatch.setattr("sase.notifications.store.append_notification", append)

        notice_id = _REAL_RAISE_NOTICE(OUTBOUND, "Telegram outbound", self._record())

        (created,) = append.call_args.args
        assert notice_id == created.id != "old"

    def test_raise_reuses_a_still_open_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sase.notifications.store.upsert_notification",
            MagicMock(return_value=SimpleNamespace(action="plus_oned", id="open-row")),
        )

        assert (
            _REAL_RAISE_NOTICE(OUTBOUND, "Telegram outbound", self._record())
            == "open-row"
        )

    def test_settle_adds_a_note_then_dismisses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, object]] = []
        monkeypatch.setattr(
            "sase.notifications.store.append_notification_plus_one",
            lambda **kw: calls.append(("plus_one", kw)),
        )
        monkeypatch.setattr(
            "sase.notifications.store.mark_dismissed",
            lambda notice_id: calls.append(("dismiss", notice_id)),
        )

        _REAL_SETTLE_NOTICE("row-1", "recovered after 5m 00s (3 failures)")

        assert [name for name, _ in calls] == ["plus_one", "dismiss"]
        plus_one = calls[0][1]
        assert isinstance(plus_one, dict)
        assert plus_one["notification_id"] == "row-1"
        assert plus_one["note"] == "recovered after 5m 00s (3 failures)"
        assert plus_one["sender"] == "telegram"
        assert calls[1] == ("dismiss", "row-1")


class TestSkipOutboundDelivery:
    def _notice(self, component: str, *, dismissed: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            sender="telegram",
            dedup_key=notice_dedup_key(component, NETWORK_DEGRADED),
            dismissed=dismissed,
        )

    def test_outbound_never_delivers_its_own_notice(self) -> None:
        assert skip_outbound_delivery(self._notice(OUTBOUND))
        assert skip_outbound_delivery(self._notice(OUTBOUND, dismissed=True))

    def test_receiver_notice_is_delivered_until_settled(self) -> None:
        assert not skip_outbound_delivery(self._notice(RECEIVER))
        assert skip_outbound_delivery(self._notice(RECEIVER, dismissed=True))

    @pytest.mark.parametrize(
        "notification",
        [
            SimpleNamespace(sender="crs", dedup_key=None, dismissed=False),
            SimpleNamespace(
                sender="other",
                dedup_key=notice_dedup_key(OUTBOUND, NETWORK_DEGRADED),
                dismissed=False,
            ),
            SimpleNamespace(
                sender="telegram",
                dedup_key="telegram-receiver-launch-failure",
                dismissed=False,
            ),
            SimpleNamespace(
                sender="telegram",
                dedup_key="telegram-degraded:outbound:sad",
                dismissed=False,
            ),
        ],
    )
    def test_other_notifications_are_delivered(
        self, notification: SimpleNamespace
    ) -> None:
        assert not skip_outbound_delivery(notification)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (45, "45s"),
        (750, "12m 30s"),
        (900, "15m"),
        (3 * 3600, "3h"),
        (3 * 3600 + 5 * 60, "3h 05m"),
        (2 * 86_400, "2d"),
        (2 * 86_400 + 4 * 3600, "2d 4h"),
        (-5, "0s"),
    ],
)
def test_format_duration(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text
