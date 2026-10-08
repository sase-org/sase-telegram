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


def _edit_refreshed_review(
    prefix: str,
    action: dict[str, Any],
    stale_view: GateView,
    updated: GateProgress,
    chat_id: str,
    message_id: int,
) -> bool:
    """Edit a stale card into the current prose plus refreshed controls.

    Returns True when the card edit succeeded (displayed revision is then
    committed via *updated*); False leaves the caller to retry markup-only
    so the refresh stays retryable without claiming new prose was shown.
    """
    try:
        action_data = action.get("action_data") if isinstance(action, dict) else None
        if not isinstance(action_data, dict):
            return False
        fresh_view = load_gate_view(dict(action_data))
    except Exception:
        return False
    try:
        from sase_telegram.formatting import render_gate_keyboard as _render_kb

        markup = _render_kb(prefix, fresh_view, updated)
    except Exception:
        return False
    # Reuse the normal plan-review formatter for the message text when the
    # bundle still carries the presentation context; otherwise keep the
    # header/notes by editing the keyboard alone.
    new_text: str | None = None
    try:
        new_text = _render_refreshed_text(action, fresh_view)
    except Exception:
        new_text = None
    try:
        if new_text is not None:
            telegram_client.edit_message_text(
                chat_id, message_id, new_text, reply_markup=markup
            )
        else:
            telegram_client.edit_message_reply_markup(
                chat_id, message_id, reply_markup=markup
            )
    except Exception:
        return False
    return True


def _render_refreshed_text(action: dict[str, Any], fresh_view: GateView) -> str | None:
    """Best-effort refreshed card text preserving header and notes."""
    try:
        from sase.notifications.store import load_notifications

        from sase_telegram.formatting import format_notification
    except Exception:
        return None
    try:
        notification_id = str(action.get("notification_id", ""))
        if not notification_id:
            return None
        for note in load_notifications(include_dismissed=True):
            if str(getattr(note, "id", "")) != notification_id:
                continue
            text, _kb, _att = format_notification(note)
            return text
    except Exception:
        return None
    return None


def _persist_decision_submit_context(
    view: GateView,
    progress: GateProgress,
    callback_query: Any,
    action: dict[str, Any],
) -> None:
    """Persist source context before submitting so stale rejects can refresh."""
    if not view.decisions:
        return
    from sase_telegram.inbound_handlers.common import (
        _callback_chat_id,
        _callback_origin_message_id,
    )

    message_id = _callback_origin_message_id(callback_query, action)
    chat_id = _callback_chat_id(callback_query, action)
    displayed = (
        progress.displayed_revision
        if progress.displayed_revision is not None
        else view.review_revision
    )
    updated = replace(
        progress,
        source_message_id=message_id,
        source_chat_id=chat_id,
        submitted_revision=int(displayed),
        submitted_values=dict(progress.decision_values or {}),
    )
    save_gate_progress(view, updated)


def persist_decision_submit_context(
    view: GateView,
    progress: GateProgress,
    callback_query: Any,
    action: dict[str, Any],
) -> None:
    """Public wrapper preserving review context for stale-refresh recovery."""
    return _persist_decision_submit_context(view, progress, callback_query, action)


def _answer_stale_with_refresh(
    callback_query: Any,
    action: dict[str, Any],
    prefix: str,
    view: GateView,
    progress: GateProgress,
    message_id: int | None,
    chat_id: str | None,
) -> None:
    from sase_telegram.decision_callbacks import refresh_token_for, stale_response

    text, _label = stale_response()
    _answer_callback(callback_query, text)
    if message_id is None or chat_id is None:
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    from sase_telegram import callback_data as _cb

    refresh = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "↻ Refresh review",
                    callback_data=_cb.encode("gate", prefix, refresh_token_for(view)),
                )
            ]
        ]
    )
    try:
        telegram_client.edit_message_reply_markup(
            chat_id, message_id, reply_markup=refresh
        )
    except Exception:
        pass
    save_gate_progress(view, progress)


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
        review_revision: int | None = None
        if view.decisions:
            from sase_telegram.decision_callbacks import (
                build_selected_option_inputs,
                displayed_revision,
            )

            # Submit only the selected schemas: each selected option gets
            # only the decision_* fields it declares; reject gets {}.
            option_inputs = build_selected_option_inputs(
                view, progress, selected_option_ids, base_inputs=option_inputs
            )
            review_revision = displayed_revision(view, progress)
        if feedback_requested:
            _begin_gate_feedback(
                callback_query,
                action,
                prefix,
                view,
                progress,
                selected_option_ids,
                option_inputs=option_inputs,
                review_revision=review_revision,
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
            review_revision=review_revision,
        )
        _persist_decision_submit_context(view, progress, callback_query, action)
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

    if view.decisions:
        from sase_telegram.decision_callbacks import (
            StaleReview,
            apply_decision_token,
            check_revision,
            split_bound_token,
        )
        from sase_telegram.plan_decisions import parse_decision_token

        if parse_decision_token(cb.choice) is not None:
            from sase_telegram.plan_decisions import parse_decision_token as _parse

            _parsed = _parse(cb.choice)
            _is_refresh = (
                _parsed is not None and str(_parsed.get("kind", "")) == "refresh"
            )
            try:
                updated, toast, _ = apply_decision_token(view, progress, cb.choice)
            except StaleReview:
                _answer_stale_with_refresh(
                    callback_query,
                    action,
                    cb.notif_id_prefix,
                    view,
                    progress,
                    message_id,
                    chat_id,
                )
                return
            except ValueError:
                _answer_callback(callback_query, "Invalid gate callback")
                return
            # Editing, back, and reset never submit. Refresh re-renders
            # both message text and keyboard from the current bundle and
            # requires another tap to approve.
            if _is_refresh and message_id is not None and chat_id is not None:
                if not _edit_refreshed_review(
                    cb.notif_id_prefix, action, view, updated, chat_id, message_id
                ):
                    try:
                        telegram_client.edit_message_reply_markup(
                            chat_id,
                            message_id,
                            reply_markup=render_gate_keyboard(
                                cb.notif_id_prefix, view, updated
                            ),
                        )
                    except Exception:
                        pass
            elif message_id is not None and chat_id is not None:
                try:
                    telegram_client.edit_message_reply_markup(
                        chat_id,
                        message_id,
                        reply_markup=render_gate_keyboard(
                            cb.notif_id_prefix, view, updated
                        ),
                    )
                except Exception:
                    pass
            _answer_callback(callback_query, toast)
            return
        base, bound_revision = split_bound_token(cb.choice)
        if bound_revision is not None:
            if not check_revision(view, progress, bound_revision):
                _answer_stale_with_refresh(
                    callback_query,
                    action,
                    cb.notif_id_prefix,
                    view,
                    progress,
                    message_id,
                    chat_id,
                )
                return
            # Replay-safe: continue with the unbound base token. Values are
            # explicit sets resolved server-side, so replay sets the same
            # value instead of flipping it.
            cb = decode(f"{cb.action_type}:{cb.notif_id_prefix}:{base}")

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
