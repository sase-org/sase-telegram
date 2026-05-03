"""Inbound chop entry point: poll Telegram for user actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from sase_telegram import credentials, pending_actions, telegram_client
from sase_telegram.bead_format import bead_show_to_markdown, parse_bead_list_output
from sase_telegram.callback_data import decode, encode
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from sase.integrations.chat_install import start_chat_install_worker
from sase_telegram.formatting import escape_markdown_v2, markdown_to_telegram_v2
from sase_telegram.inbound import (
    IMAGES_DIR,
    ResponseAction,
    build_photo_prompt,
    clear_awaiting_feedback,
    clear_awaiting_feedback_by_prefix,
    confirmation_text,
    find_externally_handled,
    get_last_offset,
    make_image_filename,
    process_callback,
    process_callback_twostep,
    process_text_message,
    reconstruct_code_markers,
    save_awaiting_feedback,
    save_offset,
)

log = logging.getLogger(__name__)

# File-based cache for set_my_commands to avoid Telegram rate limits.
_COMMANDS_REGISTERED_PATH = (
    Path.home() / ".sase" / "telegram" / "commands_registered_ts"
)
_COMMANDS_REGISTER_INTERVAL = 3600  # re-register once per hour
_COPY_TEXT_MAX = 256  # Telegram CopyTextButton character limit
_CHANGES_BUTTON_CHUNK_SIZE = 50
_BEAD_PROJECT_ENV = "SASE_TELEGRAM_BEAD_PROJECT"
_PROJECT_CONTEXT_PATH = Path.home() / ".sase" / "telegram" / "project_context.json"
_KNOWN_VCS_WORKFLOWS = ("gh", "git", "hg", "jj", "p4")
_VCS_PROJECT_RE = re.compile(
    rf"(?:^|(?<=\s)|(?<=[(\"']))#(?P<workflow>{'|'.join(_KNOWN_VCS_WORKFLOWS)})"
    r"(?:!!|\?\?)?"
    r"(?:(?::|_)(?P<ref>[A-Za-z0-9][A-Za-z0-9_.~/-]*)|"
    r"\((?P<paren>[A-Za-z0-9][A-Za-z0-9_.~/-]*)\))"
    r"(?=\s|$)",
    re.IGNORECASE,
)
_DIRECTIVE_PREFIX_RE = re.compile(r"^(?:%\S+\s+)+")


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


def _extract_project_from_prompt(prompt: str) -> str | None:
    """Extract a project name from the first VCS workflow tag in *prompt*."""
    text = prompt.lstrip()
    directive_match = _DIRECTIVE_PREFIX_RE.match(text)
    if directive_match:
        text = text[directive_match.end() :]

    match = _VCS_PROJECT_RE.search(text)
    if not match:
        return None

    project = match.group("ref") or match.group("paren")
    if not project or project.startswith("@"):
        return None
    return project


def _message_chat_id(message: Any | None) -> str | None:
    if message is None:
        return None
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None) if chat is not None else None
    if chat_id is None:
        chat_id = getattr(message, "chat_id", None)
    return str(chat_id) if chat_id is not None else None


def _configured_chat_id() -> str | None:
    try:
        chat_id = credentials.get_chat_id()
    except Exception:
        return None
    return str(chat_id) if chat_id is not None else None


def _context_chat_id(message: Any | None) -> str | None:
    return _message_chat_id(message) or _configured_chat_id()


def _load_project_context() -> dict[str, Any]:
    try:
        data = json.loads(_PROJECT_CONTEXT_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        log.warning("Failed to load Telegram project context", exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _save_project_context(context: dict[str, Any]) -> None:
    try:
        _PROJECT_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROJECT_CONTEXT_PATH.write_text(
            json.dumps(context, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        log.warning("Failed to save Telegram project context", exc_info=True)


def _record_project_context(
    prompt: str, message: Any | None, *, source: str = "launch_prompt"
) -> None:
    chat_id = _context_chat_id(message)
    if not chat_id:
        return

    project = _extract_project_from_prompt(prompt)
    if not project:
        return

    workspace = _resolve_workspace_for_project(project, source) or ""
    context = _load_project_context()
    context[chat_id] = {
        "project": project,
        "workspace": workspace,
        "updated_at": time.time(),
        "source": source,
    }
    _save_project_context(context)


def _workspace_from_context_entry(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None

    workspace = entry.get("workspace")
    if isinstance(workspace, str) and workspace and Path(workspace).is_dir():
        return workspace

    project = entry.get("project")
    if isinstance(project, str) and project.strip():
        return _resolve_workspace_for_project(
            project.strip(), "Telegram project context"
        )
    return None


def _pending_action_chat_id(action: dict[str, Any]) -> str | None:
    chat_id = action.get("chat_id")
    if chat_id is None:
        action_data = action.get("action_data")
        if isinstance(action_data, dict):
            chat_id = action_data.get("chat_id")
    return str(chat_id) if chat_id is not None else None


def _iter_pending_prompts(chat_id: str | None = None) -> list[str]:
    """Return pending Telegram prompts, newest first."""
    try:
        pending = pending_actions.list_all()
    except Exception:
        log.warning("Failed to load pending Telegram actions", exc_info=True)
        return []

    prompts: list[str] = []
    for action in sorted(
        pending.values(),
        key=lambda item: item.get("created_at", 0) if isinstance(item, dict) else 0,
        reverse=True,
    ):
        if not isinstance(action, dict):
            continue
        if chat_id is not None and _pending_action_chat_id(action) != chat_id:
            continue

        prompt = action.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            prompts.append(prompt)

        action_data = action.get("action_data")
        if isinstance(action_data, dict):
            nested_prompt = action_data.get("prompt")
            if isinstance(nested_prompt, str) and nested_prompt.strip():
                prompts.append(nested_prompt)

    return prompts


def _resolve_workspace_from_project_file(project: str) -> str | None:
    project_file = Path.home() / ".sase" / "projects" / project / f"{project}.gp"
    try:
        for line in project_file.read_text().splitlines():
            if not line.startswith("WORKSPACE_DIR:"):
                continue
            workspace_dir = line.split(":", 1)[1].strip()
            if workspace_dir and Path(workspace_dir).is_dir():
                return workspace_dir
            return None
    except OSError:
        return None
    return None


def _resolve_workspace_for_project(project: str, source: str) -> str | None:
    try:
        from sase.running_field import get_workspace_directory

        return get_workspace_directory(project, 1)
    except Exception:
        workspace_dir = _resolve_workspace_from_project_file(project)
        if workspace_dir:
            log.info("Resolved bead project %r from project WORKSPACE_DIR", project)
            return workspace_dir

        log.warning(
            "Failed to resolve bead project %r from %s",
            project,
            source,
            exc_info=True,
        )
        return None


def _resolve_bead_cwd(message: Any | None = None) -> str | None:
    """Resolve the working directory for ``sase bead`` subprocesses."""
    override = os.environ.get(_BEAD_PROJECT_ENV, "").strip()
    if override:
        return _resolve_workspace_for_project(override, _BEAD_PROJECT_ENV)

    chat_id = _context_chat_id(message)
    if chat_id:
        cwd = _workspace_from_context_entry(_load_project_context().get(chat_id))
        if cwd:
            return cwd

    pending_prompt_sets = (
        [_iter_pending_prompts(chat_id), _iter_pending_prompts()]
        if chat_id
        else [_iter_pending_prompts()]
    )
    for prompts in pending_prompt_sets:
        for prompt in prompts:
            project = _extract_project_from_prompt(prompt)
            if not project:
                continue
            cwd = _resolve_workspace_for_project(project, "pending Telegram prompt")
            if cwd:
                return cwd

    return None


def _run_bead_command(
    args: list[str], message: Any | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``sase bead`` in the resolved project context when available."""
    cmd = ["sase", "bead", *args]
    cwd = _resolve_bead_cwd(message=message)
    if cwd is None:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


