"""Exit-code contract for the supervised Telegram long-poll receiver."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from sase_telegram.credentials import TelegramCredentialError
from sase_telegram.scripts import sase_tg_inbound as inbound


def _baseline() -> SimpleNamespace:
    return SimpleNamespace(digest="test-generation")


def test_disabled_receiver_exits_zero_without_touching_credentials() -> None:
    with (
        patch.object(inbound, "observe_runtime_generation", return_value=_baseline()),
        patch.object(inbound, "is_telegram_enabled", return_value=False),
        patch.object(inbound.credentials, "get_bot_token") as bot_token,
    ):
        assert inbound._run_receiver() == 0
    bot_token.assert_not_called()


def test_missing_bot_token_exits_retryable_tempfail() -> None:
    with (
        patch.object(inbound, "observe_runtime_generation", return_value=_baseline()),
        patch.object(inbound, "is_telegram_enabled", return_value=True),
        patch.object(
            inbound.credentials,
            "get_bot_token",
            side_effect=TelegramCredentialError("no token"),
        ),
    ):
        assert inbound._run_receiver() == 75
        assert (
            inbound._run_receiver()
            == inbound._RECEIVER_CREDENTIALS_UNAVAILABLE_EXIT_CODE
        )


def test_missing_chat_id_keeps_config_exit_code() -> None:
    with (
        patch.object(inbound, "observe_runtime_generation", return_value=_baseline()),
        patch.object(inbound, "is_telegram_enabled", return_value=True),
        patch.object(inbound.credentials, "get_bot_token", return_value="tok"),
        patch.object(
            inbound.credentials,
            "get_chat_id",
            side_effect=TelegramCredentialError("no chat"),
        ),
        patch.object(inbound, "_notify_receiver_chat_id_missing") as notify,
    ):
        result = inbound._run_receiver()
        assert result == 78
        assert result == inbound._RECEIVER_CHAT_ID_MISSING_EXIT_CODE
    notify.assert_called_once()
