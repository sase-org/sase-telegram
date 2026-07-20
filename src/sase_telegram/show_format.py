"""Pure Telegram HTML renderers for ``/show`` entity views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sase_telegram.agent_format import (
    detail_rows,
    entry_display_name,
    entry_model_label,
    entry_prompt_snippet,
    format_agent_detail,
    format_agent_list_block,
    format_detail_grid,
    format_header_status_counts,
    format_status_token,
    html_escape,
    short_generation,
)
from sase_telegram.formatting import display_cl_name
from sase_telegram.show_entities import (
    KinshipIndex,
    KinshipIndexItem,
    ShowNotFound,
    ShowTarget,
)

_DRILLDOWN_LIMIT = 12
_INDEX_BUTTON_LIMIT = 24


@dataclass(frozen=True)
class ShowButtonSpec:
    """A Telegram-independent button action produced by a show renderer."""

    label: str
    action: Literal["open", "refresh", "copy"]
    ref: str


@dataclass(frozen=True)
class ShowView:
    """Rendered HTML blocks plus rows of keyboard specifications."""

    blocks: tuple[str, ...]
    button_rows: tuple[tuple[ShowButtonSpec, ...], ...] = ()


def format_show_target(target: ShowTarget, *, prompt: str | None = None) -> ShowView:
    if target.kind == "agent":
        return format_agent_show(target, prompt=prompt)
    if target.kind == "clan":
        return format_clan_show(target)
    if target.kind == "family":
        return format_family_show(target)
    return format_tribe_show(target)


def format_agent_show(target: ShowTarget, *, prompt: str | None = None) -> ShowView:
    entry = target.entry
    if entry is not None:
        blocks = [format_agent_detail(entry, prompt=prompt)]
    else:
        named = target.named_agent
        done = bool(getattr(named, "is_done", False))
        outcome = getattr(named, "outcome", None)
        status = f"✓ {outcome or 'done'}" if done else "▶ running"
        detail_grid_rows: list[tuple[str, str]] = []
        artifacts_dir = getattr(named, "artifacts_dir", None)
        if isinstance(artifacts_dir, str) and artifacts_dir:
            detail_grid_rows.append(("Artifacts", artifacts_dir))
        grid = format_detail_grid(detail_grid_rows)
        title = f"• <b>{html_escape(display_cl_name(target.name))}</b> — details"
        blocks = [f"{title}\n\n{html_escape(status)}"]
        if grid:
            blocks[0] += f"\n\n<pre>{html_escape(grid)}</pre>"

    jump_buttons: list[ShowButtonSpec] = []
    if entry is not None:
        clan = getattr(entry, "agent_clan", None)
        if isinstance(clan, str) and clan:
            jump_buttons.append(ShowButtonSpec("⛺ Clan", "open", clan))
        family = getattr(entry, "agent_family", None)
        if isinstance(family, str) and family:
            jump_buttons.append(ShowButtonSpec("🧬 Family", "open", family))
    _append_also_tribe_hint(blocks, target)
    button_rows = (tuple(jump_buttons),) if jump_buttons else ()
    return ShowView(tuple(blocks), button_rows)


def format_clan_show(target: ShowTarget) -> ShowView:
    clan = target.clan
    members = tuple(getattr(clan, "members", ()) or ())
    done_count = sum(getattr(member, "outcome", None) is not None for member in members)
    complete = bool(getattr(clan, "is_complete", False))
    progress = "✓ complete" if complete else f"{done_count}/{len(members)} done"
    header_parts = [
        f"⛺ <b>{html_escape(display_cl_name(target.name))}</b> — clan",
    ]
    if target.clan_tribe:
        header_parts.append(f"@{html_escape(target.clan_tribe)}")
    generation = getattr(clan, "generation", None)
    if isinstance(generation, str) and generation:
        header_parts.append(f"gen {html_escape(short_generation(generation))}")
    header_parts.append(progress)
    blocks = [" · ".join(header_parts)]
    if target.clan_summary:
        blocks.append(f"<i>{html_escape(target.clan_summary)}</i>")

    active_entries = [
        entry
        for entry in target.entries
        if not bool(getattr(entry, "is_terminal", False))
    ]
    status_rollup = format_header_status_counts(active_entries)
    if status_rollup:
        blocks.append(html_escape(status_rollup))

    entries_by_name = _entries_by_name(target.entries)
    for member in members:
        name = getattr(member, "name", None)
        entry = entries_by_name.get(name) if isinstance(name, str) else None
        if entry is not None:
            blocks.append(format_agent_list_block(entry))
            continue
        if not isinstance(name, str) or not name:
            continue
        outcome = getattr(member, "outcome", None)
        glyph = (
            "✓"
            if outcome in {"completed", "done", "success"}
            else "✗"
            if outcome
            else "○"
        )
        status = (
            "done"
            if outcome in {"completed", "done", "success"}
            else outcome or "unknown"
        )
        blocks.append(
            f"{glyph} <b>{html_escape(display_cl_name(name))}</b> · "
            f"{html_escape(status)}"
        )

    ref = target.name
    rows: list[tuple[ShowButtonSpec, ...]] = [
        (ShowButtonSpec("🔄 Refresh", "refresh", ref),)
    ]
    if complete:
        rows[0] += (ShowButtonSpec("🍴 Fork clan", "copy", f"#fork:{ref} "),)
    member_buttons = [
        ShowButtonSpec(
            display_cl_name(str(member.name)),
            "open",
            str(member.name),
        )
        for member in members
        if isinstance(getattr(member, "name", None), str)
        and getattr(member, "name", None)
    ]
    rows.extend(_button_rows(member_buttons[:_DRILLDOWN_LIMIT]))
    if len(member_buttons) > _DRILLDOWN_LIMIT:
        blocks.append(
            f"…and {len(member_buttons) - _DRILLDOWN_LIMIT} more members "
            "shown above (buttons capped)"
        )
    _append_also_tribe_hint(blocks, target)
    return ShowView(tuple(blocks), tuple(rows))


def format_family_show(target: ShowTarget) -> ShowView:
    family = target.family
    members = tuple(getattr(family, "members", ()) or ())
    done_count = sum(getattr(member, "outcome", None) is not None for member in members)
    successful = (
        all(
            getattr(member, "outcome", None) in {"completed", "done", "success"}
            for member in members
        )
        if members
        else False
    )
    progress = "✓ complete" if successful else f"{done_count}/{len(members)} done"
    blocks = [
        f"🧬 <b>{html_escape(display_cl_name(target.name))}</b> — family · "
        f"{len(members)} members · {progress}"
    ]
    entries_by_name = _entries_by_name(target.entries)
    active_member = next(
        (
            member
            for member in reversed(members)
            if (entry := entries_by_name.get(str(getattr(member, "name", ""))))
            is not None
            and not bool(getattr(entry, "is_terminal", False))
        ),
        None,
    )
    lines: list[str] = []
    for index, member in enumerate(members, start=1):
        name = str(getattr(member, "name", "(unnamed)"))
        entry = entries_by_name.get(name)
        outcome = getattr(member, "outcome", None)
        if member is active_member:
            glyph = "▶"
            status = _active_family_status(entry)
        elif outcome in {"completed", "done", "success"}:
            glyph = "✓"
            status = "done"
        elif outcome:
            glyph = "✗"
            status = str(outcome)
        elif entry is not None:
            glyph = "○"
            status = _status_without_glyph(format_status_token(entry))
        else:
            glyph = "○"
            status = "waiting"
        parts = [
            f"{index}. {glyph} <b>{html_escape(display_cl_name(name))}</b>",
        ]
        role = getattr(entry, "agent_family_role", None) if entry is not None else None
        if isinstance(role, str) and role:
            parts.append(html_escape(role))
        parts.append(html_escape(status))
        duration = getattr(entry, "duration", None) if entry is not None else None
        if isinstance(duration, str) and duration:
            parts.append(html_escape(duration))
        lines.append(" · ".join(parts))
    if lines:
        blocks.extend(lines)

    if active_member is not None:
        entry = entries_by_name.get(str(getattr(active_member, "name", "")))
        activity = getattr(entry, "activity", None)
        if isinstance(activity, str) and activity:
            blocks.append(f"<i>{html_escape(activity)}</i>")
        prompt = entry_prompt_snippet(entry, limit=240)
        if prompt:
            blocks.append(f"<blockquote>{html_escape(prompt)}</blockquote>")

    ref = target.name
    rows: list[tuple[ShowButtonSpec, ...]] = [
        (
            ShowButtonSpec("🔄 Refresh", "refresh", ref),
            ShowButtonSpec("🍴 Fork", "copy", f"#fork:{ref} "),
        )
    ]
    member_buttons = [
        ShowButtonSpec(
            display_cl_name(str(member.name)),
            "open",
            str(member.name),
        )
        for member in members
        if isinstance(getattr(member, "name", None), str)
        and getattr(member, "name", None)
    ]
    rows.extend(_button_rows(member_buttons[:_DRILLDOWN_LIMIT]))
    if len(member_buttons) > _DRILLDOWN_LIMIT:
        blocks.append(
            f"…and {len(member_buttons) - _DRILLDOWN_LIMIT} more members "
            "shown above (buttons capped)"
        )
    _append_also_tribe_hint(blocks, target)
    return ShowView(tuple(blocks), tuple(rows))


def format_tribe_show(target: ShowTarget) -> ShowView:
    entries = tuple(target.entries)
    clan_groups: dict[str, list[Any]] = {}
    family_groups: dict[str, list[Any]] = {}
    standalone: list[Any] = []
    for entry in entries:
        clan = getattr(entry, "agent_clan", None)
        family = getattr(entry, "agent_family", None)
        if isinstance(clan, str) and clan:
            clan_groups.setdefault(clan, []).append(entry)
        elif isinstance(family, str) and family:
            family_groups.setdefault(family, []).append(entry)
        else:
            standalone.append(entry)
    entity_count = len(clan_groups) + len(family_groups) + len(standalone)
    blocks = [
        f"🏷️ <b>@{html_escape(target.name)}</b> — tribe · "
        f"{entity_count} {_plural('entity', entity_count)} · "
        f"{len(entries)} {_plural('agent', len(entries))}"
    ]
    rollup = format_header_status_counts(list(entries))
    if rollup:
        blocks.append(html_escape(rollup))

    if clan_groups:
        blocks.append("<b>Clans</b>")
        for name, members in sorted(
            clan_groups.items(), key=lambda item: item[0].casefold()
        ):
            unique = tuple(_entries_by_name(members).values())
            done = sum(_entry_is_done(entry) for entry in unique)
            blocks.append(
                f"⛺ <b>{html_escape(display_cl_name(name))}</b> · "
                f"{done}/{len(unique)} done"
            )
    if family_groups:
        blocks.append("<b>Families</b>")
        for name, members in sorted(
            family_groups.items(), key=lambda item: item[0].casefold()
        ):
            unique = tuple(_entries_by_name(members).values())
            done = sum(_entry_is_done(entry) for entry in unique)
            phase = len(unique) if done >= len(unique) else done + 1
            blocks.append(
                f"🧬 <b>{html_escape(display_cl_name(name))}</b> · "
                f"phase {phase}/{len(unique)}"
            )
    if standalone:
        blocks.append("<b>Agents</b>")
        for entry in standalone:
            badge = getattr(entry, "provider_badge", None) or "•"
            blocks.append(
                f"{badge} <b>{html_escape(entry_display_name(entry))}</b> · "
                f"{html_escape(entry_model_label(entry))} · "
                f"{html_escape(format_status_token(entry))}"
            )

    ref = f"@{target.name}"
    rows: list[tuple[ShowButtonSpec, ...]] = [
        (ShowButtonSpec("🔄 Refresh", "refresh", ref),)
    ]
    buttons = [
        ShowButtonSpec(f"⛺ {display_cl_name(name)}", "open", name)
        for name in sorted(clan_groups, key=str.casefold)
    ]
    buttons.extend(
        ShowButtonSpec(f"🧬 {display_cl_name(name)}", "open", name)
        for name in sorted(family_groups, key=str.casefold)
    )
    buttons.extend(
        ShowButtonSpec(entry_display_name(entry), "open", str(entry.name))
        for entry in standalone
        if isinstance(getattr(entry, "name", None), str)
    )
    rows.extend(_button_rows(buttons[:_DRILLDOWN_LIMIT]))
    if len(buttons) > _DRILLDOWN_LIMIT:
        blocks.append(
            f"…and {len(buttons) - _DRILLDOWN_LIMIT} more entities "
            "shown above (buttons capped)"
        )
    return ShowView(tuple(blocks), tuple(rows))


def format_show_index(index: KinshipIndex) -> ShowView:
    blocks = ["🧭 <b>Agents by kinship</b>"]
    all_items: list[KinshipIndexItem] = []
    for title, glyph, items in (
        ("Clans", "⛺", index.clans),
        ("Families", "🧬", index.families),
        ("Tribes", "🏷️", index.tribes),
    ):
        if not items:
            continue
        blocks.append(f"<b>{title}</b>")
        for item in items:
            display_name = (
                f"@{item.name}" if item.kind == "tribe" else display_cl_name(item.name)
            )
            blocks.append(
                f"{glyph} <b>{html_escape(display_name)}</b> · "
                f"{item.member_count} {_plural('member', item.member_count)} · "
                f"{_index_progress(item)}"
            )
        all_items.extend(items)

    if not all_items:
        blocks.append("No grouped agents yet. Use /list to see agents.")
    blocks.append(
        "Use <code>/show &lt;name&gt;</code> or <code>/show @&lt;tribe&gt;</code>."
    )

    visible = all_items[:_INDEX_BUTTON_LIMIT]
    rows = _button_rows(
        [
            ShowButtonSpec(_index_button_label(item), "open", item.ref)
            for item in visible
        ]
    )
    if len(all_items) > _INDEX_BUTTON_LIMIT:
        blocks.append(
            f"…and {len(all_items) - _INDEX_BUTTON_LIMIT} more groups "
            "shown above (buttons capped)"
        )
    return ShowView(tuple(blocks), tuple(rows))


def format_show_not_found(not_found: ShowNotFound) -> ShowView:
    query = not_found.query or "(empty)"
    blocks = [
        f"No agent, clan, family, or tribe named <code>{html_escape(query)}</code>."
    ]
    if not_found.suggestions:
        blocks.append("Did you mean one of these?")
    rows = _button_rows(
        [
            ShowButtonSpec(_suggestion_label(item.kind, item.name), "open", item.ref)
            for item in not_found.suggestions
        ]
    )
    return ShowView(tuple(blocks), tuple(rows))


def _append_also_tribe_hint(blocks: list[str], target: ShowTarget) -> None:
    if target.also_tribe:
        blocks.append(f"Also a tribe — <code>/show @{html_escape(target.name)}</code>")


def _entries_by_name(entries: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for entry in entries:
        name = getattr(entry, "name", None)
        if isinstance(name, str) and name:
            result[name] = entry
    return result


def _entry_is_done(entry: Any) -> bool:
    return bool(getattr(entry, "is_terminal", False)) or getattr(
        entry, "status_bucket", None
    ) in {"Done", "Failed"}


def _button_rows(
    buttons: list[ShowButtonSpec], *, width: int = 2
) -> list[tuple[ShowButtonSpec, ...]]:
    return [
        tuple(buttons[index : index + width]) for index in range(0, len(buttons), width)
    ]


def _status_without_glyph(status: str) -> str:
    parts = status.split(None, 1)
    return (
        parts[1]
        if len(parts) == 2
        and parts[0]
        in {
            "▶",
            "✓",
            "✗",
            "◐",
            "⏳",
        }
        else status
    )


def _active_family_status(entry: Any) -> str:
    status = str(getattr(entry, "status", "") or "").strip()
    if status == "RUNNING":
        return "running"
    return _status_without_glyph(format_status_token(entry))


def _index_progress(item: KinshipIndexItem) -> str:
    if item.member_count and item.done_count >= item.member_count:
        return "✓ complete"
    return f"{item.done_count}/{item.member_count} done"


def _index_button_label(item: KinshipIndexItem) -> str:
    glyph = {"clan": "⛺", "family": "🧬", "tribe": "🏷️"}[item.kind]
    name = f"@{item.name}" if item.kind == "tribe" else display_cl_name(item.name)
    return f"{glyph} {name}"


def _suggestion_label(kind: str, name: str) -> str:
    glyph = {"agent": "🤖", "clan": "⛺", "family": "🧬", "tribe": "🏷️"}[kind]
    visible = f"@{name}" if kind == "tribe" else display_cl_name(name)
    return f"{glyph} {visible}"


def _plural(word: str, count: int) -> str:
    if count == 1:
        return word
    if word.endswith("y"):
        return f"{word[:-1]}ies"
    return f"{word}s"
