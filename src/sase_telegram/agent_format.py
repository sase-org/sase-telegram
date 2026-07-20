"""Pure HTML formatting helpers shared by Telegram agent views."""

from __future__ import annotations

from datetime import datetime
import html
from typing import Any

from sase_telegram.formatting import (
    display_cl_name,
    display_cl_names_in_text,
    display_project_name,
)

HTML_CHUNK_LIMIT = 3900


def html_escape(text: object) -> str:
    """Escape dynamic text for Telegram's HTML parse mode."""
    return html.escape(str(text), quote=False)


def entry_name(entry: Any) -> str:
    name = getattr(entry, "name", None)
    return name if isinstance(name, str) and name else "(unnamed)"


def entry_display_name(entry: Any) -> str:
    return display_cl_name(entry_name(entry))


def entry_model_label(entry: Any) -> str:
    model = getattr(entry, "model", None)
    model_label = model if isinstance(model, str) and model else "?"
    effort = getattr(entry, "reasoning_effort", None)
    if isinstance(effort, str) and effort:
        return f"{model_label} @ {effort}"
    return model_label


def format_compact_duration(seconds: float | int) -> str:
    total = max(int(seconds), 0)
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m" if secs == 0 else f"{minutes}m{secs}s"
    return f"{secs}s"


def format_wait_token(entry: Any) -> str:
    wait = getattr(entry, "wait", None)
    wait_for = tuple(getattr(wait, "wait_for", ()) or ())
    remaining = getattr(wait, "remaining_seconds", None)
    wait_until = getattr(wait, "wait_until", None)
    wait_duration = getattr(wait, "wait_duration_seconds", None)

    if wait_for:
        deps = ", ".join(str(dep) for dep in wait_for[:2])
        if len(wait_for) > 2:
            deps += f" +{len(wait_for) - 2}"
        if isinstance(remaining, int) and remaining > 0:
            return f"⏳ on {deps} · ~{format_compact_duration(remaining)} left"
        return f"⏳ on {deps}"
    if isinstance(remaining, int) and remaining > 0:
        return f"⏳ ~{format_compact_duration(remaining)} left"
    if isinstance(wait_until, str) and wait_until:
        return f"⏳ until {format_wait_until(wait_until)}"
    if isinstance(wait_duration, (int, float)) and wait_duration > 0:
        return f"⏳ {format_compact_duration(wait_duration)}"
    return "⏳ waiting"


def format_wait_until(wait_until: str) -> str:
    try:
        parsed = datetime.fromisoformat(wait_until.replace("Z", "+00:00"))
    except ValueError:
        return wait_until
    return parsed.strftime("%H:%M")


def format_finished_time(entry: Any) -> str:
    finished_at = getattr(entry, "finished_at", None)
    if isinstance(finished_at, datetime):
        return finished_at.strftime("%H:%M")
    duration = getattr(entry, "duration", None)
    return duration if isinstance(duration, str) and duration else "done"


def format_status_token(entry: Any) -> str:
    status = str(getattr(entry, "status", "") or "")
    if status == "QUESTION":
        return "❓ needs answer"
    if status == "PLAN":
        return "📋 plan ready"
    if status == "WAITING":
        return format_wait_token(entry)
    if status == "STARTING":
        return "◐ starting"
    if status == "RETRYING":
        retry = getattr(entry, "retry", None)
        attempt = getattr(retry, "retry_attempt", None)
        return f"↻{attempt}" if isinstance(attempt, int) else "↻ retrying"
    if status.startswith("FAILED"):
        return f"✗ {format_finished_time(entry)}"
    if status in {"DONE", "PLAN DONE", "TALE DONE", "STOPPED", "FEEDBACK"}:
        return f"✓ {format_finished_time(entry)}"
    duration = getattr(entry, "duration", None)
    return f"▶ {duration if isinstance(duration, str) and duration else '?'}"