def _list_changespec_xprompt_tags(project: str | None = None) -> Any:
    from sase.integrations.changespec_tags import list_changespec_xprompt_tags

    return list_changespec_xprompt_tags(project)


def _write_response(response: ResponseAction) -> None:
    """Write a response JSON file to disk."""
    response.response_path.parent.mkdir(parents=True, exist_ok=True)
    response.response_path.write_text(json.dumps(response.response_data, indent=2))


def _send_plan_confirmation(action: dict[str, Any], choice: str) -> None:
    """Send a confirmation message with a Plan copy button after approve/commit."""
    plan_file = action.get("plan_file", "")
    if plan_file:
        project_dir = action.get("action_data", {}).get("project_dir")
        if project_dir:
            try:
                rel = str(
                    Path(plan_file).resolve().relative_to(Path(project_dir).resolve())
                )
            except ValueError:
                rel = Path(plan_file).name
        else:
            rel = Path(plan_file).name
    else:
        rel = ""

    label = "Plan approved" if choice == "approve" else "Plan committed"
    text = escape_markdown_v2(label)

    if rel:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📋 Plan",
                        copy_text=CopyTextButton(text=rel),
                    )
                ]
            ]
        )
    else:
        keyboard = None

    chat_id = action.get("chat_id")
    if chat_id:
        telegram_client.send_message(
            chat_id, text, reply_markup=keyboard, parse_mode="MarkdownV2"
        )


