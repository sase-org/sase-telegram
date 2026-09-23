"""User-question callbacks and free-text answers."""

from __future__ import annotations

import json
from typing import Any
from sase_telegram import pending_actions, question_flow, telegram_client
from sase_telegram.callback_data import decode
from sase.user_question_actions import UserQuestionActionError
from sase_telegram.formatting import (
    format_answered_question,
    format_questions_complete,
    render_question_message,
)
from sase_telegram.inbound import (
    ResponseAction,
    clear_awaiting_feedback,
    clear_awaiting_feedback_by_prefix,
    load_awaiting_feedback,
    save_awaiting_feedback,
)
from sase_telegram.inbound_handlers.common import (
    _message_chat_id,
    _configured_chat_id,
    _message_id,
    _resolve_response,
    _action_message_id,
    _callback_origin_message_id,
    _callback_chat_id,
    _answer_callback,
    _answer_resolved_callback,
)

import logging

log = logging.getLogger(__name__)


def _edit_question_answered(
    *,
    chat_id: str,
    message_id: int,
    request: dict[str, Any],
    answered_index: int,
    answer: dict[str, Any],
) -> None:
    selected = answer.get("selected")
    if not isinstance(selected, list):
        selected = []
    custom = answer.get("custom_feedback")
    text = format_answered_question(
        question_flow.question_at(request, answered_index),
        index=answered_index,
        total=question_flow.question_count(request),
        selected=[str(item) for item in selected],
        custom_feedback=custom if isinstance(custom, str) else None,
    )
    try:
        telegram_client.edit_message_text(
            chat_id,
            message_id,
            text,
            reply_markup=None,
            parse_mode="MarkdownV2",
        )
    except Exception:
        log.warning("Failed to collapse answered question message", exc_info=True)
        try:
            telegram_client.edit_message_reply_markup(
                chat_id,
                message_id,
                reply_markup=None,
            )
        except Exception:
            log.warning("Failed to remove answered question keyboard", exc_info=True)


def _save_pending_action_message(
    prefix: str,
    action: dict[str, Any],
    *,
    chat_id: str,
    message_id: int,
) -> None:
    updated = dict(action)
    updated["chat_id"] = chat_id
    updated["message_id"] = message_id
    pending_actions.add(prefix, updated)


def _send_next_question(
    *,
    prefix: str,
    chat_id: str,
    request: dict[str, Any],
    progress: question_flow.QuestionProgress,
) -> question_flow.QuestionProgress:
    question = question_flow.current_question(request, progress)
    text, keyboard = render_question_message(
        question,
        index=progress.current_index,
        total=progress.total,
        selected=progress.pending_selection,
        prefix=prefix,
    )
    msg = telegram_client.send_message(
        chat_id,
        text,
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )
    message_id = _message_id(msg) or None
    return question_flow.with_active_message(
        progress,
        active_message_id=message_id,
        chat_id=chat_id,
    )


def _question_response_action(
    *,
    prefix: str,
    response_dir: str,
    response_data: dict[str, Any],
) -> ResponseAction:
    return ResponseAction(
        action_type="question",
        notif_id_prefix=prefix,
        response_path=question_flow.response_path(response_dir),
        response_data=response_data,
        answer_text=None,
    )