def entry_micro_badges(entry: Any) -> list[str]:
    badges: list[str] = []
    if getattr(entry, "has_file_changes", False):
        badges.append("✏️")
    auto_badge = getattr(entry, "auto_badge", None)
    if isinstance(auto_badge, str) and auto_badge:
        badges.append(auto_badge)
    if getattr(entry, "bead_id", None):
        badges.append("◆")
    tag = getattr(entry, "tag", None)
    if isinstance(tag, str) and tag:
        badges.append(tag if tag.startswith("#") else f"#{tag}")
    retry = getattr(entry, "retry", None)
    retry_attempt = getattr(retry, "retry_attempt", None)
    if isinstance(retry_attempt, int):
        badges.append(f"↻{retry_attempt}")
    children = getattr(entry, "children", None)
    child_badge = getattr(children, "badge", None)
    if isinstance(child_badge, str) and child_badge:
        badges.append(child_badge)
    return badges


def format_agent_list_block(entry: Any) -> str:
    """Format one rich agent block for an overview."""
    provider_badge = getattr(entry, "provider_badge", None) or "•"
    name = html_escape(entry_display_name(entry))
    model = html_escape(entry_model_label(entry))
    status_token = html_escape(format_status_token(entry))
    micro_badges = entry_micro_badges(entry)
    suffix = (
        f" {' '.join(html_escape(badge) for badge in micro_badges)}"
        if micro_badges
        else ""
    )

    lines = [
        f"{provider_badge} <b>{name}</b> · {model} · {status_token}{suffix}",
    ]
    context = entry_context_parts(entry)
    if context:
        lines.append(" · ".join(html_escape(part) for part in context))
    activity = getattr(entry, "activity", None)
    if isinstance(activity, str) and activity:
        lines.append(f"<i>{html_escape(activity)}</i>")
    prompt = entry_prompt_snippet(entry, limit=160)
    if prompt:
        lines.append(f"<blockquote>{html_escape(prompt)}</blockquote>")
    return "\n".join(lines)


def entry_context_parts(entry: Any) -> list[str]:
    parts: list[str] = []
    project = getattr(entry, "project", None)
    if isinstance(project, str) and project:
        parts.append(display_project_name(project))
    workspace_num = getattr(entry, "workspace_num", None)
    if isinstance(workspace_num, int):
        parts.append(f"ws#{workspace_num}")
    pid = getattr(entry, "pid", None)
    if isinstance(pid, int):
        parts.append(f"PID {pid}")
    vcs_provider = getattr(entry, "vcs_provider_display", None)
    if isinstance(vcs_provider, str) and vcs_provider:
        parts.append(vcs_provider)
    parent_agent_name = getattr(entry, "parent_agent_name", None)
    if isinstance(parent_agent_name, str) and parent_agent_name:
        parts.append(f"fork of {display_cl_name(parent_agent_name)}")
    agent_family = getattr(entry, "agent_family", None)
    agent_family_role = getattr(entry, "agent_family_role", None)
    if isinstance(agent_family, str) and agent_family:
        label = f"family {display_cl_name(agent_family)}"
        if isinstance(agent_family_role, str) and agent_family_role:
            label += f"·{agent_family_role}"
        parts.append(label)
    retry = getattr(entry, "retry", None)
    retry_attempt = getattr(retry, "retry_attempt", None)
    if isinstance(retry_attempt, int):
        parts.append(f"retry {retry_attempt}")
    return parts


def entry_prompt_snippet(entry: Any, *, limit: int) -> str | None:
    prompt = getattr(entry, "prompt", None)
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    snippet = display_cl_names_in_text(prompt.replace("\n", " ").strip())
    if len(snippet) > limit:
        return snippet[: max(limit - 1, 1)] + "…"
    return snippet


def format_header_status_counts(entries: list[Any]) -> str | None:
    if not entries:
        return None
    counts: dict[str, int] = {}
    glyphs: dict[str, str] = {}
    for entry in entries:
        bucket = str(getattr(entry, "status_bucket", "") or "")
        if not bucket:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
        glyph = getattr(entry, "status_glyph", "")
        glyphs[bucket] = glyph if isinstance(glyph, str) else ""
    ordered = [
        bucket
        for bucket in ("Stopped", "Failed", "Starting", "Running", "Waiting", "Done")
        if counts.get(bucket)
    ]
    if not ordered:
        return None
    return " · ".join(
        f"{glyphs.get(bucket) or ''} {counts[bucket]} {bucket.lower()}".strip()
        for bucket in ordered
    )


