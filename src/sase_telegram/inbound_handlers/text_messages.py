"""Text-message router and stale feedback cleanup."""

from __future__ import annotations

from typing import Any
from sase_telegram import pending_actions, telegram_client
from sase_telegram.custom_commands import CustomCommand
from sase.notification_gates.models import GateError
from sase_telegram.gate_flow import (
    clear_progress as clear_gate_progress,
    load_gate_view,
)
from sase_telegram.inbound import (
    clear_awaiting_feedback,
    clear_awaiting_feedback_by_prefix,
    load_awaiting_feedback,
    normalize_launch_xprompt_at_refs,
    process_text_message,
    reconstruct_code_markers,
)
from sase_telegram.inbound_handlers.common import (
    _STALE_AWAITING_FEEDBACK_TEXT,
    _telegram_agent_launches_disabled,
    _message_chat_id,
    _configured_chat_id,
    _message_id,
    _resolve_response,
    _gate_error_answer_text,
    _send_confirmation,
    _clear_awaiting_feedback_entry,
)
from sase_telegram.inbound_handlers.project_context import _record_project_context
from sase_telegram.inbound_handlers.agent_launch import _launch_agent
from sase_telegram.inbound_handlers.questions import _handle_question_text_message
from sase_telegram.inbound_handlers.gate_input_steps import (
    _handle_gate_input_text_message,
)
from sase_telegram.inbound_handlers.commands import _handle_command

import logging

log = logging.getLogger(__name__)


def _awaiting_feedback_prefix(reply_key: str | None) -> str | None:
    awaiting = load_awaiting_feedback(reply_key)
    if not isinstance(awaiting, dict):
        return None

    prefix = awaiting.get("prefix")
    if not isinstance(prefix, str) or not prefix:
        return None
    return prefix


def _pending_action_exists(prefix: str) -> bool:
    try:
        return pending_actions.get(prefix) is not None
    except Exception:
        log.warning(
            "Failed to load pending action for awaiting-feedback cleanup",
            exc_info=True,
        )
        return True


def _clear_stale_awaiting_feedback_entry(reply_key: str | None, prefix: str) -> None:
    if reply_key is not None:
        clear_awaiting_feedback(reply_key)
    clear_awaiting_feedback_by_prefix(prefix)


def _clear_stale_awaiting_feedback(reply_key: str | None) -> str | None:
    prefix = _awaiting_feedback_prefix(reply_key)
    if prefix is None or _pending_action_exists(prefix):
        return None

    _clear_stale_awaiting_feedback_entry(reply_key, prefix)
    return prefix


def _send_stale_awaiting_feedback_reply(message: Any) -> None:
    chat_id = _message_chat_id(message) or _configured_chat_id()
    if chat_id is None:
        return

    kwargs: dict[str, Any] = {}
    message_id = _message_id(message)
    if message_id:
        kwargs["reply_to_message_id"] = message_id
    try:
        telegram_client.send_message(
            chat_id,
            _STALE_AWAITING_FEEDBACK_TEXT,
            **kwargs,
        )
    except Exception:
        log.warning("Failed to send stale feedback reply", exc_info=True)


def _handle_text_message(
    message: Any,
    custom_commands: dict[str, CustomCommand] | None = None,
) -> None:
    """Handle a text message: command dispatch, feedback, or new agent launch."""
    text = reconstruct_code_markers(message.text, message.entities)
    reply_to = getattr(message, "reply_to_message", None)
    reply_key = (
        str(reply_to.message_id)
        if reply_to is not None and getattr(reply_to, "message_id", None) is not None
        else None
    )

    # Slash commands are user-visible commands even when an old two-step
    # feedback flow is still recorded.
    if text.startswith("/"):
        _clear_stale_awaiting_feedback(reply_key)
        if custom_commands is None:
            _handle_command(text, message)
        else:
            _handle_command(text, message, custom_commands)
        return

    stale_prefix = _clear_stale_awaiting_feedback(reply_key)
    if stale_prefix is not None and reply_key is not None:
        _send_stale_awaiting_feedback_reply(message)
        return

    if _handle_question_text_message(message, text, reply_key=reply_key):
        return

    if _handle_gate_input_text_message(message, text, reply_key=reply_key):
        return

    response = process_text_message(text, key=reply_key)
    if response is not None:
        action = pending_actions.get(response.notif_id_prefix)
        try:
            _resolve_response(response, action)
        except GateError as exc:
            chat_id = _message_chat_id(message) or _configured_chat_id()
            if chat_id is not None:
                telegram_client.send_message(
                    chat_id,
                    _gate_error_answer_text(exc),
                    reply_to_message_id=message.message_id,
                )
            if exc.code in {
                "already_answered",
                "gate_cancelled",
                "not_found",
            }:
                pending_actions.remove(response.notif_id_prefix)
                _clear_awaiting_feedback_entry(reply_key, response.notif_id_prefix)
            return
        # Clear only the matched awaiting entry — leaves other concurrent
        # flows intact.
        _clear_awaiting_feedback_entry(reply_key, response.notif_id_prefix)
        pending_actions.remove(response.notif_id_prefix)
        action_data = action.get("action_data") if isinstance(action, dict) else None
        if isinstance(action_data, dict) and action_data.get("bundle_path"):
            try:
                clear_gate_progress(load_gate_view(action_data))
            except GateError:
                pass
        _send_confirmation(response, message.message_id)
        return

    if _telegram_agent_launches_disabled():
        log.info("Ignoring Telegram text launch because agent launches are disabled")
        return

    # Launch a new agent with this text as the prompt
    text = normalize_launch_xprompt_at_refs(text)
    _record_project_context(text, message)
    _launch_agent(text)
