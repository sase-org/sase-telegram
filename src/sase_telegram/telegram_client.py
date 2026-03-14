"""Sync wrapper around the async python-telegram-bot library."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from pathlib import Path
from typing import Any, Callable

from telegram import Bot, InlineKeyboardMarkup, Message, Update
from telegram.error import NetworkError, RetryAfter, TimedOut

from sase_telegram.credentials import get_bot_token

log = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds


def _run_async(coro: Any) -> Any:
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _with_retry(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that retries on transient Telegram errors.

    Handles RetryAfter (flood control), TimedOut, and NetworkError
    with appropriate backoff between retries.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except RetryAfter as e:
                if attempt == _MAX_RETRIES:
                    raise
                wait = e.retry_after + 1
                log.warning(
                    "Rate limited by Telegram, retrying in %ds (attempt %d/%d)",
                    wait,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(wait)
            except (TimedOut, NetworkError) as e:
                if attempt == _MAX_RETRIES:
                    raise
                wait = _RETRY_BACKOFF_BASE * (attempt + 1)
                log.warning(
                    "Transient Telegram error (%s), retrying in %.1fs (attempt %d/%d)",
                    type(e).__name__,
                    wait,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(wait)
        raise RuntimeError("unreachable")

    return wrapper


def _get_bot() -> Bot:
    """Create a Bot instance with the stored token."""
    return Bot(token=get_bot_token())


@_with_retry
def send_message(
    chat_id: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> Message:
    """Send a text message to a Telegram chat."""
    bot = _get_bot()
    try:
        return _run_async(
            bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        )
    except Exception:
        if parse_mode:
            log.warning(
                "Failed to send with parse_mode=%s, falling back to plain text",
                parse_mode,
                exc_info=True,
            )
            return _run_async(
                bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=reply_markup
                )
            )
        raise


@_with_retry
def send_document(
    chat_id: str,
    document: str | bytes,
    caption: str | None = None,
) -> Message:
    """Send a document to a Telegram chat."""
    bot = _get_bot()
    return _run_async(
        bot.send_document(chat_id=chat_id, document=document, caption=caption)
    )


@_with_retry
def get_updates(offset: int | None = None, timeout: int = 0) -> list[Update]:
    """Fetch updates (new messages/callbacks) from the Telegram API."""
    bot = _get_bot()
    return _run_async(bot.get_updates(offset=offset, timeout=timeout))


@_with_retry
def answer_callback_query(callback_query_id: str, text: str | None = None) -> bool:
    """Answer a callback query from an inline keyboard button press."""
    bot = _get_bot()
    return _run_async(
        bot.answer_callback_query(callback_query_id=callback_query_id, text=text)
    )


@_with_retry
def edit_message_reply_markup(
    chat_id: str,
    message_id: int,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | bool:
    """Edit the reply markup of an existing message."""
    bot = _get_bot()
    return _run_async(
        bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
        )
    )


@_with_retry
def download_file(file_id: str, destination: Path) -> Path:
    """Download a Telegram file to a local path."""
    bot = _get_bot()

    async def _download() -> Path:
        file_obj = await bot.get_file(file_id)
        await file_obj.download_to_drive(custom_path=destination)
        return destination

    return _run_async(_download())
