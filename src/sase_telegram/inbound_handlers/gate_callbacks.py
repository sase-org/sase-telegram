"""Gate button entry point and option selection."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from sase_telegram import telegram_client
from sase_telegram.callback_data import decode
from sase.notification_gates.models import GateError
from sase.notification_gates.registry import adapter_for_action
from sase_telegram.formatting import render_gate_keyboard
from sase_telegram.gate_flow import (
    GateProgress,
    GateView,
    branch_for_token,
    expand_branch,
    feedback_mode,
    load_gate_view,
    load_progress as load_gate_progress,
    option_for_id,
    save_progress as save_gate_progress,
    toggle_option,
)
from sase_telegram.gate_inputs import begin_input, pending_fields, unsupported_fields
from sase_telegram.inbound import ResponseAction
from sase_telegram.inbound_handlers.common import (
    _STALE_AWAITING_FEEDBACK_TEXT,
    _callback_origin_message_id,
    _callback_chat_id,
    _answer_callback,
    _gate_error_answer_text,
)
from sase_telegram.inbound_handlers.gate_response import (
    _dismiss_gate_callback,
    _execute_gate_callback_response,
    _begin_gate_feedback,
    _send_gate_input_prompt,
)
from sase_telegram.inbound_handlers.gate_input_steps import _handle_gate_input_callback


def _reject_tty_required_selection(
    callback_query: Any, view: GateView, selected_option_ids: tuple[str, ...]
) -> bool:
    """Reject a selection that includes a requires_tty option; return True if rejected.

    Mirrors ``cli_answer._reject_detached_tty_options``: Telegram is a
    detached transport with no controlling TTY, so these options must never
    reach ``execute_gate_selection``. ``render_gate_keyboard`` already hides
    them, but a forged or stale callback token can still name one directly.
    """
    ids = [
        option_id
        for option_id in selected_option_ids
        if (option := option_for_id(view, option_id)) is not None
        and option.requires_tty
    ]
    if not ids:
        return False
    _answer_callback(
        callback_query,
        "This gate option requires a controlling TTY and cannot be answered "
        "through Telegram",
    )
    return True


def _start_or_submit_gate_selection(
    callback_query: Any,
    action: dict[str, Any],
    prefix: str,
    view: GateView,
    progress: GateProgress,
    selected_option_ids: tuple[str, ...],
    *,
    feedback_requested: bool,
) -> None:
    """Open declared-input collection for a committed selection, or submit it."""
    if _reject_tty_required_selection(callback_query, view, selected_option_ids):
        return
    try:
        fields = pending_fields(view, selected_option_ids)
    except GateError as exc:
        _answer_callback(callback_query, str(exc))
        return
    secret_fields = unsupported_fields(fields)
    if secret_fields:
        ids = ", ".join(field.id for field in secret_fields)
        _answer_callback(callback_query, f"Telegram cannot collect secret input: {ids}")
        return

    if not fields:
        option_inputs: dict[str, dict[str, Any]] = {
            option_id: {} for option_id in selected_option_ids
        }
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
        _execute_gate_callback_response(callback_query, action, response, view)
        return

    progress = replace(progress, selected_option_ids=selected_option_ids)
    progress = begin_input(
        progress, selected_option_ids, feedback_requested=feedback_requested
    )
    save_gate_progress(view, progress)
    chat_id = _callback_chat_id(callback_query, action)
    if chat_id is None:
        _answer_callback(callback_query, "This request has expired")
        return
    _send_gate_input_prompt(prefix, view, progress, chat_id=chat_id)
    _answer_callback(callback_query, "Answer the input prompt below")


def _handle_gate_callback(callback_query: Any, pending: dict[str, Any]) -> None:
    """Handle one compact callback for any v2 non-question gate."""
    cb = decode(callback_query.data)
    action = pending.get(cb.notif_id_prefix)
    if action is None:
        _answer_callback(callback_query, _STALE_AWAITING_FEEDBACK_TEXT)
        return
    adapter = adapter_for_action(
        action.get("action") if isinstance(action.get("action"), str) else None
    )
    if adapter is None or not adapter.branch_actionable:
        _answer_callback(callback_query, "This request has expired")
        return
    action_data = action.get("action_data")
    if not isinstance(action_data, dict):
        _answer_callback(callback_query, "This request has expired")
        return
    try:
        view = load_gate_view(action_data, expected_kind=adapter.kind)
    except GateError as exc:
        _answer_callback(callback_query, _gate_error_answer_text(exc))
        _dismiss_gate_callback(callback_query, action, cb.notif_id_prefix)
        return

    message_id = _callback_origin_message_id(callback_query, action)
    chat_id = _callback_chat_id(callback_query, action)
    progress = load_gate_progress(
        view,
        active_message_id=message_id,
        chat_id=chat_id,
    )

    if cb.choice.startswith("i"):
        _handle_gate_input_callback(
            callback_query, action, cb.notif_id_prefix, view, progress
        )
        return

    selected_option_ids: tuple[str, ...] | None = None
    branch_result = branch_for_token(view, cb.choice, prefix="c")
    if branch_result is not None:
        branch_index, branch = branch_result
        if len(branch) > 1:
            progress = expand_branch(view, progress, branch_index)
            save_gate_progress(view, progress)
            if message_id is not None and chat_id is not None:
                telegram_client.edit_message_reply_markup(
                    chat_id,
                    message_id,
                    reply_markup=render_gate_keyboard(
                        cb.notif_id_prefix, view, progress
                    ),
                )
            first = option_for_id(view, branch[0])
            _answer_callback(
                callback_query,
                f"Opened {first.label if first is not None else 'gate group'}",
            )
            return
        selected_option_ids = branch

    elif cb.choice.startswith("x"):
        try:
            progress, enabled = toggle_option(view, progress, cb.choice)
        except ValueError as exc:
            _answer_callback(callback_query, str(exc))
            return
        save_gate_progress(view, progress)
        if message_id is not None and chat_id is not None:
            telegram_client.edit_message_reply_markup(
                chat_id,
                message_id,
                reply_markup=render_gate_keyboard(cb.notif_id_prefix, view, progress),
            )
        _answer_callback(
            callback_query, "Option selected" if enabled else "Option cleared"
        )
        return

    elif cb.choice.startswith("f"):
        feedback_result = branch_for_token(view, cb.choice, prefix="f")
        if feedback_result is None:
            _answer_callback(callback_query, "Invalid gate callback")
            return
        branch_index, branch = feedback_result
        if len(branch) == 1:
            selected_option_ids = branch
        elif progress.expanded_branch_index != branch_index:
            _answer_callback(callback_query, "Open this gate group before submitting")
            return
        else:
            selected_set = set(progress.selected_option_ids)
            selected_option_ids = tuple(
                option_id for option_id in branch if option_id in selected_set
            )
        if not selected_option_ids:
            _answer_callback(callback_query, "Select at least one option")
            return
        if feedback_mode(view, selected_option_ids) == "disabled":
            _answer_callback(callback_query, "This option does not accept feedback")
            return
        _start_or_submit_gate_selection(
            callback_query,
            action,
            cb.notif_id_prefix,
            view,
            progress,
            selected_option_ids,
            feedback_requested=True,
        )
        return

    else:
        submit_result = branch_for_token(view, cb.choice, prefix="s")
        if submit_result is None:
            _answer_callback(callback_query, "Invalid gate callback")
            return
        branch_index, branch = submit_result
        if len(branch) == 1 or progress.expanded_branch_index != branch_index:
            _answer_callback(callback_query, "Open this gate group before submitting")
            return
        selected_set = set(progress.selected_option_ids)
        selected_option_ids = tuple(
            option_id for option_id in branch if option_id in selected_set
        )

    if not selected_option_ids:
        _answer_callback(callback_query, "Select at least one option")
        return
    _start_or_submit_gate_selection(
        callback_query,
        action,
        cb.notif_id_prefix,
        view,
        progress,
        selected_option_ids,
        feedback_requested=(feedback_mode(view, selected_option_ids) == "required"),
    )
