"""/kill, kill picker, retry, and /fork."""

from __future__ import annotations

from typing import Any
from sase_telegram import credentials, pending_actions, telegram_client
from sase_telegram.callback_data import encode, generate_key
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from sase_telegram.formatting import (
    display_cl_name,
    display_cl_names_in_text,
    display_vcs_refs_in_text,
    escape_markdown_v2,
)
from sase_telegram.inbound_handlers.common import _COPY_TEXT_MAX
from sase_telegram.inbound_handlers.agent_launch import _get_agent_retry_prompt

import logging

log = logging.getLogger(__name__)


_KILL_SELECTION_PENDING_KEY = "kill-selection"


_KILL_SELECTION_CHOICE = "select"


def _build_redo_prompt_for_killed_agent(source_prompt: str | None) -> str | None:
    """Return Telegram copy/send text for redoing a killed agent's prompt."""
    redo_prompt = source_prompt.strip() if source_prompt is not None else ""
    return redo_prompt or None


def _send_kill_result(
    name: str,
    result: Any,
    kill_info: dict[str, Any] | None,
    *,
    prompt_fallback: str | None = None,
) -> None:
    """Send a kill confirmation (or failure) message to Telegram.

    Shared by both the Kill button callback and the /kill command.
    """
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
            escaped_name = escape_markdown_v2(display_cl_name(name))
            redo_source_prompt = (
                kill_info.get("prompt") if kill_info else None
            ) or prompt_fallback
            redo_prompt = _build_redo_prompt_for_killed_agent(redo_source_prompt)
            if redo_prompt:
                redo_prompt = display_vcs_refs_in_text(redo_prompt)
            # Telegram CopyTextButton limit is 256 characters
            keyboard: InlineKeyboardMarkup | None = None
            if redo_prompt and len(redo_prompt) <= _COPY_TEXT_MAX:
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Redo",
                                copy_text=CopyTextButton(text=redo_prompt),
                            ),
                        ]
                    ]
                )
            elif redo_prompt:
                # Prompt too long for CopyTextButton — use a callback button
                # that sends the prompt as a new message when pressed.
                retry_key = f"retry-{name}"
                pending_actions.add(
                    retry_key,
                    {"action": "retry", "prompt": redo_prompt},
                )
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Redo",
                                callback_data=encode("retry", name, "go"),
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
            escaped_msg = escape_markdown_v2(
                display_cl_names_in_text(str(result.message))
            )
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
    from sase.agent.running import kill_named_agent

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


def _handle_kill_selection_from_callback(
    callback_query: Any,
    selection_key: str,
) -> None:
    """Resolve a persisted /kill selection key and kill its agent."""
    selection = pending_actions.get(_KILL_SELECTION_PENDING_KEY)
    agent_names = selection.get("agent_names") if selection else None
    agent_name = (
        agent_names.get(selection_key) if isinstance(agent_names, dict) else None
    )
    if not isinstance(agent_name, str) or not agent_name:
        telegram_client.answer_callback_query(
            callback_query.id,
            "This kill selection has expired",
        )
        return

    _handle_kill_from_callback(callback_query, agent_name)


def _handle_retry_from_callback(callback_query: Any, agent_name: str) -> None:
    """Handle a Retry button press: send the stored retry prompt as a message."""
    retry_key = f"retry-{agent_name}"
    retry_info = pending_actions.get(retry_key)

    if not retry_info or not retry_info.get("prompt"):
        telegram_client.answer_callback_query(
            callback_query.id, "Retry prompt no longer available"
        )
        return

    chat_id = credentials.get_chat_id()
    prompt = retry_info["prompt"]
    telegram_client.send_message(chat_id, prompt)
    telegram_client.answer_callback_query(callback_query.id, "Prompt sent")
    pending_actions.remove(retry_key)


