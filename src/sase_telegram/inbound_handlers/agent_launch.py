"""Launching SASE agents from Telegram."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from sase_telegram import credentials, pending_actions, telegram_client
from sase_telegram.callback_data import encode
from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from sase_telegram.formatting import (
    display_cl_name,
    display_cl_names_in_text,
    display_vcs_refs_in_text,
    escape_markdown_v2,
)
from sase_telegram.inbound import normalize_launch_xprompt_at_refs
from sase_telegram.inbound_handlers.common import _COPY_TEXT_MAX

import logging

log = logging.getLogger(__name__)


def _get_agent_retry_prompt(name: str) -> str | None:
    """Read the source prompt for retrying a named agent.

    Falls back to raw_xprompt.md when the pending action is missing (e.g. due
    to a file-level race between concurrent inbound/outbound handlers). The
    caller owns formatting the prompt for the target Telegram action.
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

    return prompt


def _build_retry_prompt_for_agent(
    agent_name: str,
    source_prompt: str | None,
) -> str | None:
    """Return Telegram copy/send text for retrying ``agent_name``."""
    source_prompt = source_prompt.strip() if source_prompt is not None else ""
    if not source_prompt:
        return None

    try:
        from sase.agent.names import allocate_retry_name
        from sase.agent.retry_prompt import rewrite_retry_prompt_name

        retry_name = allocate_retry_name(agent_name)
        return rewrite_retry_prompt_name(
            source_prompt,
            retry_name,
            directive_alias="i",
        )
    except Exception:
        log.warning(
            "Failed to build Telegram retry prompt for agent %s",
            agent_name,
            exc_info=True,
        )
        return source_prompt


def _launch_agent(prompt: str) -> None:
    """Launch one or more background sase agents from a Telegram prompt.

    Routes both single-agent and multi-model fan-out launches through the
    canonical ``launch_agents_from_cwd`` pipeline, which handles workspace
    allocation, naming, and retries through a single shared code path.
    """
    prompt = normalize_launch_xprompt_at_refs(prompt)
    log.info("Launching agent for prompt: %s", prompt[:120])
    _launch_agents_with_notifications(prompt)


def _prompt_has_pr_xprompt(prompt: str) -> bool:
    """Check if a prompt contains the #pr xprompt."""
    from sase.xprompt.workflow_validator_extract import extract_xprompt_calls

    return any(call.name == "pr" for call in extract_xprompt_calls(prompt))


def _launch_agents_with_notifications(original_prompt: str) -> None:
    """Launch one or more agents via the canonical pipeline and notify Telegram.

    Unifies the single-agent and multi-model fan-out paths:
    ``%{%m:opus | %m:sonnet}`` and friends are dispatched through
    ``launch_agents_from_cwd`` (plural) so
    workspace allocation, naming, and retries follow the same retry-aware code
    path as every other multi-model launch surface.  One Telegram notification
    is emitted per spawned ``AgentLaunchResult``.
    """
    from sase.agent.launcher import launch_agents_from_cwd
    from sase.agent.repeat_launcher import extract_repeat_and_name
    from sase.xprompt.directives import extract_prompt_directives

    try:
        from sase.xprompt import process_xprompt_references

        expanded = process_xprompt_references(original_prompt)
    except Exception:
        log.warning("Failed to expand xprompts, using raw prompt", exc_info=True)
        expanded = original_prompt

    _, directives = extract_prompt_directives(expanded)

    # Naming is owned by the core launch path. Telegram must not turn an
    # internally generated name into an explicit launch directive before the
    # child has claimed it.
    repeat_count, _, _ = extract_repeat_and_name(expanded)
    is_repeat = repeat_count is not None and repeat_count > 1

    prompt = original_prompt

    chat_id = credentials.get_chat_id()
    try:
        log.info("Calling launch_agents_from_cwd")
        results = launch_agents_from_cwd(prompt)
        log.info("Spawned %d agent(s)", len(results))
    except Exception as e:
        log.error("Failed to launch agent: %s", e, exc_info=True)
        try:
            telegram_client.send_message(
                chat_id,
                f"Failed to launch agent: {e}",
            )
        except Exception:
            log.error("Failed to send error message to Telegram", exc_info=True)
        return

    if not results:
        return

    # Recover per-slot prompts so each notification reflects the model
    # actually launched. Agent names come from the spawned artifact metadata
    # so Telegram does not race the core name allocator.
    slot_prompts = _resolve_slot_prompts(prompt, len(results))

    for result, slot_prompt in zip(results, slot_prompts, strict=True):
        result_name = getattr(result, "agent_name", None)
        if isinstance(result_name, str) and result_name:
            resolved_agent_name: str | None = result_name
        else:
            resolved_agent_name = _resolve_launch_result_agent_name(result)
            if resolved_agent_name is None:
                artifacts_dir = _launch_result_artifacts_dir(result)
                meta_path = (
                    artifacts_dir / "agent_meta.json"
                    if artifacts_dir is not None
                    else None
                )
                log.warning(
                    "Telegram launch fallback: result.agent_name unset and "
                    "agent_meta.json poll timed out "
                    "(pid=%s, timestamp=%s, path=%s)",
                    getattr(result, "pid", None),
                    getattr(result, "timestamp", None),
                    meta_path,
                )
        _send_launch_notification(
            slot_prompt=slot_prompt,
            original_prompt=original_prompt,
            result=result,
            chat_id=chat_id,
            is_repeat=is_repeat,
            repeat_count=repeat_count,
            single_directives=directives if len(results) == 1 else None,
            resolved_agent_name=resolved_agent_name,
        )


