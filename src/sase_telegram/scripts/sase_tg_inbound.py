"""Inbound chop entry point: poll Telegram for user actions."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sase_telegram import credentials, pending_actions, telegram_client
from sase_telegram.callback_data import decode
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


def _handle_callback(
    callback_query: Any, pending: dict[str, Any]
) -> None:
    """Handle an inline keyboard button press."""
    data_str: str = callback_query.data

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
            telegram_client.answer_callback_query(
                callback_query.id, "Invalid callback"
            )
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
        name_label = f" [{auto_name}]" if auto_name else ""
        telegram_client.send_message(
            chat_id,
            f"{label} launched{name_label} (PID {result.pid}, workspace #{result.workspace_num})\n\n{display}",
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

    caption = reconstruct_code_markers(
        message.caption, message.caption_entities
    ) if message.caption else message.caption
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

    caption = reconstruct_code_markers(
        message.caption, message.caption_entities
    ) if message.caption else message.caption
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
    # Unknown commands are silently ignored (preserves original behavior)


def _handle_kill_command(args: str) -> None:
    """Handle /kill <agent_name> — terminate a running agent by name."""
    from sase.agent_names import kill_named_agent

    chat_id = credentials.get_chat_id()
    name = args.strip()
    if not name:
        telegram_client.send_message(chat_id, "Usage: .kill <agent_name>")
        return

    result = kill_named_agent(name)
    telegram_client.send_message(chat_id, result.message)


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

    telegram_client.send_message(
        chat_id, "\n\n".join(blocks), parse_mode="HTML"
    )


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