def _handle_callback(callback_query: Any, pending: dict[str, Any]) -> None:
    """Handle an inline keyboard button press."""
    data_str: str = callback_query.data

    # Handle kill/retry callbacks (agent management, not notification-based)
    try:
        cb = decode(data_str)
        if cb.action_type == "kill":
            _handle_kill_from_callback(callback_query, cb.notif_id_prefix)
            return
        if cb.action_type == "retry":
            _handle_retry_from_callback(callback_query, cb.notif_id_prefix)
            return
        if cb.action_type == "bead":
            _handle_bead_callback(callback_query, cb.notif_id_prefix)
            return
    except ValueError:
        pass

    # Check two-step first (feedback/custom -> save awaiting state)
    twostep = process_callback_twostep(data_str, pending)
    if twostep is not None:
        prefix, action_info = twostep
        action = pending.get(prefix)
        # Key by the originating Telegram message_id so concurrent two-step
        # flows do not overwrite each other. Fall back to the prefix when the
        # pending entry is somehow missing the message_id.
        key = (
            str(action["message_id"])
            if action and action.get("message_id") is not None
            else prefix
        )
        save_awaiting_feedback(key, prefix, action_info)
        telegram_client.answer_callback_query(
            callback_query.id, "Send your feedback as a text message"
        )
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

    # Send confirmation with Plan copy button for approve/commit
    if response.action_type == "plan" and response.response_data.get("action") in (
        "approve",
        "commit",
    ):
        if action:
            _send_plan_confirmation(action, response.response_data["action"])

    pending_actions.remove(response.notif_id_prefix)


def _get_agent_retry_prompt(name: str) -> str | None:
    """Read the original prompt for a named agent from its artifact directory.

    Falls back to raw_xprompt.md when the pending action is missing (e.g. due
    to a file-level race between concurrent inbound/outbound handlers).
    Strips auto-assigned ``%n:<name>`` directives so the retry gets a fresh name.
    """
    from sase.agent.names import find_named_agent

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
            escaped_name = escape_markdown_v2(name)
            retry_prompt = (
                kill_info.get("prompt") if kill_info else None
            ) or prompt_fallback
            # Telegram CopyTextButton limit is 256 characters
            keyboard: InlineKeyboardMarkup | None = None
            if retry_prompt and len(retry_prompt) <= _COPY_TEXT_MAX:
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
            elif retry_prompt:
                # Prompt too long for CopyTextButton — use a callback button
                # that sends the prompt as a new message when pressed.
                retry_key = f"retry-{name}"
                pending_actions.add(
                    retry_key,
                    {"action": "retry", "prompt": retry_prompt},
                )
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔄 Retry",
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


def _handle_retry_from_callback(callback_query: Any, agent_name: str) -> None:
    """Handle a Retry button press: send the original prompt as a message."""
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


def _launch_agent(prompt: str) -> None:
    """Launch a background sase agent from a Telegram prompt."""
    log.info("Launching agent for prompt: %s", prompt[:120])

    # Expand xprompts to discover embedded directives (e.g. %model inside #mentor)
    try:
        from sase.xprompt import process_xprompt_references

        expanded = process_xprompt_references(prompt)
    except Exception:
        log.warning("Failed to expand xprompts, using raw prompt", exc_info=True)
        expanded = prompt

    # Check for multi-model directive (e.g. %m(opus,sonnet))
    from sase.xprompt.directives import split_prompt_for_models

    model_prompts = split_prompt_for_models(expanded)
    if model_prompts is not None:
        log.info("Multi-model directive found, launching %d agents", len(model_prompts))
        _launch_multi_model_agents(model_prompts)
        return

    _launch_single_agent(prompt, expanded)


def _prompt_has_pr_xprompt(prompt: str) -> bool:
    """Check if a prompt contains the #pr xprompt."""
    from sase.xprompt.workflow_validator_extract import extract_xprompt_calls

    return any(call.name == "pr" for call in extract_xprompt_calls(prompt))