def _resolve_slot_prompts(prompt: str, expected_count: int) -> list[str]:
    """Return per-slot prompts that match the launched ``AgentLaunchResult`` order.

    Falls back to repeating *prompt* when fan-out planning yields a different
    number of slots than were actually launched. This is only used to recover
    per-slot model directives for notification labels; agent names are read
    from launch artifacts.
    """
    from sase.xprompt.directives import plan_prompt_fanout_variants

    if expected_count <= 1:
        return [prompt]

    plan = plan_prompt_fanout_variants(prompt)
    if plan is None or len(plan.slots) != expected_count:
        return [prompt] * expected_count
    return [slot.prompt for slot in plan.slots]


def _resolve_launch_result_agent_name(
    result: Any,
    *,
    timeout: float = 8.0,
    interval: float = 0.1,
) -> str | None:
    """Return the actual claimed agent name written to ``agent_meta.json``."""
    artifacts_dir = _launch_result_artifacts_dir(result)
    if artifacts_dir is None:
        return None

    meta_path = artifacts_dir / "agent_meta.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            time.sleep(interval)
            continue

        name = data.get("name") if isinstance(data, dict) else None
        if isinstance(name, str) and name:
            return name
        time.sleep(interval)

    return None


def _launch_result_artifacts_dir(result: Any) -> Path | None:
    """Best-effort artifact directory lookup for an ``AgentLaunchResult``."""
    artifacts_dir = getattr(result, "artifacts_dir", None)
    if isinstance(artifacts_dir, Path):
        return artifacts_dir.expanduser()
    if isinstance(artifacts_dir, str) and artifacts_dir:
        return Path(artifacts_dir).expanduser()

    project_name = getattr(result, "project_name", None)
    timestamp = getattr(result, "timestamp", None)
    if not isinstance(project_name, str) or not project_name:
        return None
    if not isinstance(timestamp, str) or not timestamp:
        return None

    try:
        from sase.artifacts import convert_timestamp_to_artifacts_format
        from sase.core.agent_artifact_paths import (
            resolve_agent_artifact_timestamp_path,
        )

        artifacts_timestamp = convert_timestamp_to_artifacts_format(timestamp)
        return resolve_agent_artifact_timestamp_path(
            project_name,
            "ace-run",
            artifacts_timestamp,
        )
    except Exception:
        log.warning("Failed to derive artifacts dir for launch result", exc_info=True)
        return None


def _launch_provider_model_label(directives: Any | None) -> str:
    """Return the launch display label without requiring provider autodetection."""
    from sase.llm_provider.registry import (
        format_provider_model_label,
        get_default_provider_name,
        get_provider,
        resolve_model_provider,
    )

    explicit_model = directives.model if directives is not None else None
    if explicit_model:
        try:
            provider, model = resolve_model_provider(explicit_model)
            if provider is None:
                provider = get_default_provider_name()
            return format_provider_model_label(provider, model)
        except Exception:
            log.warning(
                "Falling back to explicit launch model label %r",
                explicit_model,
                exc_info=True,
            )
            return explicit_model

    try:
        provider = get_default_provider_name()
        model = get_provider().resolve_model_name()
        return format_provider_model_label(provider, model)
    except Exception:
        log.warning(
            "Falling back to generic launch label because no LLM provider "
            "could be resolved",
            exc_info=True,
        )
        return "Agent"


