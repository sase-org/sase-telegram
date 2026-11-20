"""Tests for credential retrieval functions."""

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from sase_telegram import credentials
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


class TestBotTokenNegativeCache:
    """A failed ``pass`` lookup is remembered across processes for a short window."""

    @staticmethod
    def _failing_pass() -> subprocess.CalledProcessError:
        return subprocess.CalledProcessError(
            2,
            ["pass"],
            stderr="gpg: public key decryption failed: No such file or directory\n",
        )

    @staticmethod
    def _fresh_process() -> None:
        # The outbound job is a new process every tick: nothing in memory.
        get_bot_token.cache_clear()

    def _no_env_or_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _point_home(monkeypatch, tmp_path)
        monkeypatch.delenv("SASE_TELEGRAM_BOT_TOKEN", raising=False)

    def test_failure_is_not_rerun_within_the_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_env_or_file(tmp_path, monkeypatch)
        with patch(
            "sase_telegram.credentials.subprocess.run",
            side_effect=self._failing_pass(),
        ) as mock_run:
            for _tick in range(5):
                self._fresh_process()
                with pytest.raises(TelegramCredentialError) as exc:
                    get_bot_token()
                assert "gpg: public key decryption failed" in str(exc.value)
                assert "~/.sase/telegram_bot_token does not exist" in str(exc.value)

        mock_run.assert_called_once()

    def test_token_from_pass_is_picked_up_after_the_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_env_or_file(tmp_path, monkeypatch)
        now = [1_000.0]
        monkeypatch.setattr("sase_telegram.credentials.time.time", lambda: now[0])
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="tok\n")
        with patch(
            "sase_telegram.credentials.subprocess.run",
            side_effect=[self._failing_pass(), ok],
        ) as mock_run:
            with pytest.raises(TelegramCredentialError):
                get_bot_token()

            now[0] += credentials.BOT_TOKEN_NEGATIVE_CACHE_SECONDS - 1
            self._fresh_process()
            with pytest.raises(TelegramCredentialError):
                get_bot_token()
            assert mock_run.call_count == 1

            now[0] += 1
            self._fresh_process()
            assert get_bot_token() == "tok"

        assert mock_run.call_count == 2
        # Success forgets the failure.
        assert not credentials.bot_token_negative_cache_path().exists()

    def test_env_token_is_used_immediately_despite_a_cached_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_env_or_file(tmp_path, monkeypatch)
        with patch(
            "sase_telegram.credentials.subprocess.run",
            side_effect=self._failing_pass(),
        ):
            with pytest.raises(TelegramCredentialError):
                get_bot_token()

        monkeypatch.setenv("SASE_TELEGRAM_BOT_TOKEN", "env-token")
        self._fresh_process()
        assert get_bot_token() == "env-token"

    def test_token_file_is_used_immediately_despite_a_cached_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_env_or_file(tmp_path, monkeypatch)
        with patch(
            "sase_telegram.credentials.subprocess.run",
            side_effect=self._failing_pass(),
        ):
            with pytest.raises(TelegramCredentialError):
                get_bot_token()

        token_file = tmp_path / ".sase" / "telegram_bot_token"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text("file-token\n", encoding="utf-8")
        token_file.chmod(0o600)
        self._fresh_process()
        assert get_bot_token() == "file-token"

    @pytest.mark.parametrize(
        "raw",
        [
            "{broken",
            json.dumps({"version": 99, "failed_at": 0, "pass_failures": ["x"]}),
            json.dumps({"version": 1, "failed_at": "soon", "pass_failures": ["x"]}),
            json.dumps({"version": 1, "failed_at": 0, "pass_failures": [1]}),
        ],
    )
    def test_unusable_cache_record_is_ignored(
        self, raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_env_or_file(tmp_path, monkeypatch)
        cache = credentials.bot_token_negative_cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(raw, encoding="utf-8")
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="tok\n")
        with patch("sase_telegram.credentials.subprocess.run", return_value=ok):
            assert get_bot_token() == "tok"

    def test_record_from_the_future_counts_as_expired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_env_or_file(tmp_path, monkeypatch)
        cache = credentials.bot_token_negative_cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {"version": 1, "failed_at": 4_000_000_000.0, "pass_failures": ["x"]}
            ),
            encoding="utf-8",
        )
        ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="tok\n")
        with patch("sase_telegram.credentials.subprocess.run", return_value=ok):
            assert get_bot_token() == "tok"


class TestGetChatId:
    def test_returns_env_var(self) -> None:
        with patch.dict("os.environ", {"SASE_TELEGRAM_BOT_CHAT_ID": "12345"}):
            assert get_chat_id() == "12345"

    def test_strips_surrounding_whitespace(self) -> None:
        with patch.dict("os.environ", {"SASE_TELEGRAM_BOT_CHAT_ID": "  12345  \n"}):
            assert get_chat_id() == "12345"

    def test_raises_when_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(
                TelegramCredentialError, match="SASE_TELEGRAM_BOT_CHAT_ID"
            ) as exc:
                get_chat_id()
        assert "service.procs.telegram_receiver.env" in str(exc.value)
        # Still catchable as RuntimeError for existing callers.
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="SASE_TELEGRAM_BOT_CHAT_ID"):
                get_chat_id()

    def test_raises_when_empty_or_whitespace(self) -> None:
        for value in ("", "   ", "\n\t "):
            with patch.dict(
                "os.environ", {"SASE_TELEGRAM_BOT_CHAT_ID": value}, clear=True
            ):
                with pytest.raises(
                    TelegramCredentialError, match="SASE_TELEGRAM_BOT_CHAT_ID"
                ) as exc:
                    get_chat_id()
                assert "service.procs.telegram_receiver.env" in str(exc.value)


class TestGetBotUsername:
    def test_returns_env_var(self) -> None:
        with patch.dict("os.environ", {"SASE_TELEGRAM_BOT_USERNAME": "mybot"}):
            assert get_bot_username() == "mybot"

    def test_strips_surrounding_whitespace(self) -> None:
        with patch.dict("os.environ", {"SASE_TELEGRAM_BOT_USERNAME": "  mybot  \n"}):
            assert get_bot_username() == "mybot"

    def test_raises_when_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(
                TelegramCredentialError, match="SASE_TELEGRAM_BOT_USERNAME"
            ) as exc:
                get_bot_username()
        assert "service.procs.telegram_receiver.env" in str(exc.value)
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="SASE_TELEGRAM_BOT_USERNAME"):
                get_bot_username()

    def test_raises_when_empty_or_whitespace(self) -> None:
        for value in ("", "   "):
            with patch.dict(
                "os.environ", {"SASE_TELEGRAM_BOT_USERNAME": value}, clear=True
            ):
                with pytest.raises(
                    TelegramCredentialError, match="SASE_TELEGRAM_BOT_USERNAME"
                ):
                    get_bot_username()
