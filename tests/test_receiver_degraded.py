"""Degraded-state tracking in the supervised Telegram long-poll receiver."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from telegram.error import NetworkError, TimedOut

from inbound_namespace import INBOUND as inbound
from sase_telegram.credentials import TelegramCredentialError
from sase_telegram.degraded import (
    CREDENTIAL_UNAVAILABLE,
    NETWORK_DEGRADED,
    RECEIVER,
    DegradedTracker,
)


@pytest.fixture
def loop() -> Iterator[SimpleNamespace]:
    """Stub everything in the receiver loop except its failure handling."""
    stubs = SimpleNamespace(
        enabled=MagicMock(return_value=True),
        bot_token=MagicMock(return_value="tok"),
        get_updates=MagicMock(return_value=[]),
        pre_poll=MagicMock(return_value=(0, 0)),
        post_poll=MagicMock(return_value=0),
        sleep=MagicMock(),
    )
    with ExitStack() as stack:
        for target, value in (
            ("observe_runtime_generation", lambda: SimpleNamespace(digest="gen")),
            ("is_telegram_enabled", stubs.enabled),
            ("load_custom_commands", lambda: {}),
            ("get_last_offset", lambda: None),
            ("_has_recent_pending_followups", lambda: False),
            ("_run_pre_poll_cleanup", stubs.pre_poll),
            ("_run_post_poll_cleanup", stubs.post_poll),
            (
                "_dispatch_fetched_updates",
                lambda updates, **_kw: SimpleNamespace(updates=updates),
            ),
        ):
            stack.enter_context(patch.object(inbound, target, value))
        stack.enter_context(
            patch.object(inbound.credentials, "get_bot_token", stubs.bot_token)
        )
        stack.enter_context(
            patch.object(inbound.credentials, "get_chat_id", return_value="1")
        )
        stack.enter_context(
            patch.object(inbound.telegram_client, "get_updates", stubs.get_updates)
        )
        stack.enter_context(patch.object(inbound.time, "sleep", stubs.sleep))
        yield stubs


def _polls_then_disable(stubs: SimpleNamespace, polls: int) -> None:
    stubs.enabled.side_effect = [True] * polls + [False]


def test_credential_outage_across_restarts_notifies_once_and_keeps_exit_75(
    loop: SimpleNamespace,
    degraded_notices: list[tuple[str, ...]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop.bot_token.side_effect = TelegramCredentialError("gpg: decryption failed")

    with caplog.at_level(logging.INFO):
        # Each service-host restart is a fresh receiver run.
        exits = [inbound._run_receiver() for _restart in range(40)]

    assert set(exits) == {inbound._RECEIVER_CREDENTIALS_UNAVAILABLE_EXIT_CODE}
    loop.get_updates.assert_not_called()
    assert [call[0] for call in degraded_notices] == ["raise"]
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "Telegram receiver entered credential_unavailable" in (
        warnings[0].getMessage()
    )
    assert all(r.exc_info is None for r in caplog.records)
    record = DegradedTracker(RECEIVER).current()
    assert record is not None
    assert (record.state, record.count) == (CREDENTIAL_UNAVAILABLE, 40)


def test_credential_recovery_logs_once_and_settles(
    loop: SimpleNamespace,
    degraded_notices: list[tuple[str, ...]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop.bot_token.side_effect = TelegramCredentialError("no token")
    inbound._run_receiver()
    inbound._run_receiver()

    loop.bot_token.side_effect = None
    _polls_then_disable(loop, 3)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inbound._run_receiver() == 0

    recovered = [r for r in caplog.records if "recovered from" in r.getMessage()]
    assert len(recovered) == 1
    assert recovered[0].levelno == logging.INFO
    assert "credential_unavailable" in recovered[0].getMessage()
    assert [call[0] for call in degraded_notices] == ["raise", "settle"]
    assert DegradedTracker(RECEIVER).current() is None


def test_poll_timeouts_log_no_traceback_and_recover_on_the_next_poll(
    loop: SimpleNamespace,
    degraded_notices: list[tuple[str, ...]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop.get_updates.side_effect = [
        TimedOut(),
        NetworkError("Bad Gateway"),
        TimedOut(),
        [],
    ]
    _polls_then_disable(loop, 4)

    with caplog.at_level(logging.INFO):
        assert inbound._run_receiver() == 0

    assert "Traceback" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)
    assert "Receiver poll failed" not in caplog.text
    messages = [r.getMessage() for r in caplog.records]
    assert sum("entered network_degraded" in m for m in messages) == 1
    assert sum("recovered from network_degraded" in m for m in messages) == 1
    assert [call[0] for call in degraded_notices] == ["raise", "settle"]
    assert loop.sleep.call_count == 3
    assert DegradedTracker(RECEIVER).current() is None


def test_successful_token_lookup_does_not_clear_network_degraded(
    loop: SimpleNamespace,
) -> None:
    loop.get_updates.side_effect = TimedOut()
    _polls_then_disable(loop, 2)

    inbound._run_receiver()

    record = DegradedTracker(RECEIVER).current()
    assert record is not None
    assert (record.state, record.count) == (NETWORK_DEGRADED, 2)


def test_unexpected_poll_failure_keeps_its_traceback(
    loop: SimpleNamespace,
    degraded_notices: list[tuple[str, ...]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop.get_updates.side_effect = [RuntimeError("handler bug"), []]
    _polls_then_disable(loop, 2)

    with caplog.at_level(logging.WARNING):
        assert inbound._run_receiver() == 0

    (failure,) = [r for r in caplog.records if "Receiver poll failed" in r.message]
    assert failure.exc_info is not None
    assert degraded_notices == []


def test_transient_housekeeping_failure_is_one_line_and_not_a_state(
    loop: SimpleNamespace,
    degraded_notices: list[tuple[str, ...]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop.pre_poll.side_effect = [TimedOut(), (0, 0)]
    loop.post_poll.side_effect = [RuntimeError("store bug"), 0]
    _polls_then_disable(loop, 2)

    with caplog.at_level(logging.WARNING):
        assert inbound._run_receiver() == 0

    pre, post = [r for r in caplog.records if "housekeeping failed" in r.message]
    assert pre.message == (
        "Receiver pre-poll housekeeping failed; continuing: TimedOut: Timed out"
    )
    assert pre.exc_info is None
    assert post.exc_info is not None
    assert degraded_notices == []
    assert DegradedTracker(RECEIVER).current() is None
