"""Credential retrieval for the Telegram bot."""

import functools
import os
import subprocess


@functools.lru_cache(maxsize=1)
def get_bot_token() -> str:
    """Retrieve the Telegram bot token from the password store."""
    result = subprocess.run(
        ["pass", "show", "telegram_sase_bot_token"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


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
