"""Gate response submission and input prompts."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from sase_telegram import pending_actions, telegram_client
from sase.notification_gates.models import GateError
from sase_telegram.formatting import (
    format_gate_input_prompt,
    render_gate_input_keyboard,
)
from sase_telegram.gate_flow import (
    GateProgress,
    GateView,
    clear_progress as clear_gate_progress,
    save_progress as save_gate_progress,
)
from sase_telegram.gate_inputs import current_step
from sase_telegram.inbound import (
    ResponseAction,
    clear_awaiting_feedback_by_prefix,
    save_awaiting_feedback,
)
from sase_telegram.inbound_handlers.common import (
    _message_chat_id,
    _configured_chat_id,
    _resolve_response,
    _action_message_id,
    _callback_origin_message_id,
    _callback_chat_id,
    _answer_callback,
    _gate_error_answer_text,
    _send_confirmation,
)
from sase_telegram.inbound_handlers.keyboard_cleanup import _dismiss_button_with_retry

import logging

log = logging.getLogger(__name__)


def _dismiss_gate_callback(
    callback_query: Any,
    action: dict[str, Any],
    prefix: str,
) -> None:
    message_id = _action_message_id(action)
    chat_id = _callback_chat_id(callback_query, action)
    _dismiss_button_with_retry(prefix, chat_id, message_id)
    pending_actions.remove(prefix)
    clear_awaiting_feedback_by_prefix(prefix)


def _execute_gate_callback_response(
    callback_query: Any,
    action: dict[str, Any],
    response: ResponseAction,
    view: GateView,
    *,
    message: Any | None = None,
) -> None:
    callback_acknowledged = callback_query is not None
    _answer_callback(callback_query, "Submitting gate answer...")
    try:
        result_message = _resolve_response(response, action)
    except GateError as exc:
        error_text = _gate_error_answer_text(exc)
        if not callback_acknowledged:
            _answer_callback(callback_query, error_text)
        _send_gate_response_error(
            callback_query,
            action,
            error_text,
            message=message,
        )
        if exc.code in {"already_answered", "gate_cancelled", "not_found"}:
            _dismiss_gate_callback(callback_query, action, response.notif_id_prefix)
            clear_gate_progress(view)
        return
    if not callback_acknowledged:
        _answer_callback(callback_query, result_message or "Gate answered")
    if message is not None:
        _send_confirmation(response, message.message_id)
    _dismiss_gate_callback(callback_query, action, response.notif_id_prefix)
    clear_gate_progress(view)


def _send_gate_response_error(
    callback_query: Any | None,
    action: dict[str, Any],
    text: str,
    *,
    message: Any | None,
) -> None:
    """Send a durable-submission error after an early callback acknowledgement."""
    if message is not None:
        chat_id = _message_chat_id(message) or _configured_chat_id()
        reply_to_message_id = message.message_id
    else:
        chat_id = (
            _callback_chat_id(callback_query, action)
            if callback_query is not None
            else action.get("chat_id")
        )
        reply_to_message_id = (
            _callback_origin_message_id(callback_query, action)
            if callback_query is not None
            else _action_message_id(action)
        )
    if chat_id is None:
        return
    try:
        telegram_client.send_message(
            str(chat_id),
            text,
            reply_to_message_id=reply_to_message_id,
        )
    except Exception:
        log.warning("Failed to send gate response error", exc_info=True)


def _begin_gate_feedback(
    callback_query: Any,
    action: dict[str, Any],
    prefix: str,
    view: GateView,
    progress: GateProgress,
    selected_option_ids: tuple[str, ...],
    *,
    option_inputs: dict[str, dict[str, Any]],
) -> None:
    if not selected_option_ids:
        _answer_callback(callback_query, "Select at least one option")
        return
    progress = replace(progress, selected_option_ids=selected_option_ids)
    save_gate_progress(view, progress)
    key = (
        str(progress.active_message_id)
        if progress.active_message_id is not None
        else prefix
    )
    save_awaiting_feedback(
        key,
        prefix,
        {
            "action_type": "gate",
            "bundle_path": str(view.bundle_path),
            "selected_option_ids": list(selected_option_ids),
            "option_inputs": option_inputs,
        },
    )
    _answer_callback(callback_query, "Send the required feedback as a text message")
    message_id = _action_message_id(action)
    chat_id = _callback_chat_id(callback_query, action)
    if message_id is not None and chat_id is not None:
        telegram_client.edit_message_reply_markup(
            chat_id, message_id, reply_markup=None
        )


def _send_gate_input_prompt(
    prefix: str,
    view: GateView,
    progress: GateProgress,
    *,
    chat_id: str,
) -> None:
    """Send the current input step as a new message and re-point the awaiting entry."""
    step = current_step(view, progress)
    if step is None:
        return
    text = format_gate_input_prompt(step)
    keyboard = render_gate_input_keyboard(prefix, step, progress.input_values or {})
    sent = telegram_client.send_message(
        chat_id, text, reply_markup=keyboard, parse_mode="MarkdownV2"
    )
    clear_awaiting_feedback_by_prefix(prefix)
    save_awaiting_feedback(
        str(sent.message_id),
        prefix,
        {
            "action_type": "gate_input",
            "bundle_path": str(view.bundle_path),
        },
    )