def _format_agent_description(
    name: str, model: str, duration: str, prompt: str | None, status: str | None = None
) -> str:
    """Format an HTML description block for an agent.

    Used by /kill and /fork to show context above the inline buttons.
    """
    import html

    label = html.escape(display_cl_name(name))
    model_esc = html.escape(model or "?")
    line = f"<b>{label}</b>  {model_esc}, {duration}"
    if status and status != "DONE":
        line += f" · {html.escape(status)}"
    if prompt:
        snippet = display_cl_names_in_text(prompt.replace("\n", " ").strip())
        if len(snippet) > 80:
            snippet = snippet[:80] + "…"
        line += f"\n<i>{html.escape(snippet)}</i>"
    return line


def _show_kill_selection(chat_id: str) -> None:
    """Show an inline keyboard of running agents to kill."""
    from sase.agent.running import list_running_agents

    agents = list_running_agents()
    if not agents:
        telegram_client.send_message(chat_id, "No running agents.")
        return

    named_agents = [(a, a.name) for a in agents if a.name]
    if not named_agents:
        telegram_client.send_message(chat_id, "No named agents to kill.")
        return

    descriptions: list[str] = []
    buttons: list[list[InlineKeyboardButton]] = []
    agent_names: dict[str, str] = {}
    for agent, name in named_agents:
        try:
            selection_key = generate_key()
            if selection_key in agent_names:
                raise ValueError(f"Duplicate kill selection key: {selection_key!r}")
            callback_data = encode(
                "kill",
                selection_key,
                _KILL_SELECTION_CHOICE,
            )
        except Exception:
            log.warning(
                "Skipping /kill selection button for agent %s",
                name,
                exc_info=True,
            )
            continue

        agent_names[selection_key] = name
        descriptions.append(
            _format_agent_description(
                name,
                agent.model or "?",
                agent.duration,
                agent.prompt,
            )
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    display_cl_name(name),
                    callback_data=callback_data,
                )
            ]
        )

    if not buttons:
        telegram_client.send_message(chat_id, "No selectable agents to kill.")
        return

    text = "Select an agent to kill:\n\n" + "\n\n".join(descriptions)
    pending_actions.add(
        _KILL_SELECTION_PENDING_KEY,
        {
            "action": _KILL_SELECTION_PENDING_KEY,
            "agent_names": agent_names,
        },
    )

    telegram_client.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _handle_kill_command(args: str) -> None:
    """Handle /kill [agent_name] — terminate a running agent by name."""
    from sase.agent.running import kill_named_agent

    chat_id = credentials.get_chat_id()
    name = args.strip()
    if not name:
        _show_kill_selection(chat_id)
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


def _handle_fork_command() -> None:
    """Handle /fork — show copy buttons to fork currently-running agents."""
    from sase.agent.running import list_running_agents
    from sase.project_tags import effective_vcs_workflow_tag

    chat_id = credentials.get_chat_id()

    agents = list_running_agents()
    named_agents = [(a, a.name) for a in agents if a.name]
    if not named_agents:
        telegram_client.send_message(chat_id, "No running agents to fork.")
        return

    buttons: list[list[InlineKeyboardButton]] = []
    for a, name in named_agents:
        vcs_prefix = ""
        if a.prompt:
            vcs_tag = effective_vcs_workflow_tag(a.prompt)
            if vcs_tag:
                vcs_prefix = display_vcs_refs_in_text(vcs_tag)
        # #fork:<name> implies %w:<name>; no explicit wait directive needed.
        fork_text = f"{vcs_prefix}#fork:{name} "
        buttons.append(
            [
                InlineKeyboardButton(
                    f"🍴 {display_cl_name(name)}",
                    copy_text=CopyTextButton(text=fork_text),
                )
            ]
        )

    descriptions = [
        _format_agent_description(name, a.model or "?", a.duration, a.prompt)
        for a, name in named_agents
    ]
    text = "Select an agent to fork:\n\n" + "\n\n".join(descriptions)

    telegram_client.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