def _handle_question_decision(
    *,
    callback_query: Any | None,
    prefix: str,
    action: dict[str, Any] | None,
    response_dir: str,
    request: dict[str, Any],
    chat_id: str,
    origin_message_id: int | None,
    decision: question_flow.QuestionDecision,
) -> None:
    if decision.kind == "toggle":
        question_flow.save_progress(response_dir, decision.progress)
        question = question_flow.current_question(request, decision.progress)
        _, keyboard = render_question_message(
            question,
            index=decision.progress.current_index,
            total=decision.progress.total,
            selected=decision.selected,
            prefix=prefix,
        )
        if origin_message_id is not None:
            telegram_client.edit_message_reply_markup(
                chat_id,
                origin_message_id,
                reply_markup=keyboard,
            )
        _answer_callback(callback_query, decision.answer_text)
        return

    if decision.kind == "await_custom":
        question_flow.save_progress(response_dir, decision.progress)
        key = (
            str(decision.progress.active_message_id)
            if decision.progress.active_message_id is not None
            else prefix
        )
        save_awaiting_feedback(
            key,
            prefix,
            {"action_type": "question", "response_dir": response_dir},
        )
        _answer_callback(callback_query, "Send your answer as a text message")
        if origin_message_id is not None:
            telegram_client.edit_message_reply_markup(
                chat_id,
                origin_message_id,
                reply_markup=None,
            )
        return

    if origin_message_id is not None and decision.kind != "complete":
        _edit_question_answered(
            chat_id=chat_id,
            message_id=origin_message_id,
            request=request,
            answered_index=decision.answered_index,
            answer=decision.answer,
        )

    if decision.kind == "advance":
        if question_flow.response_path(response_dir).exists():
            pending_actions.remove(prefix)
            clear_awaiting_feedback_by_prefix(prefix)
            question_flow.clear_progress(response_dir)
            _answer_callback(callback_query, "This action has already been handled")
            return

        progress = _send_next_question(
            prefix=prefix,
            chat_id=chat_id,
            request=request,
            progress=decision.progress,
        )
        question_flow.save_progress(response_dir, progress)
        if action and progress.active_message_id is not None:
            _save_pending_action_message(
                prefix,
                action,
                chat_id=chat_id,
                message_id=progress.active_message_id,
            )
        _answer_callback(callback_query, decision.answer_text)
        return

    response = _question_response_action(
        prefix=prefix,
        response_dir=response_dir,
        response_data=decision.response_data,
    )
    try:
        _resolve_response(response, action)
    except UserQuestionActionError as exc:
        if exc.code == "conflict_already_handled":
            pending_actions.remove(prefix)
            clear_awaiting_feedback_by_prefix(prefix)
            question_flow.clear_progress(response_dir)
            _answer_callback(callback_query, "This action has already been handled")
            return
        retry_progress = question_flow.QuestionProgress(
            session_id=decision.progress.session_id,
            total=decision.progress.total,
            current_index=decision.answered_index,
            answers=list(decision.progress.answers or [])[:-1],
            pending_selection=[],
            active_message_id=origin_message_id,
            chat_id=chat_id,
        )
        retry_progress = _send_next_question(
            prefix=prefix,
            chat_id=chat_id,
            request=request,
            progress=retry_progress,
        )
        question_flow.save_progress(response_dir, retry_progress)
        if action and retry_progress.active_message_id is not None:
            _save_pending_action_message(
                prefix,
                action,
                chat_id=chat_id,
                message_id=retry_progress.active_message_id,
            )
        _answer_callback(callback_query, f"Question response failed: {exc}")
        return
    if origin_message_id is not None:
        _edit_question_answered(
            chat_id=chat_id,
            message_id=origin_message_id,
            request=request,
            answered_index=decision.answered_index,
            answer=decision.answer,
        )
    telegram_client.send_message(
        chat_id,
        format_questions_complete(decision.response_data["answers"]),
        parse_mode="MarkdownV2",
    )
    pending_actions.remove(prefix)
    clear_awaiting_feedback_by_prefix(prefix)
    question_flow.clear_progress(response_dir)
    _answer_callback(callback_query, decision.answer_text)


