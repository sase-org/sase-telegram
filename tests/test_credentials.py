"""Tests for credential retrieval functions."""

import subprocess
from unittest.mock import patch

import pytest

from sase_telegram.credentials import get_bot_token, get_chat_id, get_bot_username


class TestGetBotToken:
    def test_returns_token_from_pass(self) -> None:
        # Clear LRU cache before test
        get_bot_token.cache_clear()
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="my-secret-token\n", stderr=""
        )
        with patch(
            "sase_telegram.credentials.subprocess.run", return_value=mock_result
        ) as mock_run:
            token = get_bot_token()
            assert token == "my-secret-token"
            mock_run.assert_called_once_with(
                ["pass", "show", "telegram_sase_bot_token"],
                capture_output=True,
                text=True,
                check=True,
            )
        get_bot_token.cache_clear()

    def test_caches_result(self) -> None:
        get_bot_token.cache_clear()
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="token\n", stderr=""
        )
        with patch(
            "sase_telegram.credentials.subprocess.run", return_value=mock_result
        ) as mock_run:
            get_bot_token()
            get_bot_token()
            mock_run.assert_called_once()
        get_bot_token.cache_clear()


class TestGetChatId:
    def test_returns_env_var(self) -> None:
        with patch.dict("os.environ", {"SASE_TELEGRAM_BOT_CHAT_ID": "12345"}):
            assert get_chat_id() == "12345"

    def test_raises_when_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="SASE_TELEGRAM_BOT_CHAT_ID"):
                get_chat_id()


class TestGetBotUsername:
    def test_returns_env_var(self) -> None:
        with patch.dict("os.environ", {"SASE_TELEGRAM_BOT_USERNAME": "mybot"}):
            assert get_bot_username() == "mybot"

    def test_raises_when_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="SASE_TELEGRAM_BOT_USERNAME"):
                get_bot_username()
