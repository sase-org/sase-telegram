"""/list overview, detail, and callbacks."""

from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Any
from sase_telegram import credentials, telegram_client
from sase_telegram.agent_format import (
    _format_agent_list_block,
    _format_header_status_counts,
    _html,
    _pack_html_blocks,
    format_agent_detail,
)
from sase_telegram.callback_data import encode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from sase.agent.status_buckets import AGENT_STATUS_BUCKETS
from sase_telegram.formatting import display_cl_names_in_text, display_project_name
from sase_telegram.inbound_handlers.common import (
    _callback_origin_message_id,
    _callback_chat_id,
    _answer_callback,
    _send_html_chunks,
)
from sase_telegram.inbound_handlers.agent_launch import (
    _get_agent_retry_prompt,
    _build_agent_action_keyboard,
)
from sase_telegram.inbound_handlers.agent_actions import (
    _show_kill_selection,
    _handle_fork_command,
)


@dataclass(frozen=True)
class _ListCommandArgs:
    include_recent: bool = False
    query: str | None = None


def _parse_list_args(args: str) -> _ListCommandArgs:
    tokens = [token for token in args.split() if token]
    include_recent = False
    remaining: list[str] = []
    for token in tokens:
        if token.lower() == "all":
            include_recent = True
        else:
            remaining.append(token)
    return _ListCommandArgs(
        include_recent=include_recent,
        query=" ".join(remaining) if remaining else None,
    )


def _active_list_entries(entries: list[Any]) -> list[Any]:
    return [
        entry for entry in entries if not bool(getattr(entry, "is_terminal", False))
    ]


def _load_list_entries(*, project: str | None = None) -> list[Any]:
    from sase.integrations.agent_list_entries import agent_list_entries

    return list(agent_list_entries(include_recent=True, project=project))


def _find_entry_by_name(entries: list[Any], name: str) -> Any | None:
    for entry in entries:
        if getattr(entry, "name", None) == name:
            return entry
    return None


def _handle_list_command(args: str = "") -> None:
    """Handle /list, /list all, /list <name>, and /list <project>."""
    parsed = _parse_list_args(args)
    chat_id = credentials.get_chat_id()
    all_entries = _load_list_entries()

    if parsed.query:
        detail_entry = _find_entry_by_name(all_entries, parsed.query)
        if detail_entry is not None:
            _send_list_detail(chat_id, detail_entry)
            return
        project_entries = [
            entry
            for entry in all_entries
            if getattr(entry, "project", None) == parsed.query
        ]
        chunks, keyboard = _render_list_overview(
            project_entries,
            include_recent=parsed.include_recent,
            project=parsed.query,
            include_keyboard=False,
        )
        _send_html_chunks(chat_id, chunks, reply_markup=keyboard)
        return

    chunks, keyboard = _render_list_overview(
        all_entries,
        include_recent=parsed.include_recent,
        include_keyboard=True,
    )
    _send_html_chunks(chat_id, chunks, reply_markup=keyboard)


