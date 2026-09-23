"""/changes and /xprompts commands."""

from __future__ import annotations

from typing import Any
from sase_telegram import credentials, telegram_client
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from sase_telegram.formatting import (
    display_cl_name,
    display_project_name,
    display_vcs_refs_in_text,
)

import logging

log = logging.getLogger(__name__)


_CHANGES_BUTTON_CHUNK_SIZE = 50


def _list_patch_xprompt_tags(project: str | None = None) -> Any:
    from sase.integrations.patch_tags import list_patch_xprompt_tags

    return list_patch_xprompt_tags(project)


def _handle_changes_command(args: str) -> None:
    """Handle /changes [project] — show copy buttons for Patch tags."""
    chat_id = credentials.get_chat_id()
    project_parts = args.split()
    if len(project_parts) > 1:
        telegram_client.send_message(chat_id, "Usage: /changes [project]")
        return

    project = project_parts[0] if project_parts else None
    listing = _list_patch_xprompt_tags(project)
    entries = list(listing.entries)
    skipped = list(listing.skipped)

    if not entries:
        message = (
            "No active Patches."
            if project is None
            else f"No active Patches for {display_project_name(project)}."
        )
        if skipped:
            message += f"\n{_format_patch_skipped_note(len(skipped))}"
        telegram_client.send_message(chat_id, message)
        return

    total = len(entries)
    for start in range(0, total, _CHANGES_BUTTON_CHUNK_SIZE):
        chunk = entries[start : start + _CHANGES_BUTTON_CHUNK_SIZE]
        header = (
            f"Active Patches for {display_project_name(project)} ({total})"
            if project is not None
            else f"Active Patches ({total})"
        )
        if total > _CHANGES_BUTTON_CHUNK_SIZE:
            end = start + len(chunk)
            header += f"\nShowing {start + 1}-{end} of {total}"
        if skipped and start == 0:
            header += f"\n{_format_patch_skipped_note(len(skipped))}"

        buttons = [
            [
                InlineKeyboardButton(
                    _changes_button_label(entry, filtered=project is not None),
                    copy_text=CopyTextButton(text=display_vcs_refs_in_text(entry.tag)),
                )
            ]
            for entry in chunk
        ]
        telegram_client.send_message(
            chat_id,
            header,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


def _format_patch_skipped_note(skipped_count: int) -> str:
    plural = "" if skipped_count == 1 else "s"
    return (
        f"Skipped {skipped_count} active Patch{plural} "
        "with unavailable workflow metadata."
    )


def _changes_button_label(entry: Any, *, filtered: bool) -> str:
    entry_name = display_cl_name(entry.name)
    label = (
        entry_name
        if filtered
        else f"{display_project_name(entry.project)}/{entry_name}"
    )
    if len(label) <= 64:
        return label
    return label[:61] + "..."


def _format_xprompts_caption(stats: Any) -> str:
    """Format an HTML caption summarising a CatalogStats object."""
    import html

    by_source = stats.by_source
    lines = [
        "📚 <b>xprompts Catalog</b>",
        "",
        f"<b>{stats.total}</b> xprompts across <b>{len(stats.by_project)}</b> projects",
        "",
        f"• Built-in:     {by_source.get('built-in', 0)}",
        f"• Project:      {by_source.get('project', 0)}",
        f"• Config:       {by_source.get('config', 0)}",
        f"• Plugin:       {by_source.get('plugin', 0)}",
        f"• Memory (auto): {by_source.get('memory', 0)}",
    ]

    top_tags_line: str | None = None
    if stats.by_tag:
        top = sorted(stats.by_tag.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        top_tags_html = " · ".join(
            f"<code>#{html.escape(tag)}</code>" for tag, _ in top
        )
        top_tags_line = f"Top tags: {top_tags_html}"

    lines.append("")
    if top_tags_line:
        lines.append(top_tags_line)
        lines.append("")
    lines.append(f"Generated {stats.generated_at.date().isoformat()}")

    caption = "\n".join(lines)
    if top_tags_line and len(caption) > 1000:
        lines_no_tags = [ln for ln in lines if ln != top_tags_line]
        caption = "\n".join(lines_no_tags)
    return caption


def _handle_xprompts_command() -> None:
    """Handle /xprompts — build and send the xprompts PDF catalog."""
    chat_id = credentials.get_chat_id()
    telegram_client.send_message(chat_id, "📚 Building your xprompts catalog…")

    try:
        from sase.xprompt.catalog import (
            NoXpromptsFound,
            PdfEngineUnavailable,
            build_xprompts_catalog,
        )
    except ImportError:
        log.exception("Failed to import sase.xprompt.catalog")
        telegram_client.send_message(
            chat_id,
            "Failed to build xprompts catalog: ImportError. See bot logs for details.",
        )
        return

    try:
        artifact = build_xprompts_catalog()
    except PdfEngineUnavailable:
        log.exception("PDF engine unavailable for /xprompts")
        telegram_client.send_message(
            chat_id,
            "PDF engine (wkhtmltopdf/pandoc) not installed on the bot host — "
            "cannot render the catalog PDF.",
        )
        return
    except NoXpromptsFound:
        log.exception("No xprompts found for /xprompts")
        telegram_client.send_message(
            chat_id,
            "No xprompts found — unexpected, file a bug.",
        )
        return
    except Exception as exc:
        log.exception("Failed to build xprompts catalog")
        telegram_client.send_message(
            chat_id,
            f"Failed to build xprompts catalog: {type(exc).__name__}. "
            "See bot logs for details.",
        )
        return

    caption = _format_xprompts_caption(artifact.stats)
    telegram_client.send_document(
        chat_id,
        str(artifact.pdf_path),
        caption=caption,
        parse_mode="HTML",
    )