def _launch_single_agent(prompt: str, expanded: str | None = None) -> None:
    """Launch a single background sase agent from a Telegram prompt."""
    from sase.agent.launcher import launch_agent_from_cwd
    from sase.agent.names import get_next_auto_name
    from sase.agent.repeat_launcher import extract_repeat_and_name
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

    # A %r:N prompt fans out inside spawn_repeat_batch, which owns naming
    # for the whole batch. Prepending %n:<auto> here would turn the
    # auto-picked letter into an *explicit* base and force the strict
    # collision path — wrong when the user never asked for a specific name.
    repeat_count, _, _ = extract_repeat_and_name(expanded)
    is_repeat = repeat_count is not None and repeat_count > 1

    # Auto-assign a name if the user didn't provide one
    auto_name: str | None = None
    if directives.name is None and not is_repeat:
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
        log.info(
            "Calling launch_agent_from_cwd (model=%s, name=%s)",
            label,
            directives.name or auto_name,
        )
        result = launch_agent_from_cwd(prompt)
        log.info(
            "Agent launched: workspace #%s, PID %s", result.workspace_num, result.pid
        )
        display = prompt[:200] + ("..." if len(prompt) > 200 else "")
        escaped_label = escape_markdown_v2(label)
        agent_name = directives.name or auto_name
        if agent_name:
            escaped_name = escape_markdown_v2(agent_name)
            name_line = f"  _@{escaped_name}_"
        elif is_repeat:
            name_line = f"  _repeat×{escape_markdown_v2(str(repeat_count))}_"
        else:
            name_line = ""
        meta = escape_markdown_v2(f"workspace #{result.workspace_num}")
        escaped_display = escape_markdown_v2(display)
        keyboard: InlineKeyboardMarkup | None = None
        if agent_name:
            from sase.xprompt import extract_vcs_workflow_tag, replace_ref_in_vcs_tag

            vcs_prefix = ""
            vcs_tag = extract_vcs_workflow_tag(prompt)
            if vcs_tag:
                if _prompt_has_pr_xprompt(prompt):
                    vcs_tag = replace_ref_in_vcs_tag(vcs_tag, f"@{agent_name}")
                vcs_prefix = f"{vcs_tag}"
            resume_text = f"{vcs_prefix}#resume:{agent_name} %w:{agent_name} "
            wait_text = f"{vcs_prefix}%w:{agent_name} "
            if len(original_prompt) <= _COPY_TEXT_MAX:
                retry_button = InlineKeyboardButton(
                    "🔄 Retry",
                    copy_text=CopyTextButton(text=original_prompt),
                )
            else:
                pending_actions.add(
                    f"retry-{agent_name}",
                    {"action": "retry", "prompt": original_prompt},
                )
                retry_button = InlineKeyboardButton(
                    "🔄 Retry",
                    callback_data=encode("retry", agent_name, "go"),
                )
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
                        retry_button,
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
        log.error("Failed to launch agent: %s", e, exc_info=True)
        try:
            telegram_client.send_message(
                chat_id,
                f"Failed to launch agent: {e}",
            )
        except Exception:
            log.error("Failed to send error message to Telegram", exc_info=True)


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
        log.info("Downloaded photo to %s", dest)
    except Exception as e:
        log.error("Failed to download photo: %s", e, exc_info=True)
        telegram_client.send_message(chat_id, f"Failed to download photo: {e}")
        return

    caption = (
        reconstruct_code_markers(message.caption, message.caption_entities)
        if message.caption
        else message.caption
    )
    prompt = build_photo_prompt(dest, caption)
    _record_project_context(prompt, message)
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
    _record_project_context(prompt, message)
    _launch_agent(prompt)


def _handle_command(text: str, message: Any | None = None) -> None:
    """Dispatch a slash command (e.g. '/kill agent') to the appropriate handler."""
    parts = text.split(None, 1)
    command = parts[0][1:].split("@")[0].lower()  # strip prefix and @bot suffix
    args = parts[1] if len(parts) > 1 else ""

    if command == "kill":
        _handle_kill_command(args)
    elif command == "list":
        _handle_list_command()
    elif command == "resume":
        _handle_resume_command()
    elif command == "changes":
        _handle_changes_command(args)
    elif command == "xprompts":
        _handle_xprompts_command()
    elif command in {"bead", "beads"}:
        _handle_bead_command(args, message=message)
    elif command == "update":
        _handle_update_command()
    # Unknown commands (e.g. /start) are silently ignored


def _handle_update_command() -> None:
    """Start the detached SASE update worker and acknowledge in Telegram."""
    chat_id = credentials.get_chat_id()
    result = start_chat_install_worker()
    telegram_client.send_message(chat_id, _format_update_ack(result))