def detail_rows(entry: Any) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    _append_row(rows, "Model", entry_model_label(entry))
    project = getattr(entry, "project", None)
    if isinstance(project, str) and project:
        _append_row(rows, "Project", display_project_name(project))
    changespec = getattr(entry, "changespec_name", None)
    if isinstance(changespec, str) and changespec:
        _append_row(rows, "ChangeSpec", display_cl_name(changespec))
    workspace_num = getattr(entry, "workspace_num", None)
    if isinstance(workspace_num, int):
        _append_row(rows, "Workspace", f"#{workspace_num}")
    pid = getattr(entry, "pid", None)
    if isinstance(pid, int):
        _append_row(rows, "PID", str(pid))
    _append_optional_row(rows, "VCS", getattr(entry, "vcs_provider_display", None))
    _append_optional_row(rows, "Workflow", getattr(entry, "workflow_name", None))
    auto_badge = getattr(entry, "auto_badge", None)
    if isinstance(auto_badge, str) and auto_badge:
        action = getattr(entry, "auto_approve_plan_action", None)
        _append_row(rows, "Auto", f"{auto_badge} {action}" if action else auto_badge)
    _append_optional_row(rows, "Bead", getattr(entry, "bead_id", None))
    tag = getattr(entry, "tag", None)
    if isinstance(tag, str) and tag:
        _append_row(rows, "Tag", tag if tag.startswith("#") else f"#{tag}")
    _append_kinship_rows(rows, entry)
    started_at = getattr(entry, "started_at", None)
    if isinstance(started_at, datetime):
        _append_row(rows, "Started", started_at.strftime("%Y-%m-%d %H:%M:%S"))
    finished_at = getattr(entry, "finished_at", None)
    if isinstance(finished_at, datetime):
        _append_row(rows, "Finished", finished_at.strftime("%Y-%m-%d %H:%M:%S"))
    wait_detail = detail_wait_value(entry)
    if wait_detail:
        _append_row(rows, "Wait", wait_detail)
    retry_detail = detail_retry_value(entry)
    if retry_detail:
        _append_row(rows, "Retries", retry_detail)
    _append_optional_row(rows, "Activity", getattr(entry, "activity", None))
    outputs = getattr(entry, "output_variables", None)
    if isinstance(outputs, dict) and outputs:
        _append_row(
            rows,
            "Outputs",
            " · ".join(f"{key}={value}" for key, value in sorted(outputs.items())),
        )
    artifact_count = getattr(entry, "artifact_count", 0)
    commit_count = getattr(entry, "commit_count", 0)
    artifact_parts: list[str] = []
    if isinstance(artifact_count, int) and artifact_count:
        artifact_parts.append(f"{artifact_count} files")
    if isinstance(commit_count, int) and commit_count:
        artifact_parts.append(f"{commit_count} commits")
    if artifact_parts:
        _append_row(rows, "Artifacts", " · ".join(artifact_parts))
    _append_optional_row(rows, "ERROR", getattr(entry, "error", None))
    return rows


def _append_kinship_rows(rows: list[tuple[str, str]], entry: Any) -> None:
    clan = getattr(entry, "agent_clan", None)
    if isinstance(clan, str) and clan:
        value = f"⛺ {display_cl_name(clan)}"
        generation = getattr(entry, "agent_clan_generation", None)
        if isinstance(generation, str) and generation:
            value += f" · gen {short_generation(generation)}"
        _append_row(rows, "Clan", value)

    tribe = getattr(entry, "tribe", None)
    if isinstance(tribe, str) and tribe:
        _append_row(rows, "Tribe", f"@{tribe}")

    family = getattr(entry, "agent_family", None)
    if isinstance(family, str) and family:
        value = display_cl_name(family)
        role = getattr(entry, "agent_family_role", None)
        if isinstance(role, str) and role:
            value += f" · {role}"
        _append_row(rows, "Family", value)

    parent = getattr(entry, "parent_agent_name", None)
    if isinstance(parent, str) and parent:
        _append_row(rows, "Parent", display_cl_name(parent))

    children = getattr(entry, "children", None)
    count = getattr(children, "count", 0)
    if isinstance(count, int) and count:
        value = str(count)
        status_counts = tuple(getattr(children, "status_counts", ()) or ())
        rollup = [
            f"{amount} {str(status).lower()}"
            for status, amount in status_counts
            if isinstance(amount, int) and amount
        ]
        if rollup:
            value += " · " + ", ".join(rollup)
        _append_row(rows, "Children", value)