def _agent_vcs_prefix(prompt: str | None, agent_name: str) -> str:
    if not prompt:
        return ""
    from sase.project_tags import effective_vcs_workflow_tag
    from sase.xprompt import replace_ref_in_vcs_tag

    vcs_tag = effective_vcs_workflow_tag(prompt)
    if not vcs_tag:
        return ""
    if _prompt_has_pr_xprompt(prompt):
        vcs_tag = replace_ref_in_vcs_tag(vcs_tag, f"@{agent_name}")
    return display_vcs_refs_in_text(vcs_tag)


def _build_agent_action_keyboard(
    agent_name: str,
    *,
    prompt_for_vcs: str | None,
    retry_source_prompt: str | None,
    include_kill: bool = True,
) -> InlineKeyboardMarkup:
    """Build the standard per-agent Fork/Wait/Kill/Retry controls."""
    vcs_prefix = _agent_vcs_prefix(prompt_for_vcs, agent_name)
    fork_text = f"{vcs_prefix}#fork:{agent_name} "
    wait_text = f"{vcs_prefix}%w:{agent_name} "

    retry_prompt = _build_retry_prompt_for_agent(agent_name, retry_source_prompt)
    if retry_prompt:
        retry_prompt = display_vcs_refs_in_text(retry_prompt)
    if retry_prompt and len(retry_prompt) <= _COPY_TEXT_MAX:
        retry_button = InlineKeyboardButton(
            "🔄 Retry",
            copy_text=CopyTextButton(text=retry_prompt),
        )
    elif retry_prompt:
        pending_actions.add(
            f"retry-{agent_name}",
            {"action": "retry", "prompt": retry_prompt},
        )
        retry_button = InlineKeyboardButton(
            "🔄 Retry",
            callback_data=encode("retry", agent_name, "go"),
        )
    else:
        retry_button = None

    rows = [
        [
            InlineKeyboardButton(
                "🍴 Fork",
                copy_text=CopyTextButton(text=fork_text),
            ),
            InlineKeyboardButton(
                "⏳ Wait",
                copy_text=CopyTextButton(text=wait_text),
            ),
        ]
    ]
    agent_buttons: list[InlineKeyboardButton] = []
    if include_kill:
        agent_buttons.append(
            InlineKeyboardButton(
                "🗡️ Kill",
                callback_data=encode("kill", agent_name, "go"),
            )
        )
    if retry_button is not None:
        agent_buttons.append(retry_button)
    if agent_buttons:
        rows.append(agent_buttons)
    return InlineKeyboardMarkup(rows)


def _send_launch_notification(
    *,
    slot_prompt: str,
    original_prompt: str,
    result: Any,
    chat_id: str,
    is_repeat: bool,
    repeat_count: int | None,
    single_directives: Any | None,
    resolved_agent_name: str | None,
) -> None:
    """Send one Telegram launch notification for a spawned agent."""
    from sase.xprompt.directives import extract_prompt_directives

    if single_directives is not None:
        directives = single_directives
        agent_name = resolved_agent_name or single_directives.name
    else:
        try:
            _, directives = extract_prompt_directives(slot_prompt)
        except Exception:
            log.warning(
                "Failed to extract per-slot directives, using fallbacks", exc_info=True
            )
            directives = None
        directive_name = directives.name if directives is not None else None
        agent_name = resolved_agent_name or directive_name

    label = _launch_provider_model_label(directives)

    display = slot_prompt[:200] + ("..." if len(slot_prompt) > 200 else "")
    display = display_cl_names_in_text(display)
    escaped_label = escape_markdown_v2(label)
    if agent_name:
        escaped_name = escape_markdown_v2(display_cl_name(agent_name))
        name_line = f"  _@{escaped_name}_"
    elif is_repeat and repeat_count is not None:
        name_line = f"  _repeat×{escape_markdown_v2(str(repeat_count))}_"
    else:
        name_line = ""
    meta = escape_markdown_v2(f"workspace #{result.workspace_num}")
    escaped_display = escape_markdown_v2(display)
    keyboard: InlineKeyboardMarkup | None = None
    if agent_name:
        keyboard = _build_agent_action_keyboard(
            agent_name,
            prompt_for_vcs=slot_prompt,
            retry_source_prompt=original_prompt,
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
