"""Inbound chop entry point: poll Telegram for user actions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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

from sase_telegram.formatting import escape_markdown_v2, markdown_to_telegram_v2
from sase_telegram.inbound import (
    IMAGES_DIR,
    ResponseAction,
    build_image_prompt,
    build_photo_prompt,
    clear_awaiting_feedback,
    clear_awaiting_feedback_by_prefix,
    confirmation_text,
    find_externally_handled,
    get_last_offset,
    make_image_filename,
    normalize_launch_xprompt_at_refs,
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
_UPDATE_COMPLETION_PENDING_DIR = (
    Path.home() / ".sase" / "telegram" / "update_completions"
)
_COMMANDS_REGISTER_INTERVAL = 3600  # re-register once per hour
_COPY_TEXT_MAX = 256  # Telegram CopyTextButton character limit
_CHANGES_BUTTON_CHUNK_SIZE = 50
_BEAD_PROJECT_ENV = "SASE_TELEGRAM_BEAD_PROJECT"
_PROJECT_CONTEXT_PATH = Path.home() / ".sase" / "telegram" / "project_context.json"
_MEDIA_GROUPS_PATH = Path.home() / ".sase" / "telegram" / "media_groups.json"
_MEDIA_GROUP_QUIET_SECONDS = 2.0
_KNOWN_VCS_WORKFLOWS = ("gh", "git", "hg", "jj", "p4", "cd")
_VCS_WORKFLOW_PATTERN = "|".join(_KNOWN_VCS_WORKFLOWS)
_VCS_PROJECT_PATTERN = (
    f"(?:^|(?<=\\s)|(?<=[(\\x22']))#(?P<workflow>{_VCS_WORKFLOW_PATTERN})"
    "(?:!!|\\?\\?)?"
    "(?:(?::|_)(?P<ref>[A-Za-z0-9][A-Za-z0-9_.~/-]*)|"
    "\\((?P<paren>[A-Za-z0-9][A-Za-z0-9_.~/-]*)\\))"
    "(?=\\s|$)"
)
_VCS_PROJECT_RE = re.compile(_VCS_PROJECT_PATTERN, re.IGNORECASE)
_DIRECTIVE_PREFIX_RE = re.compile(r"^(?:%\S+\s+)+")
_LAUNCH_AGENTS_DISABLED_ENV = "SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED"


@dataclass(frozen=True)
class _KnownProjectWorkspace:
    project: str
    workspace: str


@dataclass(frozen=True)
class _ProjectBeadEntry:
    project: str | None
    workspace: str | None
    icon: str
    bead_id: str
    title: str


@dataclass(frozen=True)
class _MediaGroupMessageContext:
    chat_id: str


@dataclass(frozen=True)
class _ChatInstallUnavailableResult:
    status: str = "chat_install_unavailable"
    message: str = (
        "Update not started: installed sase package does not provide "
        "chat_install.command support."
    )


def start_chat_install_worker() -> Any:
    try:
        from sase.integrations.chat_install import (
            start_chat_install_worker as worker,
        )
    except ImportError as exc:
        missing_name = getattr(exc, "name", None)
        if missing_name in {
            "sase.integrations",
            "sase.integrations.chat_install",
        } or "start_chat_install_worker" in str(exc):
            return _ChatInstallUnavailableResult()
        raise

    return worker()


def _print_inbound_summary(
    *,
    offset: int | None,
    next_offset: int | None,
    update_count: int,
    callback_count: int,
    text_count: int,
    photo_count: int,
    document_count: int,
    unsupported_count: int,
    ready_completions_sent: int,
    pending_actions_cleaned: int,
    reason: str | None = None,
) -> None:
    parts = [
        "tg_inbound:",
        f"updates={update_count}",
        f"callbacks={callback_count}",
        f"text={text_count}",
        f"photos={photo_count}",
        f"documents={document_count}",
        f"unsupported={unsupported_count}",
        f"ready_completions_sent={ready_completions_sent}",
        f"pending_actions_cleaned={pending_actions_cleaned}",
        f"offset={offset if offset is not None else '-'}",
        f"next_offset={next_offset if next_offset is not None else '-'}",
    ]
    if reason:
        parts.append(f"reason={reason}")
    print(" ".join(parts))


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


def _telegram_agent_launches_disabled() -> bool:
    return _LAUNCH_AGENTS_DISABLED_ENV in os.environ


def _extract_project_from_prompt(prompt: str) -> str | None:
    """Extract a project name from the first VCS workflow tag in *prompt*."""
    text = normalize_launch_xprompt_at_refs(prompt).lstrip()
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


def _media_group_id(message: Any) -> str | None:
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id is None:
        return None
    media_group_id = str(media_group_id)
    return media_group_id if media_group_id else None


def _media_group_key(chat_id: str, media_group_id: str) -> str:
    return f"{chat_id}:{media_group_id}"


def _load_media_groups() -> dict[str, Any]:
    try:
        data = json.loads(_MEDIA_GROUPS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        log.warning("Failed to load Telegram media groups", exc_info=True)
        return {}

    if not isinstance(data, dict):
        return {}

    groups = data.get("groups")
    if isinstance(groups, dict):
        data = groups

    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _save_media_groups(groups: dict[str, Any]) -> None:
    try:
        if not groups:
            _MEDIA_GROUPS_PATH.unlink(missing_ok=True)
            return
        _MEDIA_GROUPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MEDIA_GROUPS_PATH.write_text(
            json.dumps(groups, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        log.warning("Failed to save Telegram media groups", exc_info=True)


def _message_id(message: Any) -> int:
    raw = getattr(message, "message_id", 0)
    if isinstance(raw, int):
        return raw
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _message_caption(message: Any) -> str | None:
    caption = getattr(message, "caption", None)
    if not caption:
        return None
    caption = reconstruct_code_markers(
        caption,
        getattr(message, "caption_entities", None),
    )
    caption = normalize_launch_xprompt_at_refs(caption)
    return caption if caption.strip() else None


def _safe_document_filename(file_name: str | None) -> str:
    candidate = Path(file_name or "image.jpg").name.strip()
    candidate = candidate.replace("\x00", "")
    candidate = re.sub(r"[^A-Za-z0-9._ -]+", "_", candidate).strip(" .")
    return candidate[:160] or "image.jpg"


def _make_document_image_filename(file_id: str, file_name: str | None) -> str:
    base = make_image_filename(file_id).removesuffix(".jpg")
    return f"{base}_{_safe_document_filename(file_name)}"


def _media_group_item_from_message(message: Any, kind: str) -> dict[str, Any] | None:
    if kind == "photo":
        photos = getattr(message, "photo", None) or []
        if not photos:
            return None
        file_id = getattr(photos[-1], "file_id", None)
        file_name = None
    elif kind == "document":
        document = getattr(message, "document", None)
        if document is None:
            return None
        file_id = getattr(document, "file_id", None)
        raw_name = getattr(document, "file_name", None)
        file_name = raw_name if isinstance(raw_name, str) else None
    else:
        return None

    if not isinstance(file_id, str) or not file_id:
        return None

    return {
        "message_id": _message_id(message),
        "kind": kind,
        "file_id": file_id,
        "file_name": file_name,
    }


def _media_group_items(group: dict[str, Any]) -> list[dict[str, Any]]:
    items = group.get("items")
    if not isinstance(items, list):
        return []

    valid_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        file_id = item.get("file_id")
        if kind not in {"photo", "document"}:
            continue
        if not isinstance(file_id, str) or not file_id:
            continue
        valid_items.append(item)

    return sorted(
        valid_items,
        key=lambda item: (
            item.get("message_id", 0) if isinstance(item.get("message_id"), int) else 0,
            str(item.get("file_id", "")),
        ),
    )


def _stage_media_group_image(message: Any, kind: str) -> bool:
    """Persist one photo/document image that belongs to a Telegram album."""
    if _telegram_agent_launches_disabled():
        log.info("Ignoring Telegram media group because agent launches are disabled")
        return False

    media_group_id = _media_group_id(message)
    chat_id = _context_chat_id(message)
    item = _media_group_item_from_message(message, kind)
    if media_group_id is None or chat_id is None or item is None:
        log.warning("Skipping malformed Telegram media group message")
        return False

    now = time.time()
    groups = _load_media_groups()
    key = _media_group_key(chat_id, media_group_id)
    group = groups.get(key)
    if not isinstance(group, dict):
        group = {
            "chat_id": chat_id,
            "media_group_id": media_group_id,
            "caption": None,
            "first_seen_at": now,
            "last_seen_at": now,
            "items": [],
        }

    group["chat_id"] = chat_id
    group["media_group_id"] = media_group_id
    group["last_seen_at"] = now
    caption = _message_caption(message)
    if caption and not group.get("caption"):
        group["caption"] = caption

    items = group.get("items")
    if not isinstance(items, list):
        items = []
    if not any(
        isinstance(existing, dict) and existing.get("file_id") == item["file_id"]
        for existing in items
    ):
        items.append(item)
    group["items"] = _media_group_items({"items": items})

    groups[key] = group
    _save_media_groups(groups)
    return True


def _download_media_group_item(item: dict[str, Any]) -> Path:
    file_id = str(item["file_id"])
    if item["kind"] == "document":
        file_name = item.get("file_name")
        filename = _make_document_image_filename(
            file_id,
            file_name if isinstance(file_name, str) else None,
        )
    else:
        filename = make_image_filename(file_id)

    dest = IMAGES_DIR / filename
    telegram_client.download_file(file_id, dest)
    return dest


def _launch_media_group(group: dict[str, Any]) -> None:
    items = _media_group_items(group)
    if not items:
        return

    raw_chat_id = group.get("chat_id")
    chat_id = str(raw_chat_id) if raw_chat_id is not None else credentials.get_chat_id()
    downloaded_paths: list[Path] = []
    try:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        for item in items:
            dest = _download_media_group_item(item)
            downloaded_paths.append(dest)
            log.info("Downloaded media group image to %s", dest)
    except Exception as e:
        log.error("Failed to download Telegram media group: %s", e, exc_info=True)
        for path in downloaded_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("Failed to clean up %s", path, exc_info=True)
        try:
            telegram_client.send_message(
                chat_id,
                f"Failed to download image album: {e}",
            )
        except Exception:
            log.error("Failed to send media group error to Telegram", exc_info=True)
        return

    caption = group.get("caption")
    prompt = build_image_prompt(
        downloaded_paths,
        caption if isinstance(caption, str) else None,
    )
    _record_project_context(prompt, _MediaGroupMessageContext(chat_id=chat_id))
    _launch_agent(prompt)


def _flush_ready_media_groups() -> int:
    """Launch staged media groups whose quiet window has elapsed."""
    if _telegram_agent_launches_disabled():
        return 0

    groups = _load_media_groups()
    if not groups:
        return 0

    now = time.time()
    ready_keys: list[str] = []
    for key, group in groups.items():
        try:
            last_seen_at = float(group.get("last_seen_at", 0))
        except (TypeError, ValueError):
            last_seen_at = 0.0
        if now - last_seen_at >= _MEDIA_GROUP_QUIET_SECONDS:
            ready_keys.append(key)

    flushed = 0
    for key in ready_keys:
        group = groups.get(key)
        if isinstance(group, dict):
            _launch_media_group(group)
            flushed += 1
        groups.pop(key, None)
        _save_media_groups(groups)

    return flushed


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


def _workspace_from_project_file(project_file: Path) -> str | None:
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


def _project_spec_path(project_dir: Path, project: str) -> Path:
    """Resolve the project spec path, preferring canonical ``.sase``.

    Falls back to legacy ``.gp`` when only that exists, and defaults to the
    canonical name otherwise. Mirrors the main-repo helper but degrades
    gracefully if the older `sase` install lacks it.
    """
    try:
        from sase.ace.changespec.project_spec_path import preferred_project_spec_path
    except ImportError:
        sase_path = project_dir / f"{project}.sase"
        if sase_path.exists():
            return sase_path
        legacy_path = project_dir / f"{project}.gp"
        if legacy_path.exists():
            return legacy_path
        return sase_path
    return Path(preferred_project_spec_path(str(project_dir), project))


def _resolve_workspace_from_project_file(project: str) -> str | None:
    project_dir = Path.home() / ".sase" / "projects" / project
    return _workspace_from_project_file(_project_spec_path(project_dir, project))


def _iter_known_project_workspaces() -> list[_KnownProjectWorkspace]:
    """Return valid workspaces from ``~/.sase/projects/*/<project>.sase``.

    Legacy ``.gp`` files are honored as a fallback for projects that have
    not yet been migrated to the canonical extension.
    """
    projects_root = Path.home() / ".sase" / "projects"
    try:
        project_dirs = sorted(item for item in projects_root.iterdir() if item.is_dir())
    except OSError:
        return []

    projects: list[_KnownProjectWorkspace] = []
    seen_workspaces: set[str] = set()
    for project_dir in project_dirs:
        project = project_dir.name
        workspace = _workspace_from_project_file(
            _project_spec_path(project_dir, project)
        )
        if not workspace or workspace in seen_workspaces:
            continue
        seen_workspaces.add(workspace)
        projects.append(_KnownProjectWorkspace(project=project, workspace=workspace))
    return projects


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


def _bead_project_override() -> str | None:
    override = os.environ.get(_BEAD_PROJECT_ENV, "").strip()
    return override or None


def _resolve_bead_cwd(message: Any | None = None) -> str | None:
    """Resolve the working directory for ``sase bead`` subprocesses."""
    override = _bead_project_override()
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
    args: list[str], message: Any | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``sase bead`` in the resolved project context when available."""
    cmd = ["sase", "bead", *args]
    cwd = cwd or _resolve_bead_cwd(message=message)
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
    """Send a confirmation message with a Plan copy button after plan actions."""
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

    labels = {
        "approve": "Plan approved",
        "commit": "Plan committed",
        "epic": "Epic created",
        "legend": "Legend created",
    }
    label = labels.get(choice, "Plan updated")
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


def _is_plain_plan_reject(response: ResponseAction) -> bool:
    return (
        response.action_type == "plan"
        and response.response_data.get("action") == "reject"
        and "feedback" not in response.response_data
    )


def _plan_reject_agent_name(action: dict[str, Any] | None) -> str | None:
    if not action:
        return None

    action_data = action.get("action_data")
    containers = [action_data, action] if isinstance(action_data, dict) else [action]
    for container in containers:
        agent_name = container.get("agent_name")
        if isinstance(agent_name, str) and agent_name.strip():
            return agent_name.strip()
    return None


def _kill_agent_after_plan_reject(agent_name: str) -> None:
    from sase.agent.running import kill_named_agent

    result = kill_named_agent(agent_name)
    if not getattr(result, "success", False):
        log.info(
            "Plan reject kill for agent %s was non-fatal: %s",
            agent_name,
            getattr(result, "message", result),
        )


def _handle_post_response_side_effects(
    response: ResponseAction, action: dict[str, Any] | None
) -> None:
    if not _is_plain_plan_reject(response):
        return

    agent_name = _plan_reject_agent_name(action)
    if agent_name is None:
        log.info(
            "Skipping plan reject kill for %s: no agent_name in pending action",
            response.notif_id_prefix,
        )
        return

    try:
        _kill_agent_after_plan_reject(agent_name)
    except Exception:
        log.warning(
            "Failed to kill agent %s after Telegram plan reject",
            agent_name,
            exc_info=True,
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

    action = pending.get(response.notif_id_prefix)
    _write_response(response)
    _handle_post_response_side_effects(response, action)
    telegram_client.answer_callback_query(callback_query.id, response.answer_text)

    if action:
        telegram_client.edit_message_reply_markup(
            action["chat_id"], action["message_id"], reply_markup=None
        )

    # Send confirmation with Plan copy button for completed plan actions.
    if response.action_type == "plan" and response.response_data.get("action") in (
        "approve",
        "commit",
        "epic",
        "legend",
    ):
        if action:
            _send_plan_confirmation(action, response.response_data["action"])

    pending_actions.remove(response.notif_id_prefix)


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
            directive_alias="n",
        )
    except Exception:
        log.warning(
            "Failed to build Telegram retry prompt for agent %s",
            agent_name,
            exc_info=True,
        )
        return source_prompt


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
            escaped_name = escape_markdown_v2(name)
            redo_source_prompt = (
                kill_info.get("prompt") if kill_info else None
            ) or prompt_fallback
            redo_prompt = _build_redo_prompt_for_killed_agent(redo_source_prompt)
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
                log.warning(
                    "Telegram launch fallback: result.agent_name unset and "
                    "agent_meta.json poll timed out (pid=%s, timestamp=%s)",
                    getattr(result, "pid", None),
                    getattr(result, "timestamp", None),
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

        artifacts_timestamp = convert_timestamp_to_artifacts_format(timestamp)
    except Exception:
        log.warning("Failed to derive artifacts dir for launch result", exc_info=True)
        return None

    return (
        Path.home()
        / ".sase"
        / "projects"
        / project_name
        / "artifacts"
        / "ace-run"
        / artifacts_timestamp
    )


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
    escaped_label = escape_markdown_v2(label)
    if agent_name:
        escaped_name = escape_markdown_v2(agent_name)
        name_line = f"  _@{escaped_name}_"
    elif is_repeat and repeat_count is not None:
        name_line = f"  _repeat×{escape_markdown_v2(str(repeat_count))}_"
    else:
        name_line = ""
    meta = escape_markdown_v2(f"workspace #{result.workspace_num}")
    escaped_display = escape_markdown_v2(display)
    keyboard: InlineKeyboardMarkup | None = None
    if agent_name:
        from sase.xprompt import extract_vcs_workflow_tag, replace_ref_in_vcs_tag

        vcs_prefix = ""
        vcs_tag = extract_vcs_workflow_tag(slot_prompt)
        if vcs_tag:
            if _prompt_has_pr_xprompt(slot_prompt):
                vcs_tag = replace_ref_in_vcs_tag(vcs_tag, f"@{agent_name}")
            vcs_prefix = f"{vcs_tag}"
        # #fork:<name> implies %w:<name>, so the fork button omits the
        # redundant explicit wait. The Wait button below stays a pure wait.
        fork_text = f"{vcs_prefix}#fork:{agent_name} "
        wait_text = f"{vcs_prefix}%w:{agent_name} "
        retry_prompt = _build_retry_prompt_for_agent(agent_name, original_prompt)
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
        agent_buttons = [
            InlineKeyboardButton(
                "🗡️ Kill",
                callback_data=encode("kill", agent_name, "go"),
            )
        ]
        if retry_button is not None:
            agent_buttons.append(retry_button)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🍴 Fork",
                        copy_text=CopyTextButton(text=fork_text),
                    ),
                    InlineKeyboardButton(
                        "⏳ Wait",
                        copy_text=CopyTextButton(text=wait_text),
                    ),
                ],
                agent_buttons,
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


def _handle_photo_message(message: Any) -> None:
    """Handle a photo message: download and launch agent."""
    if _telegram_agent_launches_disabled():
        log.info("Ignoring Telegram photo launch because agent launches are disabled")
        return

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
    caption = normalize_launch_xprompt_at_refs(caption) if caption else caption
    prompt = build_photo_prompt(dest, caption)
    _record_project_context(prompt, message)
    _launch_agent(prompt)


def _handle_document_image(message: Any) -> None:
    """Handle an image sent as a document: download and launch agent."""
    if _telegram_agent_launches_disabled():
        log.info(
            "Ignoring Telegram document image launch because agent launches are disabled"
        )
        return

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
    caption = normalize_launch_xprompt_at_refs(caption) if caption else caption
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
    elif command == "fork":
        _handle_fork_command()
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
    if result.status == "launched":
        _persist_update_completion_pending(result, chat_id)
    telegram_client.send_message(chat_id, _format_update_ack(result))


def _format_update_ack(result: Any) -> str:
    if result.status == "config_missing_command":
        return "Update not started: chat_install.command is not configured."
    if result.status == "workspace_resolution_failed":
        return "Update not started: could not resolve the primary SASE workspace."
    if result.status == "already_running":
        return "Update already running."
    if result.status == "chat_install_unavailable":
        return _ChatInstallUnavailableResult.message
    if result.status == "launched":
        return result.message
    return result.message


def _persist_update_completion_pending(result: Any, chat_id: str) -> None:
    job_id = getattr(result, "job_id", None)
    status_path = getattr(result, "status_path", None)
    if not job_id or status_path is None:
        return

    record = {
        "job_id": str(job_id),
        "chat_id": str(chat_id),
        "status_path": str(status_path),
        "log_path": str(getattr(result, "log_path", "") or ""),
        "created_at": time.time(),
    }
    pending_path = _UPDATE_COMPLETION_PENDING_DIR / f"{job_id}.json"
    try:
        _UPDATE_COMPLETION_PENDING_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(pending_path, record)
    except OSError:
        log.warning(
            "Failed to persist Telegram update completion context", exc_info=True
        )


def _send_ready_update_completions() -> int:
    sent_count = 0
    try:
        pending_paths = sorted(_UPDATE_COMPLETION_PENDING_DIR.glob("*.json"))
    except OSError:
        log.warning("Failed to scan Telegram update completion context", exc_info=True)
        return sent_count

    for pending_path in pending_paths:
        pending = _load_json_file(pending_path)
        if not isinstance(pending, dict):
            pending_path.unlink(missing_ok=True)
            continue

        status_path_raw = pending.get("status_path")
        chat_id = pending.get("chat_id")
        if not isinstance(status_path_raw, str) or not isinstance(chat_id, str):
            pending_path.unlink(missing_ok=True)
            continue

        status_path = Path(status_path_raw).expanduser()
        if not status_path.exists():
            continue

        completion = _load_json_file(status_path)
        if not isinstance(completion, dict):
            continue

        text = _format_update_completion(completion, pending)
        try:
            telegram_client.send_message(chat_id, text)
        except Exception:
            log.warning(
                "Failed to send Telegram update completion for %s",
                pending.get("job_id") or pending_path.name,
                exc_info=True,
            )
            continue
        sent_count += 1
        pending_path.unlink(missing_ok=True)
    return sent_count


def _format_update_completion(
    completion: dict[str, Any], pending: dict[str, Any]
) -> str:
    log_path = completion.get("log_path") or pending.get("log_path") or ""
    log_text = _shorten_home(str(log_path)) if log_path else "(unknown)"
    exit_code = completion.get("exit_code")

    if completion.get("status") == "success" and exit_code == 0:
        return f"Update completed successfully; log: {log_text}"

    if isinstance(exit_code, int):
        return f"Update failed with exit code {exit_code}; log: {log_text}"

    return f"Update failed; log: {log_text}"


def _load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        log.warning("Failed to load JSON file: %s", path, exc_info=True)
        return None


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _shorten_home(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path.startswith(home + os.sep) else path


def _format_agent_description(
    name: str, model: str, duration: str, prompt: str | None, status: str | None = None
) -> str:
    """Format an HTML description block for an agent.

    Used by /kill and /fork to show context above the inline buttons.
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


def _handle_fork_command() -> None:
    """Handle /fork — show copy buttons to fork currently-running agents."""
    from sase.agent.running import list_running_agents
    from sase.xprompt import extract_vcs_workflow_tag

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
            vcs_tag = extract_vcs_workflow_tag(a.prompt)
            if vcs_tag:
                vcs_prefix = vcs_tag
        # #fork:<name> implies %w:<name>; no explicit wait directive needed.
        fork_text = f"{vcs_prefix}#fork:{name} "
        buttons.append(
            [
                InlineKeyboardButton(
                    f"🍴 {name}",
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


def _project_bead_token(project: str | None, bead_id: str) -> str:
    if project:
        return f"{project}/{bead_id}"
    return bead_id


def _split_project_bead_token(token: str) -> tuple[str | None, str]:
    project, sep, bead_id = token.partition("/")
    if sep and project.strip() and bead_id.strip():
        return project.strip(), bead_id.strip()
    return None, token.strip()


def _send_bead_subprocess_error(chat_id: str, err: str) -> None:
    escaped = err.replace("\\", "\\\\").replace("`", "\\`")
    telegram_client.send_message(
        chat_id,
        f"```\n{escaped}\n```",
        parse_mode="MarkdownV2",
    )


def _project_bead_entries(
    projects: list[_KnownProjectWorkspace],
) -> tuple[list[_ProjectBeadEntry], list[str]]:
    entries: list[_ProjectBeadEntry] = []
    errors: list[str] = []
    for project in projects:
        result = _run_bead_command(
            ["list"],
            cwd=project.workspace,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or "sase bead list failed"
            errors.append(f"{project.project}: {err}")
            continue
        for entry in parse_bead_list_output(result.stdout):
            entries.append(
                _ProjectBeadEntry(
                    project=project.project,
                    workspace=project.workspace,
                    icon=entry.icon,
                    bead_id=entry.bead_id,
                    title=entry.title,
                )
            )
    return entries, errors


def _legacy_bead_entries(
    result: subprocess.CompletedProcess[str],
) -> list[_ProjectBeadEntry]:
    return [
        _ProjectBeadEntry(
            project=None,
            workspace=None,
            icon=entry.icon,
            bead_id=entry.bead_id,
            title=entry.title,
        )
        for entry in parse_bead_list_output(result.stdout)
    ]


def _render_bead_selection(
    chat_id: str,
    entries: list[_ProjectBeadEntry],
    *,
    skipped_error_count: int = 0,
) -> None:
    if not entries:
        telegram_client.send_message(chat_id, "No active beads.")
        return

    bead_id_counts: dict[str, int] = {}
    for entry in entries:
        bead_id_counts[entry.bead_id] = bead_id_counts.get(entry.bead_id, 0) + 1

    truncated = len(entries) > _BEAD_PICKER_LIMIT
    shown = entries[:_BEAD_PICKER_LIMIT]
    buttons: list[list[InlineKeyboardButton]] = []
    for entry in shown:
        label_id = entry.bead_id
        if entry.project and bead_id_counts.get(entry.bead_id, 0) > 1:
            label_id = f"{entry.project}/{entry.bead_id}"
        label = f"{entry.icon} {label_id}: {entry.title}"
        if len(label) > _BEAD_BUTTON_LABEL_MAX:
            label = label[: _BEAD_BUTTON_LABEL_MAX - 1] + "…"

        callback_token = _project_bead_token(entry.project, entry.bead_id)
        try:
            callback_data = encode("bead", callback_token, "show")
        except ValueError:
            callback_data = encode("bead", entry.bead_id, "show")

        buttons.append([InlineKeyboardButton(label, callback_data=callback_data)])

    header = f"<b>Active beads ({len(entries)}):</b>"
    notes: list[str] = []
    if truncated:
        notes.append(
            f"showing first {_BEAD_PICKER_LIMIT} of {len(entries)}; "
            "refine with /bead &lt;id&gt;"
        )
    if skipped_error_count:
        notes.append(f"skipped {skipped_error_count} project(s) with list errors")
    text = header
    if notes:
        text = f"{header}\n<i>({'; '.join(notes)})</i>"

    telegram_client.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _show_bead_selection(chat_id: str, message: Any | None = None) -> None:
    """Render an inline keyboard with one button per active bead."""
    try:
        if _bead_project_override():
            result = _run_bead_command(["list"], message=message)
            if result.returncode != 0:
                err = result.stderr.strip() or "sase bead list failed"
                _send_bead_subprocess_error(chat_id, err)
                return
            _render_bead_selection(chat_id, _legacy_bead_entries(result))
            return

        projects = _iter_known_project_workspaces()
        if projects:
            entries, errors = _project_bead_entries(projects)
            if errors and not entries:
                _send_bead_subprocess_error(chat_id, "\n".join(errors))
                return
            _render_bead_selection(
                chat_id,
                entries,
                skipped_error_count=len(errors),
            )
            return

        result = _run_bead_command(["list"], message=message)
    except FileNotFoundError:
        telegram_client.send_message(chat_id, "`sase` CLI not found on bot host")
        return

    if result.returncode != 0:
        err = result.stderr.strip() or "sase bead list failed"
        _send_bead_subprocess_error(chat_id, err)
        return

    _render_bead_selection(chat_id, _legacy_bead_entries(result))


def _bead_show_result(
    bead_token: str, message: Any | None = None
) -> tuple[str, subprocess.CompletedProcess[str]]:
    project, bead_id = _split_project_bead_token(bead_token)
    if project:
        cwd = _resolve_workspace_for_project(project, "bead callback")
        if cwd is None:
            return bead_id, subprocess.CompletedProcess(
                ["sase", "bead", "show", bead_id],
                1,
                "",
                f"Unable to resolve bead project: {project}",
            )
        return bead_id, _run_bead_command(["show", bead_id], cwd=cwd)

    if _bead_project_override():
        return bead_id, _run_bead_command(["show", bead_id], message=message)

    candidate_cwds: list[str | None] = []
    context_cwd = _resolve_bead_cwd(message=message)
    if context_cwd:
        candidate_cwds.append(context_cwd)

    seen_cwds = {cwd for cwd in candidate_cwds if cwd}
    for known_project in _iter_known_project_workspaces():
        if known_project.workspace in seen_cwds:
            continue
        seen_cwds.add(known_project.workspace)
        candidate_cwds.append(known_project.workspace)

    if not candidate_cwds:
        return bead_id, _run_bead_command(["show", bead_id], message=message)

    first_result: subprocess.CompletedProcess[str] | None = None
    for cwd in candidate_cwds:
        result = _run_bead_command(["show", bead_id], cwd=cwd)
        if first_result is None:
            first_result = result
        if result.returncode == 0:
            return bead_id, result

    assert first_result is not None
    return bead_id, first_result


def _handle_bead_callback(callback_query: Any, bead_token: str) -> None:
    """Handle a tap on an active-beads picker button."""
    _project, bead_id = _split_project_bead_token(bead_token)
    telegram_client.answer_callback_query(callback_query.id, f"Loading {bead_id}…")
    _handle_bead_command(bead_token, message=getattr(callback_query, "message", None))


def _handle_bead_command(args: str, message: Any | None = None) -> None:
    """Handle /bead [<id>] — render bead details, or show active-beads picker."""
    chat_id = credentials.get_chat_id()
    parts = args.strip().split()
    if not parts:
        _show_bead_selection(chat_id, message=message)
        return
    bead_token = parts[0]

    try:
        _bead_id, result = _bead_show_result(bead_token, message=message)
    except FileNotFoundError:
        telegram_client.send_message(chat_id, "`sase` CLI not found on bot host")
        return

    if result.returncode != 0:
        err = result.stderr.strip() or "sase bead show failed"
        _send_bead_subprocess_error(chat_id, err)
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

    if _telegram_agent_launches_disabled():
        log.info("Ignoring Telegram text launch because agent launches are disabled")
        return

    # Launch a new agent with this text as the prompt
    text = normalize_launch_xprompt_at_refs(text)
    _record_project_context(text, message)
    _launch_agent(text)


_SLASH_COMMANDS = [
    ("kill", "Terminate a running agent"),
    ("list", "Show all running agents"),
    ("fork", "Copy fork text for an agent"),
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
    stale_pending = pending_actions.cleanup_stale()
    ready_completions_sent = _send_ready_update_completions()

    pending = pending_actions.list_all()
    offset = get_last_offset()
    updates = telegram_client.get_updates(offset=offset, timeout=0)
    next_offset: int | None = None
    callback_count = 0
    text_count = 0
    photo_count = 0
    document_count = 0
    unsupported_count = 0

    if updates:
        log.info("Received %d update(s) (offset=%s)", len(updates), offset)

        # Save offset BEFORE processing to prevent duplicate agent launches when
        # overlapping invocations race (at-most-once delivery).
        last_update_id = max(u.update_id for u in updates)
        next_offset = last_update_id + 1
        save_offset(next_offset)

        for update in updates:
            if update.callback_query:
                callback_count += 1
                log.info("Processing callback (update_id=%d)", update.update_id)
                _handle_callback(update.callback_query, pending)
            elif update.message:
                msg = update.message
                if msg.photo:
                    photo_count += 1
                    if _media_group_id(msg):
                        log.info("Staging grouped photo message")
                        _stage_media_group_image(msg, "photo")
                    else:
                        log.info("Processing photo message")
                        _handle_photo_message(msg)
                elif (
                    msg.document
                    and msg.document.mime_type
                    and msg.document.mime_type.startswith("image/")
                ):
                    document_count += 1
                    if _media_group_id(msg):
                        log.info(
                            "Staging grouped document image: %s",
                            msg.document.file_name,
                        )
                        _stage_media_group_image(msg, "document")
                    else:
                        log.info(
                            "Processing document image: %s",
                            msg.document.file_name,
                        )
                        _handle_document_image(msg)
                elif msg.text:
                    text_count += 1
                    log.info("Processing text message (update_id=%d)", update.update_id)
                    _handle_text_message(msg)
                else:
                    unsupported_count += 1
                    log.info(
                        "Skipping unsupported message type (update_id=%d)",
                        update.update_id,
                    )

        # Re-read pending actions since _handle_callback may have removed some.
        pending = pending_actions.list_all()

    _flush_ready_media_groups()

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

    _print_inbound_summary(
        offset=offset,
        next_offset=next_offset,
        update_count=len(updates),
        callback_count=callback_count,
        text_count=text_count,
        photo_count=photo_count,
        document_count=document_count,
        unsupported_count=unsupported_count,
        ready_completions_sent=ready_completions_sent,
        pending_actions_cleaned=len(stale_pending) + len(handled),
        reason=None if updates else "no_updates",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
