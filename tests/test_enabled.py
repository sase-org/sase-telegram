"""Tests for the machine-level telegram enable flag and chop wrapper gate."""

from pathlib import Path

import pytest

from sase_telegram.credentials import TelegramCredentialError
from sase_telegram.enabled import is_telegram_enabled, telegram_enabled_path
from sase_telegram.scripts import inbound_main, outbound_main


def _point_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Redirect ``Path.home()`` at ``home`` on every platform.

    POSIX ``expanduser`` honors ``HOME``, but ``Path.home`` is also patched
    directly so the resolution is robust regardless of platform quirks.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def _enable(home: Path) -> None:
    flag = home / ".sase" / "telegram_is_enabled"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()


class TestIsTelegramEnabled:
    def test_path_points_under_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        assert telegram_enabled_path() == tmp_path / ".sase" / "telegram_is_enabled"

    def test_false_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        assert is_telegram_enabled() is False

    def test_true_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        _enable(tmp_path)
        assert is_telegram_enabled() is True


class TestInboundWrapperGate:
    def test_disabled_returns_zero_and_is_quiet(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _point_home(monkeypatch, tmp_path)  # flag absent

        def _boom(*args: object, **kwargs: object) -> int:
            raise AssertionError("underlying inbound main must not run when disabled")

        monkeypatch.setattr("sase_telegram.scripts.sase_tg_inbound.main", _boom)

        assert inbound_main() == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_enabled_delegates_and_returns_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        _enable(tmp_path)

        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def _fake(*args: object, **kwargs: object) -> int:
            calls.append((args, kwargs))
            return 7

        monkeypatch.setattr("sase_telegram.scripts.sase_tg_inbound.main", _fake)

        assert inbound_main("--once") == 7
        assert calls == [(("--once",), {})]

    def test_enabled_credential_error_prints_single_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        _enable(tmp_path)
        message = "Telegram bot token unavailable: set SASE_TELEGRAM_BOT_TOKEN."

        def _boom(*args: object, **kwargs: object) -> int:
            raise TelegramCredentialError(message)

        monkeypatch.setattr("sase_telegram.scripts.sase_tg_inbound.main", _boom)

        assert inbound_main("--once") == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == f"{message}\n"
        assert "Traceback" not in captured.err


class TestOutboundWrapperGate:
    def test_disabled_returns_zero_and_is_quiet(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _point_home(monkeypatch, tmp_path)  # flag absent

        def _boom(*args: object, **kwargs: object) -> int:
            raise AssertionError("underlying outbound main must not run when disabled")

        monkeypatch.setattr("sase_telegram.scripts.sase_tg_outbound.main", _boom)

        assert outbound_main() == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_enabled_delegates_and_returns_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        _enable(tmp_path)

        calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def _fake(*args: object, **kwargs: object) -> int:
            calls.append((args, kwargs))
            return 3

        monkeypatch.setattr("sase_telegram.scripts.sase_tg_outbound.main", _fake)

        assert outbound_main("--dry-run") == 3
        assert calls == [(("--dry-run",), {})]

    def test_enabled_credential_error_prints_single_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _point_home(monkeypatch, tmp_path)
        _enable(tmp_path)
        message = "Telegram bot token unavailable: set SASE_TELEGRAM_BOT_TOKEN."

        def _boom(*args: object, **kwargs: object) -> int:
            raise TelegramCredentialError(message)

        monkeypatch.setattr("sase_telegram.scripts.sase_tg_outbound.main", _boom)

        assert outbound_main("--dry-run") == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == f"{message}\n"
        assert "Traceback" not in captured.err
