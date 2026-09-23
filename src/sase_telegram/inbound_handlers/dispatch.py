"""Per-update routing and poll-and-dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from sase_telegram import pending_actions, telegram_client
from sase_telegram.custom_commands import CustomCommand
from sase_telegram.inbound import save_offset
from sase_telegram.inbound_handlers.common import _message_chat_id, _configured_chat_id
from sase_telegram.inbound_handlers.images import (
    _media_group_id,
    _stage_media_group_image,
    _handle_photo_message,
    _handle_document_image,
)
from sase_telegram.inbound_handlers.callbacks import _handle_callback
from sase_telegram.inbound_handlers.text_messages import _handle_text_message

import logging

log = logging.getLogger(__name__)


def _callback_query_sender_id(callback_query: Any) -> str | None:
    user = getattr(callback_query, "from_user", None)
    user_id = getattr(user, "id", None) if user is not None else None
    return str(user_id) if user_id is not None else None


def _update_is_from_configured_chat(update: Any) -> bool:
    """Return whether *update* comes from the configured chat (and sender).

    Any Telegram account that discovers the bot can message or tap a
    callback button on it; nothing before this checked identity, so every
    handler -- including agent launch -- was reachable by a stranger. When
    the configured chat id itself cannot be resolved there is nothing
    trustworthy to compare against, so every update is rejected: fail
    closed rather than letting strangers through unfiltered.
    """
    configured = _configured_chat_id()
    if configured is None:
        log.warning(
            "Rejecting Telegram update (update_id=%d) because the chat id "
            "is not configured",
            getattr(update, "update_id", -1),
        )
        return False
    callback_query = update.callback_query
    if callback_query is not None:
        chat_id = _message_chat_id(getattr(callback_query, "message", None))
        sender_id = _callback_query_sender_id(callback_query)
        return chat_id == configured and sender_id == configured
    message = update.message
    if message is not None:
        return _message_chat_id(message) == configured
    return True


def _dispatch_one_update(
    update: Any, custom_commands: dict[str, CustomCommand] | None
) -> str | None:
    """Process one Telegram update; return its summary bucket, if any.

    Reloads pending actions fresh for each callback instead of sharing one
    snapshot across a whole poll batch, since the long-poll receiver can
    return much larger batches than the old zero-timeout poll ever did --
    a stale batch-start snapshot would make a later update in the same
    batch miss an action removed by an earlier one.
    """
    if not _update_is_from_configured_chat(update):
        log.warning(
            "Rejecting Telegram update from an unauthorized chat (update_id=%d)",
            update.update_id,
        )
        return None
    if update.callback_query:
        log.info("Processing callback (update_id=%d)", update.update_id)
        _handle_callback(update.callback_query, pending_actions.list_all())
        return "callback"
    if update.message:
        msg = update.message
        if msg.photo:
            if _media_group_id(msg):
                log.info("Staging grouped photo message")
                _stage_media_group_image(msg, "photo")
            else:
                log.info("Processing photo message")
                _handle_photo_message(msg)
            return "photo"
        if (
            msg.document
            and msg.document.mime_type
            and msg.document.mime_type.startswith("image/")
        ):
            if _media_group_id(msg):
                log.info("Staging grouped document image: %s", msg.document.file_name)
                _stage_media_group_image(msg, "document")
            else:
                log.info("Processing document image: %s", msg.document.file_name)
                _handle_document_image(msg)
            return "document"
        if msg.text:
            log.info("Processing text message (update_id=%d)", update.update_id)
            _handle_text_message(msg, custom_commands)
            return "text"
        log.info("Skipping unsupported message type (update_id=%d)", update.update_id)
        return "unsupported"
    return None


@dataclass(frozen=True)
class _PollResult:
    updates: list[Any]
    next_offset: int | None
    counts: dict[str, int]


def _poll_and_dispatch_updates(
    offset: int | None,
    *,
    timeout: int,
    custom_commands: dict[str, CustomCommand] | None,
) -> _PollResult:
    """Fetch one batch of updates and dispatch each, durably, in order.

    The offset advances after each update finishes (successfully or with a
    caught, logged error) instead of once for the whole batch, so a killed
    receiver never silently loses an update that arrived but was not yet
    claimed -- redelivery on restart resumes at exactly the first update
    this process never got to. A single update whose handler raises is
    logged and skipped (offset still advances past it) rather than wedging
    every later update behind it forever, matching a real process crash's
    effective behavior today.
    """
    updates = telegram_client.get_updates(offset=offset, timeout=timeout)
    return _dispatch_fetched_updates(
        updates, offset=offset, custom_commands=custom_commands
    )


def _reply_dispatch_failure(update: Any, exc: BaseException) -> None:
    """Best-effort reply when a message update's handler raises.

    Only message-carrying updates get a reply, in the originating chat, so
    the sender learns the message was not processed and can resend it.
    Callbacks already answer their own queries. Send failures are swallowed
    and logged.
    """
    message = getattr(update, "message", None)
    if message is None:
        return
    chat_id = _message_chat_id(message)
    if chat_id is None:
        return
    detail = " ".join(str(exc).split()) or exc.__class__.__name__
    try:
        telegram_client.send_message(
            chat_id,
            f"Could not process your message ({detail}). Please resend it.",
        )
    except Exception:
        log.warning("Failed to send Telegram dispatch-failure reply", exc_info=True)


def _dispatch_fetched_updates(
    updates: list[Any],
    *,
    offset: int | None,
    custom_commands: dict[str, CustomCommand] | None,
) -> _PollResult:
    """Dispatch an already-fetched batch, advancing the offset per update."""
    counts = {"callback": 0, "text": 0, "photo": 0, "document": 0, "unsupported": 0}
    next_offset: int | None = None
    if updates:
        log.info("Received %d update(s) (offset=%s)", len(updates), offset)
        for update in updates:
            try:
                bucket = _dispatch_one_update(update, custom_commands)
            except Exception as exc:
                log.warning(
                    "Failed to process Telegram update_id=%d",
                    update.update_id,
                    exc_info=True,
                )
                _reply_dispatch_failure(update, exc)
                bucket = None
            if bucket is not None:
                counts[bucket] += 1
            next_offset = update.update_id + 1
            save_offset(next_offset)
    return _PollResult(updates=updates, next_offset=next_offset, counts=counts)
