"""Project tag parsing and workspace resolution for Telegram inbound."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from sase_telegram import pending_actions
from sase_telegram.inbound import normalize_launch_xprompt_at_refs
from sase_telegram.inbound_handlers.common import _context_chat_id

import logging

log = logging.getLogger(__name__)


_BEAD_PROJECT_ENV = "SASE_TELEGRAM_BEAD_PROJECT"


_PROJECT_CONTEXT_PATH = Path.home() / ".sase" / "telegram" / "project_context.json"


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


@dataclass(frozen=True)
class _KnownProjectWorkspace:
    project: str
    workspace: str


@dataclass(frozen=True)
class _ProjectDiscoveryResult:
    ok: bool
    projects: list[_KnownProjectWorkspace]
    error: str = ""


def _project_from_vcs_match(match: re.Match[str] | None) -> str | None:
    """Return the project ref carried by a VCS tag regex match."""
    if not match:
        return None

    project = match.group("ref") or match.group("paren")
    if not project or project.startswith("@"):
        return None
    return project


def _extract_project_from_prompt(prompt: str) -> str | None:
    """Extract a project name from the first VCS workflow tag in *prompt*."""
    text = normalize_launch_xprompt_at_refs(prompt).lstrip()
    directive_match = _DIRECTIVE_PREFIX_RE.match(text)
    if directive_match:
        text = text[directive_match.end() :]

    # Tag-aware read: a leading ``+<project>`` tag expands to its canonical
    # VCS ref first, so mobile prompts like ``+Sase …`` resolve. Falls back
    # to the legacy ``#`` scan for non-leading tags and ``#``-only spellings.
    try:
        from sase.project_tags import effective_vcs_workflow_tag

        vcs_tag = effective_vcs_workflow_tag(text)
    except Exception:
        vcs_tag = None
    if vcs_tag:
        project = _project_from_vcs_match(_VCS_PROJECT_RE.search(vcs_tag))
        if project:
            return project

    return _project_from_vcs_match(_VCS_PROJECT_RE.search(text))


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
    canonical name otherwise.
    """
    from sase.ace.patch.project_spec_path import preferred_project_spec_path

    return Path(preferred_project_spec_path(str(project_dir), project))


def _resolve_workspace_from_project_file(project: str) -> str | None:
    project_dir = Path.home() / ".sase" / "projects" / project
    return _workspace_from_project_file(_project_spec_path(project_dir, project))


def _iter_known_project_workspaces() -> _ProjectDiscoveryResult:
    """Return workspaces for enabled projects, via ``sase project list``.

    Enumerating enabled projects through the CLI (rather than globbing
    ``~/.sase/projects/*``) excludes sibling/linked-repo and stale legacy
    project directories that are not bead-bearing projects.
    """
    try:
        result = subprocess.run(
            ["sase", "project", "list", "--state=enabled", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        log.warning("Failed to run 'sase project list' to enumerate projects")
        return _ProjectDiscoveryResult(
            ok=False,
            projects=[],
            error="'sase project list' could not be started",
        )
    if result.returncode != 0:
        err = result.stderr.strip() or "no stderr"
        log.warning(
            "'sase project list' exited %d: %s",
            result.returncode,
            err,
        )
        return _ProjectDiscoveryResult(
            ok=False,
            projects=[],
            error=f"'sase project list' exited {result.returncode}: {err}",
        )

    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("'sase project list' emitted unparseable JSON")
        return _ProjectDiscoveryResult(
            ok=False,
            projects=[],
            error="'sase project list' emitted unparseable JSON",
        )
    if not isinstance(records, list):
        log.warning("'sase project list' emitted a non-list JSON payload")
        return _ProjectDiscoveryResult(
            ok=False,
            projects=[],
            error="'sase project list' emitted a non-list JSON payload",
        )

    projects: list[_KnownProjectWorkspace] = []
    seen_workspaces: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        project = record.get("project_name")
        workspace = record.get("workspace_dir")
        if not isinstance(project, str) or not project:
            continue
        if not isinstance(workspace, str) or not workspace:
            continue
        if workspace in seen_workspaces:
            continue
        seen_workspaces.add(workspace)
        projects.append(_KnownProjectWorkspace(project=project, workspace=workspace))
    return _ProjectDiscoveryResult(ok=True, projects=projects)


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
