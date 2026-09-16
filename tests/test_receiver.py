"""Tests for the supervised Telegram long-poll receiver launcher."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sase_telegram import receiver


def _proc(
    *,
    proc_id: str = "proc-1",
    request_fingerprint: str = "telegram-receiver:12345",
    status: str = "error",
    termination_reason: str | None = "launch-failure",
    message: str = "could not start command",
    finished_at: str | None = None,
) -> SimpleNamespace:
    result = {"message": message}
    if termination_reason is not None:
        result["termination_reason"] = termination_reason
    return SimpleNamespace(
        proc_id=proc_id,
        request_fingerprint=request_fingerprint,
        status=status,
        result=result,
        stop_reason=None,
        message=message,
        log_path=f"/tmp/{proc_id}.log",
        created_at=datetime.now(UTC).isoformat(),
        finished_at=finished_at or datetime.now(UTC).isoformat(),
    )


def _use_temp_sase_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("SASE_HOME", str(tmp_path / "home"))

    from sase.notifications import store as notification_store

    notification_store.NOTIFICATIONS_DIR = None
    notification_store.NOTIFICATIONS_FILE = None
    notification_store._invalidate_load_cache()
    return notification_store


def _receiver_notifications(notification_store: Any) -> list[Any]:
    return [
        notification
        for notification in notification_store.load_notifications(
            include_dismissed=True
        )
        if notification.sender == "telegram"
        and notification.dedup_key == "telegram-receiver-launch-failure"
    ]


class TestReceiverIdentity:
    @patch("sase_telegram.credentials.get_chat_id", return_value="98765")
    def test_identity_includes_chat_id(self, _mock_chat_id: MagicMock) -> None:
        assert receiver.receiver_identity() == "telegram-receiver:98765"

    @patch("sase_telegram.credentials.get_chat_id", side_effect=RuntimeError("boom"))
    def test_identity_falls_back_when_chat_id_unresolvable(
        self, _mock_chat_id: MagicMock
    ) -> None:
        assert receiver.receiver_identity() == "telegram-receiver:unconfigured"


class TestEnsureReceiverRunning:
    @patch("sase.procs.submit_proc_request")
    @patch("sase_telegram.credentials.get_chat_id", return_value="12345")
    @patch(
        "sase_telegram.receiver.resolve_console_script",
        return_value="/venv/bin/sase_job_tg_inbound",
    )
    @patch("sase.procs.store.read_proc_snapshot")
    def test_submits_a_deterministic_request(
        self,
        mock_snapshot: MagicMock,
        _mock_resolve: MagicMock,
        _mock_chat_id: MagicMock,
        mock_submit: MagicMock,
    ) -> None:
        mock_snapshot.return_value = SimpleNamespace(procs=[])
        receiver.ensure_receiver_running()

        assert mock_submit.call_count == 1
        request = mock_submit.call_args.args[0]
        assert request.argv == ["/venv/bin/sase_job_tg_inbound", "--receiver"]
        assert request.origin == "telegram-receiver"
        assert request.concurrency_keys == ["telegram-receiver:12345"]
        assert request.request_fingerprint == "telegram-receiver:12345"
        # A persistent worker must not be bounded by a command/idle timeout.
        assert request.timeout_seconds is None
        assert request.idle_timeout_seconds is None

    @patch("sase.procs.submit_proc_request")
    @patch("sase_telegram.credentials.get_chat_id", return_value="12345")
    @patch(
        "sase_telegram.receiver.resolve_console_script",
        return_value="/venv/bin/sase_job_tg_inbound",
    )
    @patch("sase.procs.store.read_proc_snapshot")
    def test_two_calls_carry_the_same_fingerprint_and_concurrency_key(
        self,
        mock_snapshot: MagicMock,
        _mock_resolve: MagicMock,
        _mock_chat_id: MagicMock,
        mock_submit: MagicMock,
    ) -> None:
        """Same identity every call -- the mechanism a competing tick relies on.

        The actual single-owner dedup (replaying the active row for a
        matching fingerprint) is durable proc-store behavior exercised end
        to end in ``TestSingleOwnerReplay`` below; this test only pins down
        that *this* call site always asks for the same identity, since that
        determinism is what makes replay possible.
        """
        mock_snapshot.return_value = SimpleNamespace(procs=[])
        receiver.ensure_receiver_running()
        receiver.ensure_receiver_running()

        first, second = (call.args[0] for call in mock_submit.call_args_list)
        assert first.request_fingerprint == second.request_fingerprint
        assert first.concurrency_keys == second.concurrency_keys

    @patch("sase.procs.submit_proc_request")
    @patch("sase_telegram.credentials.get_chat_id", return_value="12345")
    @patch("sase.procs.store.read_proc_snapshot")
    def test_recent_launch_failure_notifies_once_and_suppresses_rearm(
        self,
        mock_snapshot: MagicMock,
        _mock_chat_id: MagicMock,
        mock_submit: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        failed = _proc()
        mock_snapshot.return_value = SimpleNamespace(procs=[failed])
        notifications: list[object] = []
        read_notifications = MagicMock(
            side_effect=lambda **_kwargs: SimpleNamespace(
                notifications=list(notifications)
            )
        )

        def _upsert_notification(
            notification: object, *, plus_one_note: str | None = None
        ) -> None:
            assert plus_one_note == "Proc proc-1: could not start command"
            notifications.append(notification)

        upsert_notification = MagicMock(side_effect=_upsert_notification)
        monkeypatch.setattr(
            "sase.notifications.store.read_current_notification_snapshot",
            read_notifications,
        )
        monkeypatch.setattr(
            "sase.notifications.store.upsert_notification",
            upsert_notification,
        )

        first = receiver.ensure_receiver_running()
        second = receiver.ensure_receiver_running()

        assert first is failed
        assert second is failed
        mock_submit.assert_not_called()
        assert upsert_notification.call_count == 1
        notification = upsert_notification.call_args.args[0]
        assert notification.sender == "telegram"
        assert notification.dedup_key == "telegram-receiver-launch-failure"
        assert "could not start command" in "\n".join(notification.notes)
        assert "/tmp/proc-1.log" in "\n".join(notification.notes)
        assert notification.files == ["/tmp/proc-1.log"]

    @pytest.mark.parametrize(
        "row",
        [
            None,
            _proc(status="running", termination_reason=None),
            _proc(status="success", termination_reason="success"),
            _proc(status="error", termination_reason="error"),
        ],
    )
    @patch("sase.procs.submit_proc_request")
    @patch("sase_telegram.credentials.get_chat_id", return_value="12345")
    @patch(
        "sase_telegram.receiver.resolve_console_script",
        return_value="/venv/bin/sase_job_tg_inbound",
    )
    @patch("sase.procs.store.read_proc_snapshot")
    def test_absent_or_healthy_newest_row_rearms(
        self,
        mock_snapshot: MagicMock,
        _mock_resolve: MagicMock,
        _mock_chat_id: MagicMock,
        mock_submit: MagicMock,
        row: object | None,
    ) -> None:
        mock_snapshot.return_value = SimpleNamespace(procs=[] if row is None else [row])
        launched = MagicMock(proc_id="fresh")
        mock_submit.return_value = launched

        assert receiver.ensure_receiver_running() is launched

        mock_submit.assert_called_once()

    @patch("sase.procs.submit_proc_request")
    @patch("sase_telegram.credentials.get_chat_id", return_value="12345")
    @patch(
        "sase_telegram.receiver.resolve_console_script",
        return_value="/venv/bin/sase_job_tg_inbound",
    )
    @patch("sase.procs.store.read_proc_snapshot")
    def test_old_launch_failure_rearms_after_backoff(
        self,
        mock_snapshot: MagicMock,
        _mock_resolve: MagicMock,
        _mock_chat_id: MagicMock,
        mock_submit: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        failed_at = datetime.now(UTC) - timedelta(seconds=301)
        mock_snapshot.return_value = SimpleNamespace(
            procs=[_proc(finished_at=failed_at.isoformat())]
        )
        monkeypatch.setattr(
            "sase.notifications.store.read_current_notification_snapshot",
            lambda **_kwargs: SimpleNamespace(notifications=[]),
        )

        def _upsert_notification(
            _notification: object, *, plus_one_note: str | None = None
        ) -> None:
            assert plus_one_note == "Proc proc-1: could not start command"

        monkeypatch.setattr(
            "sase.notifications.store.upsert_notification",
            _upsert_notification,
        )
        launched = MagicMock(proc_id="fresh")
        mock_submit.return_value = launched

        assert receiver.ensure_receiver_running() is launched

        mock_submit.assert_called_once()


class TestReceiverLaunchFailureNotifications:
    @patch("sase.procs.submit_proc_request")
    @patch("sase_telegram.credentials.get_chat_id", return_value="12345")
    @patch("sase.procs.store.read_proc_snapshot")
    def test_recent_launch_failure_creates_real_notification_during_backoff(
        self,
        mock_snapshot: MagicMock,
        _mock_chat_id: MagicMock,
        mock_submit: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        notification_store = _use_temp_sase_home(tmp_path, monkeypatch)
        failed = _proc()
        mock_snapshot.return_value = SimpleNamespace(procs=[failed])

        assert receiver.ensure_receiver_running() is failed

        mock_submit.assert_not_called()
        rows = _receiver_notifications(notification_store)
        assert len(rows) == 1
        notification = rows[0]
        assert notification.plus_one_count == 0
        assert notification.dismissed is False
        assert "Proc proc-1: could not start command" in "\n".join(notification.notes)
        assert notification.files == ["/tmp/proc-1.log"]

    @pytest.mark.parametrize("dismissed", [False, True])
    @patch("sase.procs.submit_proc_request")
    @patch("sase_telegram.credentials.get_chat_id", return_value="12345")
    @patch("sase.procs.store.read_proc_snapshot")
    def test_existing_notification_blocks_repeated_ticks_even_when_dismissed(
        self,
        mock_snapshot: MagicMock,
        _mock_chat_id: MagicMock,
        mock_submit: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        dismissed: bool,
    ) -> None:
        notification_store = _use_temp_sase_home(tmp_path, monkeypatch)
        failed = _proc()
        mock_snapshot.return_value = SimpleNamespace(procs=[failed])

        receiver.ensure_receiver_running()
        [notification] = _receiver_notifications(notification_store)
        notification_store.append_notification_plus_one(
            note="existing retry evidence",
            sender="telegram",
            timestamp=datetime.now(UTC).isoformat(),
            dedup_key="telegram-receiver-launch-failure",
        )
        if dismissed:
            assert notification_store.mark_dismissed(notification.id) is True

        receiver.ensure_receiver_running()
        receiver.ensure_receiver_running()

        mock_submit.assert_not_called()
        rows = _receiver_notifications(notification_store)
        assert len(rows) == 1
        assert rows[0].plus_one_count == 1
        assert rows[0].dismissed is dismissed

    @patch("sase.procs.submit_proc_request")
    @patch("sase_telegram.credentials.get_chat_id", return_value="12345")
    @patch(
        "sase_telegram.receiver.resolve_console_script",
        return_value="/venv/bin/sase_job_tg_inbound",
    )
    @patch("sase.procs.store.read_proc_snapshot")
    def test_expired_launch_failure_notifies_and_rearms_with_real_store(
        self,
        mock_snapshot: MagicMock,
        _mock_resolve: MagicMock,
        _mock_chat_id: MagicMock,
        mock_submit: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        notification_store = _use_temp_sase_home(tmp_path, monkeypatch)
        failed_at = datetime.now(UTC) - timedelta(seconds=301)
        mock_snapshot.return_value = SimpleNamespace(
            procs=[_proc(finished_at=failed_at.isoformat())]
        )
        launched = MagicMock(proc_id="fresh")
        mock_submit.return_value = launched

        assert receiver.ensure_receiver_running() is launched

        request = mock_submit.call_args.args[0]
        assert request.argv == ["/venv/bin/sase_job_tg_inbound", "--receiver"]
        rows = _receiver_notifications(notification_store)
        assert len(rows) == 1
        assert rows[0].plus_one_count == 0

    @patch("sase.procs.submit_proc_request")
    @patch("sase_telegram.credentials.get_chat_id", return_value="12345")
    @patch("sase.procs.store.read_proc_snapshot")
    def test_stale_empty_snapshot_race_plus_ones_existing_notification(
        self,
        mock_snapshot: MagicMock,
        _mock_chat_id: MagicMock,
        mock_submit: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        notification_store = _use_temp_sase_home(tmp_path, monkeypatch)
        mock_snapshot.return_value = SimpleNamespace(procs=[_proc()])
        stale_read = MagicMock(return_value=SimpleNamespace(notifications=[]))
        monkeypatch.setattr(
            notification_store,
            "read_current_notification_snapshot",
            stale_read,
        )

        assert receiver.ensure_receiver_running().proc_id == "proc-1"
        assert receiver.ensure_receiver_running().proc_id == "proc-1"

        mock_submit.assert_not_called()
        assert stale_read.call_count == 2
        rows = _receiver_notifications(notification_store)
        assert len(rows) == 1
        notification = rows[0]
        assert notification.plus_one_count == 1
        assert len(notification.plus_ones) == 1
        assert notification.plus_ones[0].note == "Proc proc-1: could not start command"


class TestSingleOwnerReplay:
    """Prove two racing ``ensure_receiver_running`` calls yield one proc.

    Uses the real durable proc store (isolated under a temp ``SASE_HOME``)
    with a harmless long-sleeping stand-in command instead of the real
    Telegram receiver, so this exercises the actual single-owner reservation
    sase-telegram relies on -- the "two competing pollers" scenario -- without
    a real receiver process or network calls.
    """

    def test_second_ensure_call_replays_the_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sase.procs import kill_proc, wait_for_proc

        monkeypatch.setenv("SASE_HOME", str(tmp_path / "home"))
        sleep_argv = [sys.executable, "-c", "import time; time.sleep(20)"]

        with patch("sase_telegram.credentials.get_chat_id", return_value="racer"):
            first = receiver.ensure_receiver_running(argv=sleep_argv)
            second = receiver.ensure_receiver_running(argv=sleep_argv)

        try:
            assert second.proc_id == first.proc_id
            assert second.status in {"pending", "running"}
        finally:
            kill_proc(first.proc_id)
            wait_for_proc(first.proc_id, timeout=15)

    def test_ensure_after_termination_launches_a_fresh_receiver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crashed/killed receiver is replaced, not left dead forever.

        procs do not auto-relaunch a crashed supervised proc (see
        ``receiver.py``'s module docstring), so this proves the *other*
        half of that design: once the durable row is terminal, the next
        ``ensure_receiver_running`` call (mirroring the next job tick)
        reserves a genuinely new proc rather than replaying the dead one.
        """
        from sase.procs import kill_proc, wait_for_proc

        monkeypatch.setenv("SASE_HOME", str(tmp_path / "home"))
        sleep_argv = [sys.executable, "-c", "import time; time.sleep(20)"]

        with patch("sase_telegram.credentials.get_chat_id", return_value="racer"):
            first = receiver.ensure_receiver_running(argv=sleep_argv)
            kill_proc(first.proc_id)
            wait_for_proc(first.proc_id, timeout=15)

            second = receiver.ensure_receiver_running(argv=sleep_argv)

        try:
            assert second.proc_id != first.proc_id
            assert second.status in {"pending", "running"}
        finally:
            kill_proc(second.proc_id)
            wait_for_proc(second.proc_id, timeout=15)