def _format_update_ack(result: Any) -> str:
    if result.status == "config_missing_command":
        return "Update not started: chat_install.command is not configured."
    if result.status == "workspace_resolution_failed":
        return "Update not started: could not resolve the primary SASE workspace."
    if result.status == "already_running":
        return "Update already running."
    if result.status == "launched":
        return result.message
    return result.message


def _format_agent_description(
    name: str, model: str, duration: str, prompt: str | None, status: str | None = None
) -> str:
    """Format an HTML description block for an agent.

    Used by /kill and /resume to show context above the inline buttons.
    """
    import html

    label = html.escape(name)
    model_esc = html.escape(model or "?")
    line = f"<b>{label}</b>  {model_esc}, {duration}"
    if status and status != "DONE":
        line += f" · {html.escape(status)}"
    if prompt:
        snippet = prompt.replace("\n", " ").strip()
        if len(snippet) > 80:
            snippet = snippet[:80] + "…"
        line += f"\n<i>{html.escape(snippet)}</i>"
    return line


def _format_agent_list_block(agent: Any) -> str:
    """Format one agent block for the informational /list response."""
    import html

    name = agent.name if isinstance(agent.name, str) and agent.name else "(unnamed)"
    model_value = agent.model if isinstance(agent.model, str) and agent.model else "?"
    duration_value = (
        agent.duration if isinstance(agent.duration, str) and agent.duration else "?"
    )
    label = html.escape(name)
    model = html.escape(model_value)
    duration = html.escape(duration_value)

    details: list[str] = []
    project = getattr(agent, "project", None)
    if isinstance(project, str) and project:
        details.append(html.escape(project))
    workspace_num = getattr(agent, "workspace_num", None)
    if isinstance(workspace_num, int):
        details.append(f"ws#{workspace_num}")
    pid = getattr(agent, "pid", None)
    if isinstance(pid, int):
        details.append(f"PID {pid}")
    if getattr(agent, "approve", False) is True:
        details.append("autonomous")

    block = f"<b>{label}</b>  {model}, {duration}"
    if details:
        block += f"\n{' · '.join(details)}"
    prompt = getattr(agent, "prompt", None)
    if isinstance(prompt, str) and prompt:
        snippet = prompt.replace("\n", " ").strip()
        if len(snippet) > 120:
            snippet = snippet[:120] + "…"
        block += f"\n<i>{html.escape(snippet)}</i>"
    return block


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

    descriptions = [
        _format_agent_description(name, a.model or "?", a.duration, a.prompt)
        for a, name in named_agents
    ]
    text = "Select an agent to kill:\n\n" + "\n\n".join(descriptions)
    buttons = [
        [InlineKeyboardButton(name, callback_data=encode("kill", name, "go"))]
        for _, name in named_agents
    ]

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


def _handle_list_command() -> None:
    """Handle /list — show all currently running agents."""
    from sase.agent.running import list_running_agents
    from sase.integrations.agent_status_groups import (
        group_agent_statuses,
        status_bucket_header,
    )

    chat_id = credentials.get_chat_id()
    agents = list_running_agents()

    if not agents:
        telegram_client.send_message(chat_id, "No running agents.")
        return

    blocks: list[str] = [f"<b>{len(agents)} Running Agent(s)</b>"]
    for group in group_agent_statuses(agents):
        blocks.append(f"<b>{status_bucket_header(group.bucket, len(group.agents))}</b>")
        blocks.extend(_format_agent_list_block(agent) for agent in group.agents)

    telegram_client.send_message(chat_id, "\n\n".join(blocks), parse_mode="HTML")


