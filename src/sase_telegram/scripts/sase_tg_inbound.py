"""Inbound chop entry point: poll Telegram for user actions."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from sase_telegram import credentials, pending_actions, telegram_client
from sase_telegram.callback_data import decode, encode
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from sase_telegram.formatting import escape_markdown_v2
from sase_telegram.inbound import (
    IMAGES_DIR,
    ResponseAction,
    build_photo_prompt,
    clear_awaiting_feedback,
    get_last_offset,
    make_image_filename,
    process_callback,
    process_callback_twostep,
    process_text_message,
    reconstruct_code_markers,
    save_awaiting_feedback,
    save_offset,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sase_tg_inbound",
        description="Poll Telegram for user action responses",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process pending updates once and exit (no long-polling)",
    )
    parser.add_argument(
        "--context",
        default=None,
        help="Optional context string for lumberjack compatibility",
    )
    return parser.parse_args(argv)


def _write_response(response: ResponseAction) -> None:
    """Write a response JSON file to disk."""
    response.response_path.parent.mkdir(parents=True, exist_ok=True)
    response.response_path.write_text(json.dumps(response.response_data, indent=2))


def _handle_callback(callback_query: Any, pending: dict[str, Any]) -> None:
    """Handle an inline keyboard button press."""
    data_str: str = callback_query.data

    # Handle kill callbacks (agent management, not notification-based)
    try:
        cb = decode(data_str)
        if cb.action_type == "kill":
            _handle_kill_from_callback(callback_query, cb.notif_id_prefix)
            return
    except ValueError:
        pass

    # Check two-step first (feedback/custom -> save awaiting state)
    twostep = process_callback_twostep(data_str, pending)
    if twostep is not None:
        prefix, action_info = twostep
        save_awaiting_feedback(prefix, action_info)
        telegram_client.answer_callback_query(
            callback_query.id, "Send your feedback as a text message"
        )
        action = pending.get(prefix)
        if action:
            telegram_client.edit_message_reply_markup(
                action["chat_id"], action["message_id"], reply_markup=None
            )
        return

    # Regular one-shot callback
    response = process_callback(data_str, pending)

    if response is None:
        # Unknown or already-handled callback
        try:
            decode(data_str)
        except ValueError:
            telegram_client.answer_callback_query(callback_query.id, "Invalid callback")
            return
        telegram_client.answer_callback_query(
            callback_query.id, "This action has already been handled"
        )
        return

    # Check if the response directory still exists (expired request)
    if not response.response_path.parent.exists():
        telegram_client.answer_callback_query(
            callback_query.id, "This request has expired"
        )
        pending_actions.remove(response.notif_id_prefix)
        return

    _write_response(response)
    telegram_client.answer_callback_query(callback_query.id, response.answer_text)

    action = pending.get(response.notif_id_prefix)
    if action:
        telegram_client.edit_message_reply_markup(
            action["chat_id"], action["message_id"], reply_markup=None
        )

    pending_actions.remove(response.notif_id_prefix)


def _get_agent_retry_prompt(name: str) -> str | None:
    """Read the original prompt for a named agent from its artifact directory.

    Falls back to raw_xprompt.md when the pending action is missing (e.g. due
    to a file-level race between concurrent inbound/outbound handlers).
    Strips auto-assigned ``%n:<name>`` directives so the retry gets a fresh name.
    """
    from sase.agent_names import find_named_agent

    agent = find_named_agent(name)
    if agent is None:
        return None

    raw_path = Path(agent.artifacts_dir) / "raw_xprompt.md"
    try:
        prompt = raw_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None

    if not prompt:
        return None

    # Strip auto-assigned %n:<name> directive so the retry gets a fresh name
    return re.sub(r"^%n:\S+\s*", "", prompt)


def _send_kill_result(
    name: str,
    result: Any,
    kill_info: dict[str, Any] | None,
    *,
    prompt_fallback: str | None = None,
) -> None:
    """Send a kill confirmation (or failure) message to Telegram.

    Shared by both the Kill button callback and the .kill dot command.
    """
    import logging

    log = logging.getLogger(__name__)

    chat_id = credentials.get_chat_id()
    kill_key = f"kill-{name}"

    # Remove keyboard from the original launch message
    if kill_info:
        try:
            telegram_client.edit_message_reply_markup(
                kill_info["chat_id"],
                kill_info["message_id"],
                reply_markup=None,
            )
        except Exception:
            pass  # Message may have been deleted or already edited

    try:
        if result.success:
            escaped_name = escape_markdown_v2(name)
            retry_prompt = (
                kill_info.get("prompt") if kill_info else None
            ) or prompt_fallback
            keyboard: InlineKeyboardMarkup | None = None
            if retry_prompt:
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Retry",
                                copy_text=CopyTextButton(text=retry_prompt),
                            ),
                        ]
                    ]
                )
            telegram_client.send_message(
                chat_id,
                f"💀 *Agent @{escaped_name} terminated*",
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
        else:
            escaped_msg = escape_markdown_v2(result.message)
            telegram_client.send_message(
                chat_id,
                f"⚠️ *Kill failed:* {escaped_msg}",
                parse_mode="MarkdownV2",
            )
    except Exception:
        log.exception("Failed to send kill result message for agent %s", name)
    finally:
        if kill_info:
            pending_actions.remove(kill_key)


def _handle_kill_from_callback(callback_query: Any, agent_name: str) -> None:
    """Handle a Kill button press from a launch message."""
    from sase.agent_names import kill_named_agent

    kill_key = f"kill-{agent_name}"
    kill_info = pending_actions.get(kill_key)

    # Read prompt fallback from agent artifacts BEFORE killing (the agent
    # must still be findable).  Only needed when pending_actions lost the entry.
    prompt_fallback = (
        _get_agent_retry_prompt(agent_name)
        if not (kill_info and kill_info.get("prompt"))
        else None
    )

    result = kill_named_agent(agent_name)

    try:
        telegram_client.answer_callback_query(
            callback_query.id,
            "Agent killed" if result.success else result.message,
        )
    except Exception:
        pass  # Callback popup is best-effort; confirmation message matters more

    _send_kill_result(agent_name, result, kill_info, prompt_fallback=prompt_fallback)


def _launch_agent(prompt: str) -> None:
    """Launch a background sase agent from a Telegram prompt."""
    # Expand xprompts to discover embedded directives (e.g. %model inside #mentor)
    try:
        from sase.xprompt import process_xprompt_references

        expanded = process_xprompt_references(prompt)
    except Exception:
        expanded = prompt

    # Check for multi-model directive (e.g. %m(opus,sonnet))
    from sase.xprompt.directives import split_prompt_for_models

    model_prompts = split_prompt_for_models(expanded)
    if model_prompts is not None:
        _launch_multi_model_agents(model_prompts)
        return

    _launch_single_agent(prompt, expanded)


def _launch_single_agent(prompt: str, expanded: str | None = None) -> None:
    """Launch a single background sase agent from a Telegram prompt."""
    from sase.agent_launcher import launch_agent_from_cwd
    from sase.agent_names import get_next_auto_name
    from sase.llm_provider.registry import (
        format_provider_model_label,
        get_default_provider_name,
        get_provider,
        resolve_model_provider,
    )
    from sase.xprompt.directives import extract_prompt_directives

    if expanded is None:
        try:
            from sase.xprompt import process_xprompt_references

            expanded = process_xprompt_references(prompt)
        except Exception:
            expanded = prompt
    _, directives = extract_prompt_directives(expanded)

    # Save original prompt before modification (for kill-retry copy button)
    original_prompt = prompt

    # Auto-assign a name if the user didn't provide one
    auto_name: str | None = None
    if directives.name is None:
        auto_name = get_next_auto_name()
        prompt = f"%n:{auto_name} {prompt}"

    # Resolve provider/model for the launch label
    if directives.model:
        provider, model = resolve_model_provider(directives.model)
        provider = provider or get_default_provider_name()
    else:
        provider = get_default_provider_name()
        model = get_provider().resolve_model_name()
    label = format_provider_model_label(provider, model)

    chat_id = credentials.get_chat_id()
    try:
        result = launch_agent_from_cwd(prompt)
        display = prompt[:200] + ("..." if len(prompt) > 200 else "")
        escaped_label = escape_markdown_v2(label)
        agent_name = directives.name or auto_name
        if agent_name:
            escaped_name = escape_markdown_v2(agent_name)
            name_line = f"  _@{escaped_name}_"
        else:
            name_line = ""
        meta = escape_markdown_v2(f"workspace #{result.workspace_num}")
        escaped_display = escape_markdown_v2(display)
        keyboard: InlineKeyboardMarkup | None = None
        if agent_name:
            from sase.xprompt import extract_vcs_workflow_tag

            vcs_prefix = ""
            vcs_tag = extract_vcs_workflow_tag(prompt)
            if vcs_tag:
                vcs_prefix = f"{vcs_tag}"
            resume_text = f"{vcs_prefix}#resume:{agent_name} %w:{agent_name} "
            wait_text = f"{vcs_prefix}%w:{agent_name} "
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "▶️ Resume",
                            copy_text=CopyTextButton(text=resume_text),
                        ),
                        InlineKeyboardButton(
                            "⏳ Wait",
                            copy_text=CopyTextButton(text=wait_text),
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "🗡️ Kill",
                            callback_data=encode("kill", agent_name, "go"),
                        ),
                    ],
                ]
            )
        msg = telegram_client.send_message(
            chat_id,
            f"🚀 *{escaped_label} Launched*{name_line}\n{meta}\n\n{escaped_display}",
            parse_mode="MarkdownV2",
            reply_markup=keyboard,
        )
        if agent_name:
            pending_actions.add(
                f"kill-{agent_name}",
                {
                    "action": "kill",
                    "agent_name": agent_name,
                    "prompt": original_prompt,
                    "message_id": msg.message_id,
                    "chat_id": chat_id,
                },
            )
    except Exception as e:
        telegram_client.send_message(
            chat_id,
            f"Failed to launch agent: {e}",
        )


def _launch_multi_model_agents(model_prompts: list[str]) -> None:
    """Launch one agent per model for a multi-model directive.

    Each prompt in *model_prompts* has the multi-model directive replaced
    with a single ``%model:X``.  Each agent gets its own auto-name and
    a separate Telegram notification.
    """
    import time

    for i, model_prompt in enumerate(model_prompts):
        if i > 0:
            time.sleep(1)
        _launch_single_agent(model_prompt)


def _handle_photo_message(message: Any) -> None:
    """Handle a photo message: download and launch agent."""
    photo = message.photo[-1]  # highest resolution
    file_id: str = photo.file_id
    filename = make_image_filename(file_id)
    dest = IMAGES_DIR / filename

    chat_id = credentials.get_chat_id()
    try:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        telegram_client.download_file(file_id, dest)
    except Exception as e:
        telegram_client.send_message(chat_id, f"Failed to download photo: {e}")
        return

    caption = (
        reconstruct_code_markers(message.caption, message.caption_entities)
        if message.caption
        else message.caption
    )
    prompt = build_photo_prompt(dest, caption)
    _launch_agent(prompt)


def _handle_document_image(message: Any) -> None:
    """Handle an image sent as a document: download and launch agent."""
    doc = message.document
    file_id: str = doc.file_id
    original_name = doc.file_name or "image.jpg"
    ts = make_image_filename(file_id).split("_", 2)  # extract timestamp parts
    filename = f"{ts[0]}_{ts[1]}_{original_name}"
    dest = IMAGES_DIR / filename

    chat_id = credentials.get_chat_id()
    try:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        telegram_client.download_file(file_id, dest)
    except Exception as e:
        telegram_client.send_message(chat_id, f"Failed to download image: {e}")
        return

    caption = (
        reconstruct_code_markers(message.caption, message.caption_entities)
        if message.caption
        else message.caption
    )
    prompt = build_photo_prompt(dest, caption)
    _launch_agent(prompt)


def _handle_dot_command(text: str) -> None:
    """Dispatch a Telegram dot command (e.g. '.kill agent') to the appropriate handler."""
    parts = text.split(None, 1)
    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if command == ".kill":
        _handle_kill_command(args)
    elif command == ".list":
        _handle_list_command()
    elif command == ".listx":
        _handle_listx_command()
    # Unknown commands are silently ignored (preserves original behavior)


def _handle_kill_command(args: str) -> None:
    """Handle .kill <agent_name> — terminate a running agent by name."""
    from sase.agent_names import kill_named_agent

    chat_id = credentials.get_chat_id()
    name = args.strip()
    if not name:
        telegram_client.send_message(chat_id, "Usage: .kill <agent_name>")
        return

    kill_key = f"kill-{name}"
    kill_info = pending_actions.get(kill_key)

    # Read prompt fallback from agent artifacts BEFORE killing (the agent
    # must still be findable).  Only needed when pending_actions lost the entry.
    prompt_fallback = (
        _get_agent_retry_prompt(name)
        if not (kill_info and kill_info.get("prompt"))
        else None
    )

    result = kill_named_agent(name)
    _send_kill_result(name, result, kill_info, prompt_fallback=prompt_fallback)


def _handle_list_command() -> None:
    """Handle .list — show all currently running agents."""
    import html

    from sase.agent_names import list_running_agents

    chat_id = credentials.get_chat_id()
    agents = list_running_agents()

    if not agents:
        telegram_client.send_message(chat_id, "No running agents.")
        return

    blocks: list[str] = [f"<b>{len(agents)} Running Agent(s)</b>"]
    for a in agents:
        label = html.escape(a.name or "(unnamed)")
        model = html.escape(a.model or "?")

        details: list[str] = []
        if a.project:
            details.append(html.escape(a.project))
        if a.workspace_num is not None:
            details.append(f"ws#{a.workspace_num}")
        if a.pid is not None:
            details.append(f"PID {a.pid}")
        if a.approve:
            details.append("autonomous")

        block = f"<b>{label}</b>  {model}, {a.duration}"
        if details:
            block += f"\n{' · '.join(details)}"
        if a.prompt:
            snippet = a.prompt.replace("\n", " ")
            if len(snippet) > 120:
                snippet = snippet[:120] + "…"
            block += f"\n<i>{html.escape(snippet)}</i>"
        blocks.append(block)

    telegram_client.send_message(chat_id, "\n\n".join(blocks), parse_mode="HTML")


def _handle_listx_command() -> None:
    """Handle .listx — show done but not yet dismissed agents."""
    import html

    from sase.ace.dismissed_agents import load_dismissed_agents
    from sase.ace.tui.models.agent_loader import load_all_agents

    _DISMISSABLE_STATUSES = {"DONE", "FAILED", "PLAN DONE"}

    chat_id = credentials.get_chat_id()
    all_agents = load_all_agents()
    dismissed = load_dismissed_agents()

    done_agents = [
        a
        for a in all_agents
        if a.status in _DISMISSABLE_STATUSES
        and not a.is_workflow_child
        and a.identity not in dismissed
    ]

    if not done_agents:
        telegram_client.send_message(chat_id, "No done agents.")
        return

    blocks: list[str] = [f"<b>{len(done_agents)} Done Agent(s)</b>"]
    for a in done_agents:
        label = html.escape(a.agent_name or a.cl_name)
        model = html.escape(a.model or "?")

        details: list[str] = []
        if a.status != "DONE":
            details.append(a.status)
        if a.effective_workspace_num is not None:
            details.append(f"ws#{a.effective_workspace_num}")

        block = f"<b>{label}</b>  {model}, {a.duration_display}"
        if details:
            block += f"\n{' · '.join(details)}"

        # Show raw xprompt snippet if available
        raw = a.get_raw_xprompt_content()
        if raw:
            snippet = raw.replace("\n", " ").strip()
            if len(snippet) > 120:
                snippet = snippet[:120] + "…"
            block += f"\n<i>{html.escape(snippet)}</i>"

        blocks.append(block)

    telegram_client.send_message(chat_id, "\n\n".join(blocks), parse_mode="HTML")


def _handle_text_message(text: str) -> None:
    """Handle a text message: feedback completion, or new agent launch."""
    response = process_text_message(text)
    if response is not None:
        _write_response(response)
        clear_awaiting_feedback()
        pending_actions.remove(response.notif_id_prefix)
        return

    # Dispatch Telegram dot commands (e.g. ".kill agent")
    if text.startswith("."):
        _handle_dot_command(text)
        return

    # Ignore Telegram bot commands (e.g. /start, /help)
    if text.startswith("/"):
        return

    # Launch a new agent with this text as the prompt
    _launch_agent(text)


def main(argv: list[str] | None = None) -> int:
    """Inbound Telegram chop entry point."""
    _parse_args(argv)

    # Clean up stale pending actions
    pending_actions.cleanup_stale()

    pending = pending_actions.list_all()
    offset = get_last_offset()
    updates = telegram_client.get_updates(offset=offset, timeout=0)

    if not updates:
        return 0

    # Save offset BEFORE processing to prevent duplicate agent launches when
    # overlapping invocations race (at-most-once delivery).
    last_update_id = max(u.update_id for u in updates)
    save_offset(last_update_id + 1)

    for update in updates:
        if update.callback_query:
            _handle_callback(update.callback_query, pending)
        elif update.message:
            msg = update.message
            if msg.photo:
                _handle_photo_message(msg)
            elif (
                msg.document
                and msg.document.mime_type
                and msg.document.mime_type.startswith("image/")
            ):
                _handle_document_image(msg)
            elif msg.text:
                text = reconstruct_code_markers(msg.text, msg.entities)
                _handle_text_message(text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
