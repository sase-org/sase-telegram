"""Credential retrieval for the Telegram bot."""

from __future__ import annotations

import functools
import json
import logging
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

_BOT_TOKEN_ENV_VAR = "SASE_TELEGRAM_BOT_TOKEN"
_BOT_TOKEN_FILE = Path(".sase") / "telegram_bot_token"
_PASS_TOKEN_CMD = ["pass", "show", "telegram_sase_bot_token"]

#: How long a failed token lookup is remembered across processes. The
#: outbound job is a fresh process every few seconds, so without this a broken
#: ``pass``/gpg setup reran ``pass show`` on every tick. A token that becomes
#: available through ``pass`` is picked up once the window expires; the env
#: var and token file are cheap and are always checked first.
BOT_TOKEN_NEGATIVE_CACHE_SECONDS = 60.0
_BOT_TOKEN_NEGATIVE_CACHE_FILE = (
    Path(".sase") / "telegram" / "bot_token_unavailable.json"
)
_NEGATIVE_CACHE_VERSION = 1


class TelegramCredentialError(RuntimeError):
    """Raised when Telegram credentials are unavailable or unusable."""


def telegram_bot_token_file_path() -> Path:
    """Return the default file path for the Telegram bot token."""
    return Path.home() / _BOT_TOKEN_FILE


def bot_token_negative_cache_path() -> Path:
    """Return the file that remembers a recent failed token lookup."""
    return Path.home() / _BOT_TOKEN_NEGATIVE_CACHE_FILE


@functools.lru_cache(maxsize=1)
def get_bot_token() -> str:
    """Retrieve the Telegram bot token from env, file, or the password store.

    A failed ``pass`` lookup is remembered for
    :data:`BOT_TOKEN_NEGATIVE_CACHE_SECONDS`, across processes: until then
    later calls still check the env var and token file but reuse the
    remembered ``pass`` failure instead of rerunning ``pass``/gpg.
    """
    failures: list[str] = []

    env_token = os.environ.get(_BOT_TOKEN_ENV_VAR, "").strip()
    if env_token:
        return env_token
    failures.append(f"{_BOT_TOKEN_ENV_VAR} is unset")

    file_token = _get_token_from_file(failures)
    if file_token:
        return file_token

    cached_pass_failures = _recent_pass_failures()
    if cached_pass_failures is not None:
        failures.extend(cached_pass_failures)
        raise TelegramCredentialError(_token_unavailable_message(failures))

    pass_failures: list[str] = []
    pass_token = _get_token_from_pass(pass_failures)
    if pass_token:
        _forget_pass_failures()
        return pass_token

    failures.extend(pass_failures)
    _remember_pass_failures(pass_failures)
    raise TelegramCredentialError(_token_unavailable_message(failures))


def _recent_pass_failures(now: float | None = None) -> list[str] | None:
    """Return a remembered ``pass`` failure that is still inside its window."""
    try:
        raw = bot_token_negative_cache_path().read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _NEGATIVE_CACHE_VERSION
    ):
        return None
    failed_at = payload.get("failed_at")
    pass_failures = payload.get("pass_failures")
    if not isinstance(failed_at, (int, float)) or not isinstance(pass_failures, list):
        return None
    if not all(isinstance(item, str) for item in pass_failures):
        return None
    age = (time.time() if now is None else now) - failed_at
    # A record from the future (a clock change) counts as expired.
    if not 0 <= age < BOT_TOKEN_NEGATIVE_CACHE_SECONDS:
        return None
    return list(pass_failures)


def _remember_pass_failures(pass_failures: list[str], now: float | None = None) -> None:
    path = bot_token_negative_cache_path()
    payload = {
        "version": _NEGATIVE_CACHE_VERSION,
        "failed_at": time.time() if now is None else now,
        "pass_failures": pass_failures,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        log.debug("Could not remember the Telegram token lookup failure: %s", exc)


def _forget_pass_failures() -> None:
    try:
        bot_token_negative_cache_path().unlink(missing_ok=True)
    except OSError as exc:
        log.debug("Could not clear the Telegram token lookup failure: %s", exc)


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
    if value is None or not value.strip():
        raise TelegramCredentialError(
            "SASE_TELEGRAM_BOT_CHAT_ID environment variable is not set (or is blank): "
            "the service-host receiver needs it under "
            "service.procs.telegram_receiver.env, because it does not inherit "
            "AXE routine env."
        )
    return value.strip()


def get_bot_username() -> str:
    """Get the Telegram bot username from the SASE_TELEGRAM_BOT_USERNAME env var."""
    value = os.environ.get("SASE_TELEGRAM_BOT_USERNAME")
    if value is None or not value.strip():
        raise TelegramCredentialError(
            "SASE_TELEGRAM_BOT_USERNAME environment variable is not set (or is blank): "
            "the service-host receiver needs it under "
            "service.procs.telegram_receiver.env, because it does not inherit "
            "AXE routine env."
        )
    return value.strip()
