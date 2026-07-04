"""Tests for credential retrieval functions."""

import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from sase_telegram.credentials import (
    TelegramCredentialError,
    get_bot_token,
    get_chat_id,
    get_bot_username,
)


@pytest.fixture(autouse=True)
def _clear_bot_token_cache() -> Iterator[None]:
    get_bot_token.cache_clear()
    yield
    get_bot_token.cache_clear()


def _point_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


class TestGetBotToken:
    def test_returns_token_from_env_before_other_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        monkeypatch.setenv("SASE_TELEGRAM_BOT_TOKEN", " env-token \n")

        token_file = tmp_path / ".sase" / "telegram_bot_token"
        token_file.parent.mkdir()
        token_file.write_text("file-token\n", encoding="utf-8")
        token_file.chmod(0o600)

        with patch("sase_telegram.credentials.subprocess.run") as mock_run:
            assert get_bot_token() == "env-token"
            mock_run.assert_not_called()

    def test_returns_token_from_file_before_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        monkeypatch.delenv("SASE_TELEGRAM_BOT_TOKEN", raising=False)
        token_file = tmp_path / ".sase" / "telegram_bot_token"
        token_file.parent.mkdir()
        token_file.write_text("file-token\n", encoding="utf-8")
        token_file.chmod(0o600)

        with patch("sase_telegram.credentials.subprocess.run") as mock_run:
            assert get_bot_token() == "file-token"
            mock_run.assert_not_called()

    def test_returns_token_from_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        monkeypatch.delenv("SASE_TELEGRAM_BOT_TOKEN", raising=False)
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

    def test_caches_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        monkeypatch.delenv("SASE_TELEGRAM_BOT_TOKEN", raising=False)
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="token\n", stderr=""
        )
        with patch(
            "sase_telegram.credentials.subprocess.run", return_value=mock_result
        ) as mock_run:
            get_bot_token()
            get_bot_token()
            mock_run.assert_called_once()

    def test_rejects_group_or_other_readable_token_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        monkeypatch.delenv("SASE_TELEGRAM_BOT_TOKEN", raising=False)
        token_file = tmp_path / ".sase" / "telegram_bot_token"
        token_file.parent.mkdir()
        token_file.write_text("file-token\n", encoding="utf-8")
        token_file.chmod(0o644)

        with (
            patch(
                "sase_telegram.credentials.subprocess.run",
                side_effect=FileNotFoundError,
            ),
            pytest.raises(TelegramCredentialError) as exc,
        ):
            get_bot_token()

        message = str(exc.value)
        assert "Telegram bot token unavailable" in message
        assert "SASE_TELEGRAM_BOT_TOKEN" in message
        assert "~/.sase/telegram_bot_token" in message
        assert "chmod 600" in message
        assert "pass" in message

    def test_raises_actionable_error_when_every_source_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        monkeypatch.delenv("SASE_TELEGRAM_BOT_TOKEN", raising=False)

        with (
            patch(
                "sase_telegram.credentials.subprocess.run",
                side_effect=FileNotFoundError,
            ),
            pytest.raises(TelegramCredentialError) as exc,
        ):
            get_bot_token()

        assert str(exc.value).startswith(
            "Telegram bot token unavailable: set SASE_TELEGRAM_BOT_TOKEN"
        )
        assert "create ~/.sase/telegram_bot_token" in str(exc.value)
        assert "pass executable was not found" in str(exc.value)


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
