"""/bead picker, show, and subprocess helpers."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from typing import Any
from sase_telegram import credentials, telegram_client
from sase_telegram.bead_format import bead_show_to_markdown, parse_bead_list_json
from sase_telegram.callback_data import encode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from sase_telegram.formatting import (
    display_cl_names_in_text,
    display_project_name,
    markdown_to_telegram_v2,
)
from sase_telegram.inbound_handlers.project_context import (
    _KnownProjectWorkspace,
    _iter_known_project_workspaces,
    _resolve_workspace_for_project,
    _bead_project_override,
    _resolve_bead_cwd,
)

import logging

log = logging.getLogger(__name__)


_ACTIVE_BEAD_LIST_ARGS = (
    "list",
    "--status=open",
    "--status=in_progress",
    "--format=json",
)


@dataclass(frozen=True)
class _ProjectBeadEntry:
    project: str | None
    workspace: str | None
    icon: str
    bead_id: str
    title: str


def _run_bead_command(
    args: list[str] | tuple[str, ...],
    message: Any | None = None,
    cwd: str | None = None,
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


def _run_active_bead_list(
    message: Any | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    """List only open and in-progress beads without the CLI closed fallback."""
    return _run_bead_command(_ACTIVE_BEAD_LIST_ARGS, message=message, cwd=cwd)


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


def _send_project_discovery_error(chat_id: str, err: str) -> None:
    telegram_client.send_message(
        chat_id,
        f"Could not enumerate SASE projects for /bead: {err}",
    )


_BEAD_LIST_ERROR_SUMMARY_MAX = 200


def _summarize_bead_list_error(project: str, stderr: str) -> str:
    """Collapse a failing ``sase bead list`` run's stderr to one bounded line.

    The full stderr (which may be a multi-line traceback) is logged at
    warning level by the caller; only the last non-empty line — the
    ``SomeError: message`` summary for an uncaught exception — is sent to
    the chat.
    """
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    summary = lines[-1] if lines else "sase bead list failed"
    if len(summary) > _BEAD_LIST_ERROR_SUMMARY_MAX:
        summary = summary[: _BEAD_LIST_ERROR_SUMMARY_MAX - 1] + "…"
    return f"{display_project_name(project)}: {summary}"


def _project_bead_entries(
    projects: list[_KnownProjectWorkspace],
) -> tuple[list[_ProjectBeadEntry], list[str]]:
    entries: list[_ProjectBeadEntry] = []
    errors: list[str] = []
    for project in projects:
        result = _run_active_bead_list(cwd=project.workspace)
        if result.returncode != 0:
            log.warning(
                "sase bead list failed for project %r: %s",
                project.project,
                result.stderr,
            )
            errors.append(_summarize_bead_list_error(project.project, result.stderr))
            continue
        for entry in parse_bead_list_json(result.stdout):
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
        for entry in parse_bead_list_json(result.stdout)
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
            label_id = f"{display_project_name(entry.project)}/{entry.bead_id}"
        label = f"{entry.icon} {label_id}: {display_cl_names_in_text(entry.title)}"
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
            result = _run_active_bead_list(message=message)
            if result.returncode != 0:
                err = result.stderr.strip() or "sase bead list failed"
                _send_bead_subprocess_error(chat_id, err)
                return
            _render_bead_selection(chat_id, _legacy_bead_entries(result))
            return

        discovery = _iter_known_project_workspaces()
        if not discovery.ok:
            _send_project_discovery_error(chat_id, discovery.error)
            return
        if discovery.projects:
            entries, errors = _project_bead_entries(discovery.projects)
            if errors and not entries:
                lines = "\n".join(f"- {err}" for err in errors)
                telegram_client.send_message(
                    chat_id,
                    f"No active beads. {len(errors)} project(s) could not be "
                    f"listed:\n{lines}",
                )
                return
            _render_bead_selection(
                chat_id,
                entries,
                skipped_error_count=len(errors),
            )
            return

        result = _run_active_bead_list(message=message)
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
                f"Unable to resolve bead project: {display_project_name(project)}",
            )
        return bead_id, _run_bead_command(["show", bead_id], cwd=cwd)

    if _bead_project_override():
        return bead_id, _run_bead_command(["show", bead_id], message=message)

    first_result: subprocess.CompletedProcess[str] | None = None
    seen_cwds: set[str] = set()
    context_cwd = _resolve_bead_cwd(message=message)
    if context_cwd:
        result = _run_bead_command(["show", bead_id], cwd=context_cwd)
        if result.returncode == 0:
            return bead_id, result
        first_result = result
        seen_cwds = {context_cwd}

    discovery = _iter_known_project_workspaces()
    if not discovery.ok:
        if first_result is not None:
            return bead_id, first_result
        return bead_id, subprocess.CompletedProcess(
            ["sase", "bead", "show", bead_id],
            1,
            "",
            f"Could not enumerate SASE projects for /bead: {discovery.error}",
        )

    candidate_cwds: list[str] = []
    for known_project in discovery.projects:
        if known_project.workspace in seen_cwds:
            continue
        seen_cwds.add(known_project.workspace)
        candidate_cwds.append(known_project.workspace)

    if first_result is None and not candidate_cwds:
        return bead_id, _run_bead_command(["show", bead_id], message=message)

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