def short_generation(generation: str) -> str:
    """Return a compact, still-recognizable clan generation token."""
    generation = generation.strip()
    if len(generation) <= 10:
        return generation
    return generation[-8:]


def _append_row(rows: list[tuple[str, str]], key: str, value: object) -> None:
    text = str(value).strip()
    if text:
        rows.append((key, text))


def _append_optional_row(
    rows: list[tuple[str, str]], key: str, value: object | None
) -> None:
    if isinstance(value, str) and value:
        rows.append((key, value))


def format_detail_grid(rows: list[tuple[str, str]]) -> str:
    if not rows:
        return ""
    width = max(len(key) for key, _ in rows)
    return "\n".join(f"{key:<{width}}  {value}" for key, value in rows)


def detail_wait_value(entry: Any) -> str | None:
    wait = getattr(entry, "wait", None)
    if wait is None or not bool(getattr(wait, "has_wait", False)):
        return None
    return format_wait_token(entry).removeprefix("⏳ ").strip()


def detail_retry_value(entry: Any) -> str | None:
    retry = getattr(entry, "retry", None)
    if retry is None or not bool(getattr(retry, "has_retry", False)):
        return None
    parts: list[str] = []
    attempt = getattr(retry, "retry_attempt", None)
    if isinstance(attempt, int):
        parts.append(f"attempt {attempt}")
    category = getattr(retry, "retry_error_category", None)
    if isinstance(category, str) and category:
        parts.append(category)
    parent = getattr(retry, "retry_of_timestamp", None)
    if isinstance(parent, str) and parent:
        parts.append(f"of {parent}")
    child = getattr(retry, "retried_as_timestamp", None)
    if isinstance(child, str) and child:
        parts.append(f"retried as {child}")
    return " · ".join(parts) if parts else None


def format_agent_detail(entry: Any, *, prompt: str | None = None) -> str:
    """Format the shared agent detail card used by ``/list`` and ``/show``."""
    provider_badge = getattr(entry, "provider_badge", None) or "•"
    title = (
        f"{provider_badge} <b>{html_escape(entry_display_name(entry))}</b> — details"
    )
    status = html_escape(format_status_token(entry))
    grid = format_detail_grid(detail_rows(entry))
    if prompt is None:
        raw_prompt = getattr(entry, "prompt", None)
        if isinstance(raw_prompt, str) and raw_prompt.strip():
            prompt = display_cl_names_in_text(raw_prompt.strip())

    parts = [title, status]
    if grid:
        parts.append(f"<pre>{html_escape(grid)}</pre>")
    if prompt:
        parts.append(f"<blockquote expandable>{html_escape(prompt)}</blockquote>")
    return "\n\n".join(parts)


def pack_html_blocks(
    blocks: list[str], *, chunk_limit: int = HTML_CHUNK_LIMIT
) -> list[str]:
    chunks: list[str] = []
    current = ""
    for block in blocks:
        separator = "\n\n" if current else ""
        candidate = f"{current}{separator}{block}" if current else block
        if current and len(candidate) > chunk_limit:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# Compatibility aliases keep the inbound module's established private helper names
# available to existing tests and callers while implementation lives here.
_html = html_escape
_entry_name = entry_name
_entry_display_name = entry_display_name
_entry_model_label = entry_model_label
_format_compact_duration = format_compact_duration
_format_wait_token = format_wait_token
_format_wait_until = format_wait_until
_format_finished_time = format_finished_time
_format_status_token = format_status_token
_entry_micro_badges = entry_micro_badges
_format_agent_list_block = format_agent_list_block
_entry_context_parts = entry_context_parts
_entry_prompt_snippet = entry_prompt_snippet
_format_header_status_counts = format_header_status_counts
_detail_rows = detail_rows
_format_detail_grid = format_detail_grid
_detail_wait_value = detail_wait_value
_detail_retry_value = detail_retry_value
_pack_html_blocks = pack_html_blocks
