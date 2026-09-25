"""/show rendering and callbacks."""

from __future__ import annotations

from typing import Any
from sase_telegram import credentials, pending_actions, telegram_client
from sase_telegram.agent_format import _pack_html_blocks
from sase_telegram.callback_data import encode, generate_key
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from sase_telegram.formatting import display_cl_names_in_text
from sase_telegram.show_entities import (
    InvalidShowReference,
    ShowNotFound,
    ShowTarget,
    build_kinship_index,
    resolve_show_reference,
)
from sase_telegram.show_format import (
    ShowButtonSpec,
    ShowView,
    format_show_index,
    format_show_not_found,
    format_show_target,
)
from sase_telegram.inbound_handlers.common import (
    _COPY_TEXT_MAX,
    _message_chat_id,
    _callback_origin_message_id,
    _callback_chat_id,
    _answer_callback,
    _send_html_chunks,
)
from sase_telegram.inbound_handlers.agent_launch import (
    _get_agent_retry_prompt,
    _build_agent_action_keyboard,
)
from sase_telegram.inbound_handlers.agent_list import _load_list_entries

import logging

log = logging.getLogger(__name__)


def _handle_show_command(args: str = "", message: Any | None = None) -> None:
    """Handle ``/show`` for agents, clans, sessions, tribes, and the index."""
    chat_id = _message_chat_id(message) or credentials.get_chat_id()
    try:
        entries = _load_list_entries()
        ref = args.strip()
        if ref:
            chunks, keyboard = _render_show_reference(ref, entries)
        else:
            view = format_show_index(build_kinship_index(entries))
            chunks = _pack_html_blocks(list(view.blocks))
            keyboard = _build_show_keyboard(view)
    except InvalidShowReference:
        telegram_client.send_message(
            chat_id,
            "Invalid tribe reference. Use <code>/show @name</code>; tribe names "
            "may contain letters, digits, underscores, dots, and dashes.",
            parse_mode="HTML",
        )
        return
    except Exception:
        log.exception("Failed to build /show view")
        telegram_client.send_message(chat_id, "Failed to build /show view.")
        return
    _send_html_chunks(chat_id, chunks, reply_markup=keyboard)


def _render_show_reference(
    ref: str, entries: list[Any] | None = None
) -> tuple[list[str], InlineKeyboardMarkup | None]:
    """Resolve and render one show reference from a fresh entry snapshot."""
    all_entries = entries if entries is not None else _load_list_entries()
    resolved = resolve_show_reference(ref, all_entries)
    if isinstance(resolved, ShowNotFound):
        view = format_show_not_found(resolved)
        target = None
        prompt = None
        action_prompt = None
    else:
        target = resolved
        prompt = None
        action_prompt = None
        if target.kind == "agent" and target.entry is not None:
            action_prompt = _get_agent_retry_prompt(target.name) or getattr(
                target.entry, "prompt", None
            )
            if isinstance(action_prompt, str) and action_prompt.strip():
                prompt = display_cl_names_in_text(action_prompt.strip())
        view = format_show_target(target, prompt=prompt)
    return (
        _pack_html_blocks(list(view.blocks)),
        _build_show_keyboard(view, target=target, prompt=action_prompt),
    )


def _build_show_keyboard(
    view: ShowView,
    *,
    target: ShowTarget | None = None,
    prompt: str | None = None,
) -> InlineKeyboardMarkup | None:
    """Convert pure show button specs into persisted Telegram callbacks."""
    rows: list[list[InlineKeyboardButton]] = []
    if target is not None and target.kind == "agent":
        entry = target.entry
        include_kill = not bool(getattr(entry, "is_terminal", False))
        if entry is None:
            include_kill = not bool(getattr(target.named_agent, "is_done", False))
        base = _build_agent_action_keyboard(
            target.name,
            prompt_for_vcs=prompt,
            retry_source_prompt=prompt,
            include_kill=include_kill,
        )
        rows.extend([list(row) for row in base.inline_keyboard])

    for spec_row in view.button_rows:
        buttons: list[InlineKeyboardButton] = []
        for spec in spec_row:
            button = _show_button(spec)
            if button is not None:
                buttons.append(button)
        if buttons:
            rows.append(buttons)
    return InlineKeyboardMarkup(rows) if rows else None


def _show_button(spec: ShowButtonSpec) -> InlineKeyboardButton | None:
    if spec.action == "copy":
        if len(spec.ref) > _COPY_TEXT_MAX:
            log.warning("Skipping overlong /show copy-text button: %s", spec.label)
            return None
        return InlineKeyboardButton(
            spec.label,
            copy_text=CopyTextButton(text=spec.ref),
        )

    selection_key = generate_key()
    pending_actions.add(
        f"show-{selection_key}",
        {"action": "show", "ref": spec.ref},
    )
    return InlineKeyboardButton(
        spec.label,
        callback_data=encode("show", selection_key, spec.action),
    )


def _handle_show_callback(callback_query: Any, selection_key: str, choice: str) -> None:
    """Open or refresh a persisted ``/show`` selection."""
    action = pending_actions.get(f"show-{selection_key}")
    if not isinstance(action, dict) or action.get("action") != "show":
        _answer_callback(callback_query, "Selection expired — run /show again")
        return
    ref = action.get("ref")
    if not isinstance(ref, str) or not ref:
        _answer_callback(callback_query, "Selection expired — run /show again")
        return
    if choice not in {"open", "refresh"}:
        _answer_callback(callback_query, "Invalid show action")
        return

    try:
        chunks, keyboard = _render_show_reference(ref)
    except InvalidShowReference:
        _answer_callback(callback_query, "Invalid tribe reference")
        return
    except Exception:
        log.exception("Failed to build /show callback view for %r", ref)
        _answer_callback(callback_query, "Failed to build /show view")
        return

    chat_id = _callback_chat_id(callback_query, action)
    if chat_id is None:
        _answer_callback(callback_query, "Could not resolve Telegram chat")
        return
    if choice == "refresh":
        message_id = _callback_origin_message_id(callback_query, action)
        if message_id is not None and len(chunks) == 1:
            telegram_client.edit_message_text(
                chat_id,
                message_id,
                chunks[0],
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            _send_html_chunks(chat_id, chunks, reply_markup=keyboard)
        _answer_callback(callback_query, "Refreshed")
        return

    _send_html_chunks(chat_id, chunks, reply_markup=keyboard)
    _answer_callback(callback_query, "Opened")