def _handle_question_callback(callback_query: Any, pending: dict[str, Any]) -> None:
    """Handle a progress-aware user-question callback."""
    try:
        cb = decode(callback_query.data)
    except ValueError:
        telegram_client.answer_callback_query(callback_query.id, "Invalid callback")
        return

    action = pending.get(cb.notif_id_prefix)
    if action is None:
        telegram_client.answer_callback_query(
            callback_query.id,
            "This action has already been handled",
        )
        return

    action_data = action.get("action_data", {})
    response_dir = (
        action_data.get("response_dir") if isinstance(action_data, dict) else None
    )
    if not isinstance(response_dir, str) or not response_dir:
        telegram_client.answer_callback_query(
            callback_query.id, "This request has expired"
        )
        pending_actions.remove(cb.notif_id_prefix)
        clear_awaiting_feedback_by_prefix(cb.notif_id_prefix)
        return

    response_path = question_flow.response_path(response_dir)
    if response_path.exists():
        _answer_resolved_callback(
            callback_query,
            action,
            cb.notif_id_prefix,
            "already_handled",
        )
        question_flow.clear_progress(response_dir)
        return

    try:
        request = question_flow.load_question_request(response_dir)
    except (OSError, json.JSONDecodeError):
        telegram_client.answer_callback_query(
            callback_query.id, "This request has expired"
        )
        pending_actions.remove(cb.notif_id_prefix)
        clear_awaiting_feedback_by_prefix(cb.notif_id_prefix)
        question_flow.clear_progress(response_dir)
        return

    origin_message_id = _callback_origin_message_id(callback_query, action)
    active_message_id = _action_message_id(action) or origin_message_id
    chat_id = _callback_chat_id(callback_query, action)
    if chat_id is None:
        telegram_client.answer_callback_query(
            callback_query.id, "This request has expired"
        )
        return

    progress = question_flow.load_progress(
        response_dir,
        request,
        active_message_id=active_message_id,
        chat_id=chat_id,
    )
    if question_flow.is_stale_tap(progress, origin_message_id):
        telegram_client.answer_callback_query(
            callback_query.id,
            "This question has already been answered",
        )
        if origin_message_id is not None:
            try:
                telegram_client.edit_message_reply_markup(
                    chat_id,
                    origin_message_id,
                    reply_markup=None,
                )
            except Exception:
                log.warning("Failed to dismiss stale question keyboard", exc_info=True)
        return

    try:
        decision = question_flow.apply_question_choice(request, progress, cb.choice)
    except ValueError:
        telegram_client.answer_callback_query(callback_query.id, "Invalid callback")
        return

    _handle_question_decision(
        callback_query=callback_query,
        prefix=cb.notif_id_prefix,
        action=action,
        response_dir=response_dir,
        request=request,
        chat_id=chat_id,
        origin_message_id=origin_message_id,
        decision=decision,
    )


def _awaiting_key_from_progress(
    progress: question_flow.QuestionProgress, fallback: str
) -> str:
    if progress.active_message_id is not None:
        return str(progress.active_message_id)
    return fallback


def _clear_question_awaiting(reply_key: str | None, prefix: str) -> None:
    if reply_key is not None:
        clear_awaiting_feedback(reply_key)
    else:
        clear_awaiting_feedback_by_prefix(prefix)


def _handle_question_text_message(
    message: Any,
    text: str,
    *,
    reply_key: str | None,
) -> bool:
    """Complete a question custom-answer flow when the user sends text."""
    awaiting = load_awaiting_feedback(reply_key)
    if not awaiting:
        return False

    info = awaiting.get("action_info")
    if not isinstance(info, dict) or info.get("action_type") != "question":
        return False

    prefix = awaiting.get("prefix")
    response_dir = info.get("response_dir")
    if not isinstance(prefix, str) or not isinstance(response_dir, str):
        return False

    action = pending_actions.get(prefix)
    chat_id = _message_chat_id(message)
    if chat_id is None and action and action.get("chat_id") is not None:
        chat_id = str(action["chat_id"])
    if chat_id is None:
        chat_id = _configured_chat_id()
    if chat_id is None:
        return True

    response_path = question_flow.response_path(response_dir)
    if response_path.exists():
        _clear_question_awaiting(reply_key, prefix)
        pending_actions.remove(prefix)
        question_flow.clear_progress(response_dir)
        telegram_client.send_message(chat_id, "This action has already been handled")
        return True

    try:
        request = question_flow.load_question_request(response_dir)
    except (OSError, json.JSONDecodeError):
        _clear_question_awaiting(reply_key, prefix)
        pending_actions.remove(prefix)
        question_flow.clear_progress(response_dir)
        telegram_client.send_message(chat_id, "This question request has expired")
        return True

    active_message_id = _action_message_id(action) if action else None
    if active_message_id is None and reply_key is not None:
        try:
            active_message_id = int(reply_key)
        except ValueError:
            active_message_id = None

    progress = question_flow.load_progress(
        response_dir,
        request,
        active_message_id=active_message_id,
        chat_id=chat_id,
    )
    decision = question_flow.apply_question_custom_text(request, progress, text)

    _handle_question_decision(
        callback_query=None,
        prefix=prefix,
        action=action,
        response_dir=response_dir,
        request=request,
        chat_id=chat_id,
        origin_message_id=progress.active_message_id,
        decision=decision,
    )
    clear_awaiting_feedback(
        reply_key
        if reply_key is not None
        else _awaiting_key_from_progress(progress, prefix)
    )
    clear_awaiting_feedback_by_prefix(prefix)
    return True
