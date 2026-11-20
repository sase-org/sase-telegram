"""Sync wrapper around the async python-telegram-bot library."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from telegram import Bot, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from sase_telegram.credentials import get_bot_token

log = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # seconds
_MAX_MESSAGE_LENGTH = 4096

# Lower-cased fragments of Telegram's entity-parse BadRequest descriptions
# (PTB strips the "Bad Request: " prefix). Only these trigger the plain-text
# fallback; every other failure propagates unchanged.
_ENTITY_PARSE_ERROR_MARKERS = (
    "can't parse entities",
    "can't parse message text",
    "can't find end of the entity",
)

_DELIVERED_CHUNKS_ATTR = "sase_telegram_delivered_chunks"


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
                retry_after = e.retry_after
                if hasattr(retry_after, "total_seconds"):
                    wait = retry_after.total_seconds() + 1
                else:
                    wait = float(retry_after) + 1
                log.warning(
                    "Rate limited by Telegram, retrying in %.1fs (attempt %d/%d)",
                    wait,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(wait)
            except BadRequest:
                raise
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


def _split_message(text: str, limit: int = _MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a message into chunks that fit within Telegram's character limit.

    Splits on newline boundaries first, then word boundaries.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to split at the last newline before the limit
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            # No newline found — try splitting at a space
            split_at = text.rfind(" ", 0, limit)
        if split_at == -1:
            # No good break point — hard split
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _is_entity_parse_error(exc: BaseException) -> bool:
    """Return whether *exc* is Telegram rejecting the message's formatting."""
    if not isinstance(exc, BadRequest):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _ENTITY_PARSE_ERROR_MARKERS)


def _call_with_entity_fallback(
    call: Callable[..., Coroutine[Any, Any, Any]],
    kwargs: dict[str, Any],
    *,
    action: str,
) -> Any:
    """Run one Telegram text call under ``_with_retry`` with a parse fallback.

    *call* receives a fresh ``Bot`` followed by *kwargs*. When *kwargs* has a
    ``parse_mode`` and Telegram answers with an entity-parse ``BadRequest``,
    the call is repeated without ``parse_mode``. The fallback is sticky across
    retry attempts, so a transient error after it never re-sends the formatted
    text. Every other error -- other ``BadRequest``s, ``TimedOut``,
    ``NetworkError``, ``RetryAfter``, ``Forbidden``, ``InvalidToken``, and
    credential errors from ``_get_bot()`` -- reaches the retry wrapper and the
    caller unchanged.
    """
    parse_mode = kwargs.get("parse_mode")
    plain_kwargs = {key: value for key, value in kwargs.items() if key != "parse_mode"}
    use_plain = False

    def attempt() -> Any:
        nonlocal use_plain
        if not use_plain:
            bot = _get_bot()
            try:
                return _run_async(call(bot, **kwargs))
            except BadRequest as exc:
                if not parse_mode or not _is_entity_parse_error(exc):
                    raise
                log.warning(
                    "Telegram rejected parse_mode=%s for %s (%s); "
                    "falling back to plain text",
                    parse_mode,
                    action,
                    exc,
                )
                use_plain = True
        # Use a fresh Bot instance -- the previous asyncio.run() closed its
        # event loop, which can leave the old Bot's internal httpx client in
        # a broken state (python-telegram-bot v21+).
        return _run_async(call(_get_bot(), **plain_kwargs))

    return _with_retry(attempt)()


def _send_chunk(
    chat_id: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
    reply_to_message_id: int | None = None,
    disable_notification: bool | None = None,
) -> Message:
    """Send one message-sized chunk with its own retries and parse fallback."""
    kwargs: dict[str, object] = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": reply_markup,
        "parse_mode": parse_mode,
        "reply_to_message_id": reply_to_message_id,
    }
    if disable_notification is not None:
        kwargs["disable_notification"] = disable_notification
    # Drop Nones so older client libraries without the flag keep working.
    kwargs = {key: value for key, value in kwargs.items() if value is not None}
    message: Message = _call_with_entity_fallback(
        lambda bot, **kw: bot.send_message(**kw), kwargs, action="send_message"
    )
    return message


def delivered_chunks(exc: BaseException) -> int | None:
    """Return how many chunks a failed :func:`send_message` had delivered.

    The count is the index of the first undelivered chunk of the full text,
    so it can be passed back as ``start_chunk`` to resume. Returns ``None``
    when *exc* did not come out of :func:`send_message`'s chunk loop.
    """
    count = getattr(exc, _DELIVERED_CHUNKS_ATTR, None)
    return count if isinstance(count, int) else None


