"""Credential retrieval for the Telegram bot."""

from __future__ import annotations

import functools
import os
import stat
import subprocess
from pathlib import Path

_BOT_TOKEN_ENV_VAR = "SASE_TELEGRAM_BOT_TOKEN"
_BOT_TOKEN_FILE = Path(".sase") / "telegram_bot_token"
_PASS_TOKEN_CMD = ["pass", "show", "telegram_sase_bot_token"]


class TelegramCredentialError(RuntimeError):
    """Raised when Telegram credentials are unavailable or unusable."""


def telegram_bot_token_file_path() -> Path:
    """Return the default file path for the Telegram bot token."""
    return Path.home() / _BOT_TOKEN_FILE


@functools.lru_cache(maxsize=1)
def get_bot_token() -> str:
    """Retrieve the Telegram bot token from env, file, or the password store."""
    failures: list[str] = []

    env_token = os.environ.get(_BOT_TOKEN_ENV_VAR, "").strip()
    if env_token:
        return env_token
    failures.append(f"{_BOT_TOKEN_ENV_VAR} is unset")

    file_token = _get_token_from_file(failures)
    if file_token:
        return file_token

    pass_token = _get_token_from_pass(failures)
    if pass_token:
        return pass_token

    raise TelegramCredentialError(_token_unavailable_message(failures))


def _get_token_from_file(failures: list[str]) -> str | None:
    token_path = telegram_bot_token_file_path()
    display_path = "~/.sase/telegram_bot_token"

    try:
        token_stat = token_path.stat()
    except FileNotFoundError:
        failures.append(f"{display_path} does not exist")
        return None
    except OSError as exc:
        failures.append(f"{display_path} could not be inspected: {exc}")
        return None

    if not stat.S_ISREG(token_stat.st_mode):
        failures.append(f"{display_path} is not a regular file")
        return None
    if token_stat.st_mode & (stat.S_IRGRP | stat.S_IROTH):
        failures.append(f"{display_path} is group/other-readable; run chmod 600")
        return None

    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        failures.append(f"{display_path} could not be read: {exc}")
        return None
    if not token:
        failures.append(f"{display_path} is empty")
        return None
    return token


def _get_token_from_pass(failures: list[str]) -> str | None:
    try:
        result = subprocess.run(
            _PASS_TOKEN_CMD,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        failures.append("pass executable was not found")
        return None
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if isinstance(exc.stderr, str) else ""
        detail = f": {stderr}" if stderr else ""
        failures.append(f"`pass show telegram_sase_bot_token` failed{detail}")
        return None

    token = result.stdout.strip()
    if not token:
        failures.append("`pass show telegram_sase_bot_token` returned an empty token")
        return None
    return token


def _token_unavailable_message(failures: list[str]) -> str:
    options = (
        "set SASE_TELEGRAM_BOT_TOKEN, create ~/.sase/telegram_bot_token with mode "
        "600, or install pass and make `pass show telegram_sase_bot_token` work"
    )
    return f"Telegram bot token unavailable: {options}. Checked: {'; '.join(failures)}."


def get_chat_id() -> str:
    """Get the Telegram chat ID from the SASE_TELEGRAM_BOT_CHAT_ID env var."""
    value = os.environ.get("SASE_TELEGRAM_BOT_CHAT_ID")
    if not value:
        raise RuntimeError("SASE_TELEGRAM_BOT_CHAT_ID environment variable is not set")
    return value


def get_bot_username() -> str:
    """Get the Telegram bot username from the SASE_TELEGRAM_BOT_USERNAME env var."""
    value = os.environ.get("SASE_TELEGRAM_BOT_USERNAME")
    if not value:
        raise RuntimeError("SASE_TELEGRAM_BOT_USERNAME environment variable is not set")
    return value