def _handle_list_callback(callback_query: Any, mode: str, choice: str) -> None:
    """Handle the global /list overview keyboard."""
    chat_id = _callback_chat_id(callback_query, None)
    if choice == "kill":
        _answer_callback(callback_query, "Opening kill list")
        if chat_id is not None:
            _show_kill_selection(chat_id)
        return
    if choice == "fork":
        _answer_callback(callback_query, "Opening fork list")
        _handle_fork_command()
        return
    if choice not in {"refresh", "all", "active"}:
        _answer_callback(callback_query, "Invalid list action")
        return

    include_recent = mode == "all"
    if choice == "all":
        include_recent = True
    elif choice == "active":
        include_recent = False

    chunks, keyboard = _render_list_overview(
        _load_list_entries(),
        include_recent=include_recent,
        include_keyboard=True,
    )
    message_id = _callback_origin_message_id(callback_query, None)
    if chat_id is not None and message_id is not None and len(chunks) == 1:
        telegram_client.edit_message_text(
            chat_id,
            message_id,
            chunks[0],
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    elif chat_id is not None:
        _send_html_chunks(chat_id, chunks, reply_markup=keyboard)
    _answer_callback(callback_query, "Refreshed")


def _render_list_overview(
    all_entries: list[Any],
    *,
    include_recent: bool,
    project: str | None = None,
    include_keyboard: bool,
) -> tuple[list[str], InlineKeyboardMarkup | None]:
    from sase.integrations.agent_status_groups import status_bucket_header

    visible_entries = (
        all_entries if include_recent else _active_list_entries(all_entries)
    )
    active_count = len(_active_list_entries(all_entries))
    blocks = [_format_list_header(all_entries, active_count, include_recent, project)]

    if visible_entries:
        for bucket, group_entries in _group_list_entries(visible_entries):
            blocks.append(
                f"<b>{_html(status_bucket_header(bucket, len(group_entries)))}</b>"
            )
            blocks.extend(_format_agent_list_block(entry) for entry in group_entries)
    else:
        empty = f"No {'agents' if include_recent else 'running agents'}" + (
            f" for {_html(display_project_name(project))}." if project else "."
        )
        blocks.append(empty)

    footer = _format_list_footer(all_entries, include_recent=include_recent)
    if footer:
        blocks.append(footer)

    keyboard = (
        _build_list_overview_keyboard(include_recent=include_recent)
        if include_keyboard
        else None
    )
    return _pack_html_blocks(blocks), keyboard


def _group_list_entries(entries: list[Any]) -> list[tuple[str, list[Any]]]:
    grouped: dict[str, list[Any]] = {}
    for entry in entries:
        bucket = str(getattr(entry, "status_bucket", "") or "Running")
        grouped.setdefault(bucket, []).append(entry)

    ordered: list[tuple[str, list[Any]]] = [
        (bucket, grouped[bucket])
        for bucket in AGENT_STATUS_BUCKETS
        if grouped.get(bucket)
    ]
    ordered.extend(
        (bucket, bucket_entries)
        for bucket, bucket_entries in grouped.items()
        if bucket not in AGENT_STATUS_BUCKETS
    )
    return ordered


def _format_list_header(
    entries: list[Any],
    active_count: int,
    include_recent: bool,
    project: str | None,
) -> str:
    title_parts = ["🤖 <b>Agents</b>"]
    if project:
        title_parts.append(_html(display_project_name(project)))
    if include_recent:
        title_parts.append(f"{len(entries)} total")
        title_parts.append(f"{active_count} active")
    else:
        title_parts.append(f"{active_count} active")

    status_line = _format_header_status_counts(_active_list_entries(entries))
    if status_line:
        return " · ".join(title_parts) + "\n" + status_line
    return " · ".join(title_parts)


def _format_list_footer(entries: list[Any], *, include_recent: bool) -> str | None:
    if include_recent:
        return None
    done = failed = 0
    for entry in entries:
        finished_at = getattr(entry, "finished_at", None)
        if not isinstance(finished_at, datetime):
            continue
        now = datetime.now(finished_at.tzinfo)
        if now - finished_at > timedelta(hours=1):
            continue
        bucket = getattr(entry, "status_bucket", None)
        if bucket == "Done":
            done += 1
        elif bucket == "Failed":
            failed += 1
    parts: list[str] = []
    if done:
        parts.append(f"✓ {done} done")
    if failed:
        parts.append(f"✗ {failed} failed")
    if not parts:
        return None
    return "— " + " · ".join(parts) + " in the last hour · /list all"


def _build_list_overview_keyboard(*, include_recent: bool) -> InlineKeyboardMarkup:
    next_mode = "active" if include_recent else "all"
    toggle_label = "Hide finished" if include_recent else "✓ Show finished"
    current_mode = "all" if include_recent else "active"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=encode("list", current_mode, "refresh"),
                ),
                InlineKeyboardButton(
                    toggle_label,
                    callback_data=encode("list", current_mode, next_mode),
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗡️ Kill…",
                    callback_data=encode("list", current_mode, "kill"),
                ),
                InlineKeyboardButton(
                    "🍴 Fork…",
                    callback_data=encode("list", current_mode, "fork"),
                ),
            ],
        ]
    )


def _send_list_detail(chat_id: str, entry: Any) -> None:
    text = _format_list_detail(entry)
    agent_name = getattr(entry, "name", None)
    keyboard = None
    if isinstance(agent_name, str) and agent_name:
        prompt = _get_agent_retry_prompt(agent_name) or getattr(entry, "prompt", None)
        keyboard = _build_agent_action_keyboard(
            agent_name,
            prompt_for_vcs=prompt,
            retry_source_prompt=prompt,
            include_kill=not bool(getattr(entry, "is_terminal", False)),
        )
    telegram_client.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


def _format_list_detail(entry: Any) -> str:
    prompt = _get_detail_prompt(entry)
    return format_agent_detail(entry, prompt=prompt)


def _get_detail_prompt(entry: Any) -> str | None:
    name = getattr(entry, "name", None)
    prompt = _get_agent_retry_prompt(name) if isinstance(name, str) and name else None
    if not prompt:
        prompt = getattr(entry, "prompt", None)
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return display_cl_names_in_text(prompt.strip())