def send_message(
    chat_id: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
    reply_to_message_id: int | None = None,
    disable_notification: bool | None = None,
    *,
    start_chunk: int = 0,
) -> Message:
    """Send a text message to a Telegram chat.

    Messages exceeding Telegram's 4096-character limit are automatically
    split into multiple messages.  Only the last chunk carries the
    ``reply_markup`` so that inline keyboards appear once at the end.
    ``disable_notification`` sends quietly across split/fallback sends.

    Each chunk is retried on its own, so a delivered chunk is never re-sent.
    When a chunk exhausts its retries, its error propagates with its type
    unchanged and the number of chunks delivered so far attached: read it
    with :func:`delivered_chunks` (a PEP 678 note repeats it in tracebacks).
    Passing that count as *start_chunk* resumes at the first undelivered
    chunk.
    """
    chunks = _split_message(text)
    if not 0 <= start_chunk < len(chunks):
        raise ValueError(
            f"start_chunk={start_chunk} is outside the {len(chunks)} chunk(s) "
            "of this message"
        )
    last_msg: Message | None = None
    for index in range(start_chunk, len(chunks)):
        # Only attach reply_markup to the last chunk
        markup = reply_markup if index == len(chunks) - 1 else None
        try:
            last_msg = _send_chunk(
                chat_id=chat_id,
                text=chunks[index],
                reply_markup=markup,
                parse_mode=parse_mode,
                reply_to_message_id=reply_to_message_id,
                disable_notification=disable_notification,
            )
        except Exception as exc:
            setattr(exc, _DELIVERED_CHUNKS_ATTR, index)
            exc.add_note(
                f"sase-telegram: {index} of {len(chunks)} message chunk(s) "
                "were delivered before this failure"
            )
            raise
    assert last_msg is not None  # noqa: S101 — chunks is never empty
    return last_msg


@_with_retry
def send_document(
    chat_id: str,
    document: str | bytes,
    caption: str | None = None,
    parse_mode: str | None = None,
    filename: str | None = None,
) -> Message:
    """Send a document to a Telegram chat."""
    bot = _get_bot()
    return _run_async(
        bot.send_document(
            chat_id=chat_id,
            document=document,
            caption=caption,
            parse_mode=parse_mode,
            filename=filename,
        )
    )


@_with_retry
def send_photo(
    chat_id: str,
    photo: str | bytes,
    caption: str | None = None,
) -> Message:
    """Send a photo to a Telegram chat (renders inline in chat)."""
    bot = _get_bot()
    return _run_async(bot.send_photo(chat_id=chat_id, photo=photo, caption=caption))


@_with_retry
def send_animation(
    chat_id: str,
    animation: str | bytes,
    caption: str | None = None,
) -> Message:
    """Send an animation to a Telegram chat (renders inline in chat)."""
    bot = _get_bot()
    return _run_async(
        bot.send_animation(chat_id=chat_id, animation=animation, caption=caption)
    )


@_with_retry
def send_video(
    chat_id: str,
    video: str | bytes,
    caption: str | None = None,
) -> Message:
    """Send a video to a Telegram chat (renders inline in chat)."""
    bot = _get_bot()
    return _run_async(bot.send_video(chat_id=chat_id, video=video, caption=caption))


@_with_retry
def send_audio(
    chat_id: str,
    audio: str | bytes,
    caption: str | None = None,
    title: str | None = None,
    performer: str | None = None,
    duration: int | None = None,
) -> Message:
    """Send an audio file to a Telegram chat (lands in the music player)."""
    bot = _get_bot()
    return _run_async(
        bot.send_audio(
            chat_id=chat_id,
            audio=audio,
            caption=caption,
            title=title,
            performer=performer,
            duration=duration,
            write_timeout=120,
        )
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


def _is_message_not_modified(exc: BaseException) -> bool:
    """Return whether *exc* is Telegram's not-modified BadRequest."""
    message = str(exc)
    chained = exc.__cause__ or exc.__context__
    if chained is not None:
        message += f" {chained}"
    return "message is not modified" in message.lower()


def edit_message_text(
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> Message | bool:
    """Edit an existing message's text, falling back to plain text on parse errors.

    An already-identical message counts as success and returns ``True``.
    """
    try:
        result: Message | bool = _call_with_entity_fallback(
            lambda bot, **kw: bot.edit_message_text(**kw),
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            },
            action="edit_message_text",
        )
    except BadRequest as exc:
        if _is_message_not_modified(exc):
            return True
        raise
    return result


@_with_retry
def set_my_commands(commands: list[tuple[str, str]]) -> bool:
    """Register bot commands for the Telegram auto-complete menu."""
    from telegram import BotCommand

    bot = _get_bot()
    bot_commands = [BotCommand(cmd, desc) for cmd, desc in commands]
    return _run_async(bot.set_my_commands(bot_commands))


@_with_retry
def download_file(file_id: str, destination: Path) -> Path:
    """Download a Telegram file to a local path."""
    bot = _get_bot()

    async def _download() -> Path:
        file_obj = await bot.get_file(file_id)
        await file_obj.download_to_drive(custom_path=destination)
        return destination

    return _run_async(_download())
