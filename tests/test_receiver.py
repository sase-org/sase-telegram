"""Tests for the supervised Telegram long-poll receiver launcher."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sase_telegram import receiver


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
    def test_submits_a_deterministic_request(
        self, _mock_chat_id: MagicMock, mock_submit: MagicMock
    ) -> None:
        receiver.ensure_receiver_running()

        assert mock_submit.call_count == 1
        request = mock_submit.call_args.args[0]
        assert request.argv == ["sase_chop_tg_inbound", "--receiver"]
        assert request.origin == "telegram-receiver"
        assert request.concurrency_keys == ["telegram-receiver:12345"]
        assert request.request_fingerprint == "telegram-receiver:12345"
        # A persistent worker must not be bounded by a command/idle timeout.
        assert request.timeout_seconds is None
        assert request.idle_timeout_seconds is None

    @patch("sase.procs.submit_proc_request")
    @patch("sase_telegram.credentials.get_chat_id", return_value="12345")
    def test_two_calls_carry_the_same_fingerprint_and_concurrency_key(
        self, _mock_chat_id: MagicMock, mock_submit: MagicMock
    ) -> None:
        """Same identity every call -- the mechanism a competing tick relies on.

        The actual single-owner dedup (replaying the active row for a
        matching fingerprint) is durable proc-store behavior exercised end
        to end in ``TestSingleOwnerReplay`` below; this test only pins down
        that *this* call site always asks for the same identity, since that
        determinism is what makes replay possible.
        """
        receiver.ensure_receiver_running()
        receiver.ensure_receiver_running()

        first, second = (call.args[0] for call in mock_submit.call_args_list)
        assert first.request_fingerprint == second.request_fingerprint
        assert first.concurrency_keys == second.concurrency_keys


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
        ``ensure_receiver_running`` call (mirroring the next chop tick)
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