def _handle_resume_command() -> None:
    """Handle /resume — show copy buttons to resume running or done agents."""
    from sase.ace.dismissed_agents import load_dismissed_agents
    from sase.ace.tui.models.agent_loader import load_all_agents
    from sase.agent.running import list_running_agents
    from sase.xprompt import extract_vcs_workflow_tag

    _DISMISSABLE_STATUSES = {"DONE", "FAILED", "PLAN DONE"}
    chat_id = credentials.get_chat_id()

    # --- Running agents ---
    running = list_running_agents()
    running_buttons: list[list[InlineKeyboardButton]] = []
    running_names: set[str] = set()
    for a in running:
        if not a.name:
            continue
        running_names.add(a.name)
        vcs_prefix = ""
        if a.prompt:
            vcs_tag = extract_vcs_workflow_tag(a.prompt)
            if vcs_tag:
                vcs_prefix = vcs_tag
        resume_text = f"{vcs_prefix}#resume:{a.name} %w:{a.name} "
        running_buttons.append(
            [
                InlineKeyboardButton(
                    f"🏃 {a.name}",
                    copy_text=CopyTextButton(text=resume_text),
                )
            ]
        )

    # --- Done/undismissed agents ---
    all_agents = load_all_agents()
    dismissed = load_dismissed_agents()
    done_buttons: list[list[InlineKeyboardButton]] = []
    for agent in all_agents:
        name = agent.agent_name or agent.cl_name
        if name == "unknown":
            continue
        if agent.status not in _DISMISSABLE_STATUSES:
            continue
        if agent.is_workflow_child:
            continue
        if agent.identity in dismissed:
            continue
        if name in running_names:
            continue
        vcs_prefix = ""
        raw = agent.get_raw_xprompt_content()
        if raw:
            vcs_tag = extract_vcs_workflow_tag(raw)
            if vcs_tag:
                vcs_prefix = vcs_tag
        resume_text = f"{vcs_prefix}#resume:{name} "
        done_buttons.append(
            [
                InlineKeyboardButton(
                    f"✅ {name}",
                    copy_text=CopyTextButton(text=resume_text),
                )
            ]
        )

    buttons = running_buttons + done_buttons
    if not buttons:
        telegram_client.send_message(chat_id, "No agents to resume.")
        return

    # Build description blocks
    running_descs = [
        _format_agent_description(a.name, a.model or "?", a.duration, a.prompt)
        for a in running
        if a.name and a.name in running_names
    ]
    done_descs: list[str] = []
    for agent in all_agents:
        name = agent.agent_name or agent.cl_name
        if name == "unknown":
            continue
        if agent.status not in _DISMISSABLE_STATUSES:
            continue
        if agent.is_workflow_child:
            continue
        if agent.identity in dismissed:
            continue
        if name in running_names:
            continue
        raw = agent.get_raw_xprompt_content()
        done_descs.append(
            _format_agent_description(
                name,
                agent.model or "?",
                agent.duration_display,
                raw,
                status=agent.status,
            )
        )

    parts = ["Select an agent to resume:"]
    has_both = running_descs and done_descs
    if has_both:
        parts.append("\nRunning:\n" + "\n\n".join(running_descs))
        parts.append("\nDone:\n" + "\n\n".join(done_descs))
    else:
        all_descs = running_descs or done_descs
        parts.append("\n" + "\n\n".join(all_descs))
    text = "\n".join(parts)

    telegram_client.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _handle_changes_command(args: str) -> None:
    """Handle /changes [project] — show copy buttons for ChangeSpec tags."""
    chat_id = credentials.get_chat_id()
    project_parts = args.split()
    if len(project_parts) > 1:
        telegram_client.send_message(chat_id, "Usage: /changes [project]")
        return

    project = project_parts[0] if project_parts else None
    listing = _list_changespec_xprompt_tags(project)
    entries = list(listing.entries)
    skipped = list(listing.skipped)

    if not entries:
        message = (
            "No active ChangeSpecs."
            if project is None
            else f"No active ChangeSpecs for {project}."
        )
        if skipped:
            message += f"\n{_format_changespec_skipped_note(len(skipped))}"
        telegram_client.send_message(chat_id, message)
        return

    total = len(entries)
    for start in range(0, total, _CHANGES_BUTTON_CHUNK_SIZE):
        chunk = entries[start : start + _CHANGES_BUTTON_CHUNK_SIZE]
        header = (
            f"Active ChangeSpecs for {project} ({total})"
            if project is not None
            else f"Active ChangeSpecs ({total})"
        )
        if total > _CHANGES_BUTTON_CHUNK_SIZE:
            end = start + len(chunk)
            header += f"\nShowing {start + 1}-{end} of {total}"
        if skipped and start == 0:
            header += f"\n{_format_changespec_skipped_note(len(skipped))}"

        buttons = [
            [
                InlineKeyboardButton(
                    _changes_button_label(entry, filtered=project is not None),
                    copy_text=CopyTextButton(text=entry.tag),
                )
            ]
            for entry in chunk
        ]
        telegram_client.send_message(
            chat_id,
            header,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


def _format_changespec_skipped_note(skipped_count: int) -> str:
    plural = "" if skipped_count == 1 else "s"
    return (
        f"Skipped {skipped_count} active ChangeSpec{plural} "
        "with unavailable workflow metadata."
    )


def _changes_button_label(entry: Any, *, filtered: bool) -> str:
    label = entry.name if filtered else f"{entry.project}/{entry.name}"
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


_BEAD_PICKER_LIMIT = 80
_BEAD_BUTTON_LABEL_MAX = 60


def _show_bead_selection(chat_id: str, message: Any | None = None) -> None:
    """Render an inline keyboard with one button per open bead."""
    try:
        result = _run_bead_command(["list"], message=message)
    except FileNotFoundError:
        telegram_client.send_message(chat_id, "`sase` CLI not found on bot host")
        return

    if result.returncode != 0:
        err = result.stderr.strip() or "sase bead list failed"
        escaped = err.replace("\\", "\\\\").replace("`", "\\`")
        telegram_client.send_message(
            chat_id,
            f"```\n{escaped}\n```",
            parse_mode="MarkdownV2",
        )
        return

    entries = parse_bead_list_output(result.stdout)
    if not entries:
        telegram_client.send_message(chat_id, "No open beads.")
        return

    truncated = len(entries) > _BEAD_PICKER_LIMIT
    shown = entries[:_BEAD_PICKER_LIMIT]
    buttons: list[list[InlineKeyboardButton]] = []
    for entry in shown:
        label = f"{entry.icon} {entry.bead_id}: {entry.title}"
        if len(label) > _BEAD_BUTTON_LABEL_MAX:
            label = label[: _BEAD_BUTTON_LABEL_MAX - 1] + "…"
        buttons.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=encode("bead", entry.bead_id, "show"),
                )
            ]
        )

    header = f"<b>Open beads ({len(entries)}):</b>"
    if truncated:
        text = (
            f"{header}\n"
            f"<i>(showing first {_BEAD_PICKER_LIMIT} of {len(entries)} — "
            f"refine with /bead &lt;id&gt;)</i>"
        )
    else:
        text = header

    telegram_client.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _handle_bead_callback(callback_query: Any, bead_id: str) -> None:
    """Handle a tap on an open-beads picker button."""
    telegram_client.answer_callback_query(callback_query.id, f"Loading {bead_id}…")
    _handle_bead_command(bead_id, message=getattr(callback_query, "message", None))


