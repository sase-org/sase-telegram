"""Shared chat/message/callback primitives for Telegram inbound."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from sase_telegram import credentials, pending_actions, telegram_client
from telegram import InlineKeyboardMarkup
from sase.notification_gates.models import GateError
from sase_telegram.inbound import (
    ResponseAction,
    clear_awaiting_feedback,
    clear_awaiting_feedback_by_prefix,
    confirmation_text,
    resolve_gate_response,
    resolve_user_question_response,
)

import logging

log = logging.getLogger(__name__)


_COPY_TEXT_MAX = 256  # Telegram CopyTextButton character limit


_LAUNCH_AGENTS_DISABLED_ENV = "SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED"


_STALE_AWAITING_FEEDBACK_TEXT = "This action has already been handled"


def _telegram_agent_launches_disabled() -> bool:
    return _LAUNCH_AGENTS_DISABLED_ENV in os.environ


def _message_chat_id(message: Any | None) -> str | None:
    if message is None:
        return None
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None) if chat is not None else None
    if chat_id is None:
        chat_id = getattr(message, "chat_id", None)
    return str(chat_id) if chat_id is not None else None


def _configured_chat_id() -> str | None:
    try:
        chat_id = credentials.get_chat_id()
    except Exception:
        return None
    return str(chat_id) if chat_id is not None else None


def _context_chat_id(message: Any | None) -> str | None:
    return _message_chat_id(message) or _configured_chat_id()


def _message_id(message: Any) -> int:
    raw = getattr(message, "message_id", 0)
    if isinstance(raw, int):
        return raw
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _write_response(response: ResponseAction) -> None:
    """Write a response JSON file to disk."""
    response.response_path.parent.mkdir(parents=True, exist_ok=True)
    response.response_path.write_text(json.dumps(response.response_data, indent=2))


def _resolve_response(
    response: ResponseAction, action: dict[str, Any] | None
) -> str | None:
    if response.action_type == "gate":
        return resolve_gate_response(response, action)
    if response.action_type == "question":
        return resolve_user_question_response(response, action)
    _write_response(response)
    return response.answer_text


def _action_message_id(action: dict[str, Any] | None) -> int | None:
    if not action:
        return None
    raw = action.get("message_id")
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _callback_origin_message_id(
    callback_query: Any, action: dict[str, Any] | None
) -> int | None:
    message = getattr(callback_query, "message", None)
    message_id = _message_id(message) if message is not None else 0
    return message_id or _action_message_id(action)


def _callback_chat_id(callback_query: Any, action: dict[str, Any] | None) -> str | None:
    message = getattr(callback_query, "message", None)
    chat_id = _message_chat_id(message)
    if chat_id is not None:
        return chat_id
    if action and action.get("chat_id") is not None:
        return str(action["chat_id"])
    return _configured_chat_id()


def _answer_callback(callback_query: Any | None, text: str | None) -> None:
    if callback_query is None:
        return
    try:
        telegram_client.answer_callback_query(callback_query.id, text)
    except Exception:
        log.warning("Failed to answer Telegram callback", exc_info=True)


def _gate_error_answer_text(exc: GateError) -> str:
    if exc.code in {"already_answered", "gate_cancelled", "not_found"}:
        return _STALE_AWAITING_FEEDBACK_TEXT
    if exc.code in {"invalid_request", "missing_gate", "stale", "expired"}:
        return "This request has expired"
    return f"Gate response failed: {exc}"


def _answer_resolved_callback(
    callback_query: Any,
    action: dict[str, Any],
    prefix: str,
    resolution: str,
) -> None:
    """Answer + dismiss a callback whose action was resolved elsewhere."""
    message = (
        "This request has expired"
        if resolution == "stale"
        else "This action has already been handled"
    )
    try:
        telegram_client.answer_callback_query(callback_query.id, message)
    except Exception:
        pass
    message_id = action.get("message_id")
    chat_id = action.get("chat_id")
    if message_id is not None and chat_id is not None:
        try:
            telegram_client.edit_message_reply_markup(
                chat_id, message_id, reply_markup=None
            )
        except Exception:
            pass
    pending_actions.remove(prefix)
    clear_awaiting_feedback_by_prefix(prefix)


def _load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        log.warning("Failed to load JSON file: %s", path, exc_info=True)
        return None


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _shorten_home(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home + os.sep) else path


def _send_html_chunks(
    chat_id: str,
    chunks: list[str],
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    for index, chunk in enumerate(chunks):
        telegram_client.send_message(
            chat_id,
            chunk,
            parse_mode="HTML",
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )


def _send_confirmation(response: ResponseAction, message_id: int) -> None:
    """Send a confirmation reply to the user's feedback/answer message."""
    try:
        chat_id = credentials.get_chat_id()
        telegram_client.send_message(
            chat_id,
            confirmation_text(response),
            reply_to_message_id=message_id,
        )
    except Exception:
        log.warning("Failed to send confirmation reply", exc_info=True)


def _clear_awaiting_feedback_entry(reply_key: str | None, prefix: str) -> None:
    if reply_key is not None:
        clear_awaiting_feedback(reply_key)
    else:
        clear_awaiting_feedback_by_prefix(prefix)
