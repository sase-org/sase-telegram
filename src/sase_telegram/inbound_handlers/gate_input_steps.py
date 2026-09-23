"""Multi-step gate input collection."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from sase_telegram import pending_actions, telegram_client
from sase_telegram.callback_data import decode
from sase.notification_gates.models import GateError
from sase.xprompt.models import InputType, XPromptValidationError
from sase_telegram.formatting import render_gate_input_keyboard
from sase_telegram.gate_flow import (
    GateProgress,
    GateView,
    load_gate_view,
    load_progress as load_gate_progress,
    save_progress as save_gate_progress,
)
from sase_telegram.gate_inputs import (
    advance,
    apply_choice,
    apply_text_answer,
    clear_input,
    current_step,
    decode_input_token,
    skip_step,
    submitted_option_inputs,
)
from sase_telegram.inbound import ResponseAction, load_awaiting_feedback
from sase_telegram.inbound_handlers.common import (
    _STALE_AWAITING_FEEDBACK_TEXT,
    _message_chat_id,
    _configured_chat_id,
    _message_id,
    _callback_chat_id,
    _answer_callback,
    _gate_error_answer_text,
    _clear_awaiting_feedback_entry,
)
from sase_telegram.inbound_handlers.gate_response import (
    _execute_gate_callback_response,
    _begin_gate_feedback,
    _send_gate_input_prompt,
)


def _handle_gate_input_callback(
    callback_query: Any,
    action: dict[str, Any],
    prefix: str,
    view: GateView,
    progress: GateProgress,
) -> None:
    """Handle one compact ``i<index><verb>`` callback for the input step flow."""
    cb = decode(callback_query.data)
    decoded = decode_input_token(cb.choice)
    if decoded is None:
        _answer_callback(callback_query, "Invalid gate callback")
        return
    token_index, verb = decoded

    prompt_message = getattr(callback_query, "message", None)
    prompt_chat_id = _message_chat_id(prompt_message)
    prompt_message_id = _message_id(prompt_message)

    if verb == "c":
        progress = clear_input(progress)
        save_gate_progress(view, progress)
        _clear_awaiting_feedback_entry(None, prefix)
        if prompt_chat_id is not None and prompt_message_id:
            telegram_client.edit_message_reply_markup(
                prompt_chat_id, prompt_message_id, reply_markup=None
            )
        _answer_callback(
            callback_query, "Input cancelled — the gate is still answerable"
        )
        return

    step = current_step(view, progress)
    if step is None or step.index != token_index:
        _answer_callback(callback_query, "This input step is no longer active")
        return

    field = step.field
    values = progress.input_values or {}

    if verb == "k":
        if field.required:
            _answer_callback(callback_query, "This input is required")
            return
        progress = replace(progress, input_values=skip_step(values, field))
        _advance_gate_input(callback_query, action, prefix, view, progress)
        return

    if verb == "d":
        if not (field.repeatable and field.type is InputType.ENUM):
            _answer_callback(callback_query, "Invalid gate callback")
            return
        if field.required and not values.get(field.id):
            _answer_callback(callback_query, "Select at least one option")
            return
        _advance_gate_input(callback_query, action, prefix, view, progress)
        return

    choice_index = int(verb[1:])
    if field.type is not InputType.ENUM or not (0 <= choice_index < len(field.choices)):
        _answer_callback(callback_query, "Invalid gate callback")
        return
    value = field.choices[choice_index].value
    new_values, selected_now = apply_choice(values, field, value)
    progress = replace(progress, input_values=new_values)
    if field.repeatable:
        save_gate_progress(view, progress)
        if prompt_chat_id is not None and prompt_message_id:
            telegram_client.edit_message_reply_markup(
                prompt_chat_id,
                prompt_message_id,
                reply_markup=render_gate_input_keyboard(
                    prefix, step, progress.input_values or {}
                ),
            )
        _answer_callback(callback_query, "Selected" if selected_now else "Removed")
        return
    if prompt_chat_id is not None and prompt_message_id:
        telegram_client.edit_message_reply_markup(
            prompt_chat_id, prompt_message_id, reply_markup=None
        )
    _advance_gate_input(callback_query, action, prefix, view, progress)


def _advance_gate_input(
    callback_query: Any,
    action: dict[str, Any],
    prefix: str,
    view: GateView,
    progress: GateProgress,
    *,
    message: Any | None = None,
) -> None:
    """Move past the current input step, sending the next prompt or submitting."""
    progress = advance(progress)
    save_gate_progress(view, progress)
    step = current_step(view, progress)
    if step is not None:
        chat_id = (
            _callback_chat_id(callback_query, action)
            if callback_query is not None
            else (_message_chat_id(message) or _configured_chat_id())
        )
        if chat_id is None:
            return
        _send_gate_input_prompt(prefix, view, progress, chat_id=chat_id)
        _answer_callback(callback_query, "Answer the input prompt below")
        return

    option_inputs = submitted_option_inputs(view, progress)
    selected_option_ids = progress.input_option_ids
    feedback_requested = progress.input_feedback_requested
    progress = clear_input(progress)
    save_gate_progress(view, progress)
    _clear_awaiting_feedback_entry(None, prefix)

    if feedback_requested:
        _begin_gate_feedback(
            callback_query,
            action,
            prefix,
            view,
            progress,
            selected_option_ids,
            option_inputs=option_inputs,
        )
        return

    response = ResponseAction(
        action_type="gate",
        notif_id_prefix=prefix,
        response_path=view.bundle_path / "response.json",
        response_data={},
        answer_text=None,
        selected_option_ids=selected_option_ids,
        option_inputs=option_inputs,
    )
    _execute_gate_callback_response(
        callback_query, action, response, view, message=message
    )


def _handle_gate_input_text_message(
    message: Any,
    text: str,
    *,
    reply_key: str | None,
) -> bool:
    """Complete one declared-input step when the user sends a text reply."""
    awaiting = load_awaiting_feedback(reply_key)
    if not awaiting:
        return False
    info = awaiting.get("action_info")
    if not isinstance(info, dict) or info.get("action_type") != "gate_input":
        return False

    prefix = awaiting.get("prefix")
    bundle_path = info.get("bundle_path")
    if not isinstance(prefix, str) or not isinstance(bundle_path, str):
        return False

    action = pending_actions.get(prefix)
    chat_id = _message_chat_id(message)
    if chat_id is None and action and action.get("chat_id") is not None:
        chat_id = str(action["chat_id"])
    if chat_id is None:
        chat_id = _configured_chat_id()
    if chat_id is None:
        return True

    def _reply(reply_text: str) -> None:
        telegram_client.send_message(
            chat_id, reply_text, reply_to_message_id=message.message_id
        )

    action_data = action.get("action_data") if isinstance(action, dict) else None
    response_path = Path(bundle_path) / "response.json"
    if action is None or not isinstance(action_data, dict) or response_path.exists():
        _clear_awaiting_feedback_entry(reply_key, prefix)
        pending_actions.remove(prefix)
        _reply(_STALE_AWAITING_FEEDBACK_TEXT)
        return True

    try:
        view = load_gate_view(action_data)
    except GateError as exc:
        _clear_awaiting_feedback_entry(reply_key, prefix)
        pending_actions.remove(prefix)
        _reply(_gate_error_answer_text(exc))
        return True

    progress = load_gate_progress(view)
    step = current_step(view, progress)
    if step is None:
        _clear_awaiting_feedback_entry(reply_key, prefix)
        _reply("This input step is no longer active")
        return True

    if step.field.type is InputType.ENUM:
        _reply("Choose one of the buttons above")
        return True

    try:
        new_values = apply_text_answer(progress.input_values or {}, step.field, text)
    except XPromptValidationError as exc:
        _reply(str(exc))
        _send_gate_input_prompt(prefix, view, progress, chat_id=chat_id)
        return True

    progress = replace(progress, input_values=new_values)
    _advance_gate_input(None, action, prefix, view, progress, message=message)
    return True