def _handle_bead_command(args: str, message: Any | None = None) -> None:
    """Handle /bead [<id>] — render bead details, or show open-beads picker."""
    chat_id = credentials.get_chat_id()
    parts = args.strip().split()
    if not parts:
        _show_bead_selection(chat_id, message=message)
        return
    bead_id = parts[0]

    try:
        result = _run_bead_command(["show", bead_id], message=message)
    except FileNotFoundError:
        telegram_client.send_message(chat_id, "`sase` CLI not found on bot host")
        return

    if result.returncode != 0:
        err = result.stderr.strip() or "sase bead show failed"
        # Inside ``` code blocks only `\` and `` ` `` need escaping.
        escaped = err.replace("\\", "\\\\").replace("`", "\\`")
        telegram_client.send_message(
            chat_id,
            f"```\n{escaped}\n```",
            parse_mode="MarkdownV2",
        )
        return

    md = bead_show_to_markdown(result.stdout)
    telegram_client.send_message(
        chat_id,
        markdown_to_telegram_v2(md),
        parse_mode="MarkdownV2",
    )


def _send_confirmation(response: ResponseAction, message_id: int) -> None:
    """Send a confirmation reply to the user's feedback/answer message."""
    try:
        chat_id = credentials.get_chat_id()
        telegram_client.send_message(
            chat_id,
            confirmation_text(response),
            reply_to_message_id=message_id,
        )
    except Exception:
        log.warning("Failed to send confirmation reply", exc_info=True)


def _handle_text_message(message: Any) -> None:
    """Handle a text message: feedback completion, or new agent launch."""
    text = reconstruct_code_markers(message.text, message.entities)
    reply_to = getattr(message, "reply_to_message", None)
    reply_key = (
        str(reply_to.message_id)
        if reply_to is not None and getattr(reply_to, "message_id", None) is not None
        else None
    )
    response = process_text_message(text, key=reply_key)
    if response is not None:
        _write_response(response)
        # Clear only the matched awaiting entry — leaves other concurrent
        # flows intact.
        if reply_key is not None:
            clear_awaiting_feedback(reply_key)
        else:
            clear_awaiting_feedback_by_prefix(response.notif_id_prefix)
        pending_actions.remove(response.notif_id_prefix)
        _send_confirmation(response, message.message_id)
        return

    # Dispatch slash commands (e.g. "/kill agent")
    if text.startswith("/"):
        _handle_command(text, message)
        return

    # Launch a new agent with this text as the prompt
    _record_project_context(text, message)
    _launch_agent(text)


_SLASH_COMMANDS = [
    ("kill", "Terminate a running agent"),
    ("list", "Show all running agents"),
    ("resume", "Copy resume text for an agent"),
    ("changes", "Copy ChangeSpec workflow tags"),
    ("xprompts", "Export the xprompts catalog as a PDF"),
    ("bead", "Show a bead's details as Markdown"),
    ("update", "Update SASE and restart axe"),
]


def _slash_commands_fingerprint(commands: list[tuple[str, str]] | None = None) -> str:
    """Return a stable fingerprint for the registered Telegram command list."""
    payload = json.dumps(
        commands if commands is not None else _SLASH_COMMANDS,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _commands_registration_is_current(now: float) -> bool:
    try:
        payload = json.loads(_COMMANDS_REGISTERED_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return False

    if not isinstance(payload, dict) or payload.get("version") != 1:
        return False
    if payload.get("fingerprint") != _slash_commands_fingerprint():
        return False
    try:
        last_ts = float(payload["timestamp"])
    except (KeyError, TypeError, ValueError):
        return False
    return now - last_ts < _COMMANDS_REGISTER_INTERVAL


def _register_commands_if_needed() -> None:
    """Register slash commands with Telegram, at most once per hour.

    Uses a file-based timestamp and command fingerprint to avoid calling
    ``set_my_commands`` on every tick (every 5 seconds), while still picking up
    command list changes before the normal hourly interval expires.
    """
    now = time.time()
    if _COMMANDS_REGISTERED_PATH.exists() and _commands_registration_is_current(now):
        return

    try:
        telegram_client.set_my_commands(_SLASH_COMMANDS)
        _COMMANDS_REGISTERED_PATH.parent.mkdir(parents=True, exist_ok=True)
        _COMMANDS_REGISTERED_PATH.write_text(
            json.dumps(
                {
                    "version": 1,
                    "timestamp": now,
                    "fingerprint": _slash_commands_fingerprint(),
                },
                sort_keys=True,
            )
        )
        log.info("Registered Telegram slash commands")
    except Exception:
        log.warning(
            "Failed to register slash commands (will retry later)", exc_info=True
        )


def main(argv: list[str] | None = None) -> int:
    """Inbound Telegram chop entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Suppress noisy httpx request logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _parse_args(argv)

    # Register slash commands (cached, non-blocking on failure)
    _register_commands_if_needed()

    # Clean up stale pending actions
    pending_actions.cleanup_stale()

    pending = pending_actions.list_all()
    offset = get_last_offset()
    updates = telegram_client.get_updates(offset=offset, timeout=0)

    if updates:
        log.info("Received %d update(s) (offset=%s)", len(updates), offset)

        # Save offset BEFORE processing to prevent duplicate agent launches when
        # overlapping invocations race (at-most-once delivery).
        last_update_id = max(u.update_id for u in updates)
        save_offset(last_update_id + 1)

        for update in updates:
            if update.callback_query:
                log.info("Processing callback: %s", update.callback_query.data)
                _handle_callback(update.callback_query, pending)
            elif update.message:
                msg = update.message
                if msg.photo:
                    log.info("Processing photo message")
                    _handle_photo_message(msg)
                elif (
                    msg.document
                    and msg.document.mime_type
                    and msg.document.mime_type.startswith("image/")
                ):
                    log.info("Processing document image: %s", msg.document.file_name)
                    _handle_document_image(msg)
                elif msg.text:
                    log.info("Processing text message: %s", msg.text[:100])
                    _handle_text_message(msg)
                else:
                    log.info(
                        "Skipping unsupported message type (update_id=%d)",
                        update.update_id,
                    )

        # Re-read pending actions since _handle_callback may have removed some.
        pending = pending_actions.list_all()

    # Clean up pending actions handled by the TUI (remove stale buttons).
    handled = find_externally_handled(pending)
    for prefix, message_id, chat_id in handled:
        try:
            telegram_client.edit_message_reply_markup(
                chat_id, message_id, reply_markup=None
            )
        except Exception:
            pass  # Message may have been deleted or already edited
        pending_actions.remove(prefix)
        clear_awaiting_feedback_by_prefix(prefix)

    return 0


if __name__ == "__main__":
    sys.exit(main())
