"""Static Decisions question sheet for Telegram plan reviews (sase-1hi.10.6)."""

from __future__ import annotations

from typing import Any

from sase_telegram.formatting import escape_markdown_v2
from sase_telegram.plan_decisions import (
    DECISION_SHEET_BUDGET,
    current_values,
    effective_values,
    sheet_for,
)

_BOOKKEEPING_KEYS = {"decisions", "decided_by", "decided_via"}


def strip_decision_bookkeeping(
    frontmatter: dict[Any, Any],
) -> dict[Any, Any]:
    """Return frontmatter without decision bookkeeping fields."""
    return {
        key: value for key, value in frontmatter.items() if key not in _BOOKKEEPING_KEYS
    }


def _memory_provenance_text(provenance: str) -> str:
    mapping = {
        "asked": "you asked",
        "not_asked": "not asked",
        "quote_not_found": "⚠ quote not found · off",
        "inherited": "approved in epic",
    }
    return mapping.get(str(provenance), str(provenance))


def _memory_note_text(resolved: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for record in resolved:
        selector = str(record.get("selector", ""))
        kind = str(record.get("type", "reference"))
        scope = str(record.get("scope", "project"))
        label = selector or scope
        exists = record.get("exists", True)
        new_chip = " · new" if exists is False else ""
        if kind == "core":
            parts.append(f"{label} (core · loaded every turn{new_chip})")
        else:
            parts.append(f"{label} ({kind}{new_chip})")
    return ", ".join(parts)


def _render_sheet_text(
    definitions: list[dict[str, Any]],
    values: dict[str, Any],
    *,
    drop_non_default_labels: bool = False,
    drop_all_labels: bool = False,
) -> str:
    """Render unescaped sheet text as whole fields/lines.

    Protected: every ask, its complete starred default line, why, memory
    selector/type/new/provenance chip, and requested quote always survive
    through every stage. Only choice labels degrade, by whole lines.
    Choice keys always survive in full (the radio keyboard and attached
    plan carry them too).
    """
    total = len(definitions)
    memos = sum(1 for d in definitions if d.get("memory") is not None)
    lines = [f"Decisions · {total}" + (f" · 🧠 {memos}" if memos else "")]
    for number, definition in enumerate(definitions, start=1):
        decision_id = str(definition.get("id", ""))
        ask = str(definition.get("ask", ""))
        kind = str(definition.get("kind", ""))
        default = definition.get("default")
        memory = definition.get("memory")
        prefix = "🧠 " if memory is not None else ""
        # Protected ask line.
        lines.append(f"{number}. {prefix}{decision_id} — {ask}")
        if kind == "choice":
            choices = definition.get("choices", []) or []
            default_key = str(default)
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                key = str(choice.get("key", ""))
                label = str(choice.get("label", ""))
                star = "★ " if key == default_key else ""
                if drop_all_labels:
                    lines.append(f"   {star}{key}")
                elif drop_non_default_labels and key != default_key:
                    lines.append(f"   {star}{key}")
                elif label:
                    lines.append(f"   {star}{key} · {label}")
                else:
                    lines.append(f"   {star}{key}")
            why = definition.get("why")
            if isinstance(why, str) and why.strip():
                lines.append(f"   ★ {why.strip()}")
        else:
            default_word = "yes" if bool(default) else "no"
            lines.append(f"   ★ {default_word}")
            why = definition.get("why")
            if isinstance(why, str) and why.strip():
                lines.append(f"   ★ {why.strip()}")
        if memory is not None:
            mem = memory if isinstance(memory, dict) else {}
            selectors = mem.get("selectors", [])
            selector_text = ", ".join(str(s) for s in selectors) if selectors else ""
            resolved = mem.get("resolved", []) if isinstance(mem, dict) else []
            note_text = _memory_note_text(resolved) if resolved else selector_text
            provenance = str(mem.get("provenance", "not_asked"))
            chip = _memory_provenance_text(provenance)
            quote = mem.get("quote", "")
            if note_text:
                # Avoid duplicated provenance wording when the note text
                # already names the provenance.
                if chip.lower() in note_text.lower():
                    lines.append(f"   🧠 {note_text}")
                else:
                    lines.append(f"   🧠 {note_text} · {chip}")
            else:
                lines.append(f"   🧠 {chip}")
            if isinstance(quote, str) and quote.strip():
                lines.append(f'   "{quote.strip()}"')
    return "\n".join(lines)


def _split_expandable_parts(
    definitions: list[dict[str, Any]],
    values: dict[str, Any],
) -> tuple[str, str]:
    """Split whole-line outside text from quoted choice lines (unescaped).

    Asks, starred defaults, why, memory, and quotes stay outside the
    quotes; only choice-key lines are quoted. Never slices a field.
    """
    total = len(definitions)
    memos = sum(1 for d in definitions if d.get("memory") is not None)
    outside = [f"Decisions · {total}" + (f" · 🧠 {memos}" if memos else "")]
    choice_block: list[str] = []
    for number, definition in enumerate(definitions, start=1):
        decision_id = str(definition.get("id", ""))
        ask = str(definition.get("ask", ""))
        kind = str(definition.get("kind", ""))
        default = definition.get("default")
        memory = definition.get("memory")
        prefix = "🧠 " if memory is not None else ""
        outside.append(f"{number}. {prefix}{decision_id} — {ask}")
        if kind == "choice":
            default_key = str(default)
            for choice in definition.get("choices", []) or []:
                if not isinstance(choice, dict):
                    continue
                key = str(choice.get("key", ""))
                star = "★ " if key == default_key else ""
                choice_block.append(f"   {star}{key}")
            why = definition.get("why")
            if isinstance(why, str) and why.strip():
                outside.append(f"   ★ {why.strip()}")
        else:
            default_word = "yes" if bool(default) else "no"
            outside.append(f"   ★ {default_word}")
            why = definition.get("why")
            if isinstance(why, str) and why.strip():
                outside.append(f"   ★ {why.strip()}")
        if memory is not None:
            mem = memory if isinstance(memory, dict) else {}
            selectors = mem.get("selectors", [])
            selector_text = ", ".join(str(s) for s in selectors) if selectors else ""
            resolved = mem.get("resolved", []) if isinstance(mem, dict) else []
            note_text = _memory_note_text(resolved) if resolved else selector_text
            provenance = str(mem.get("provenance", "not_asked"))
            chip = _memory_provenance_text(provenance)
            quote = mem.get("quote", "")
            if note_text:
                if chip.lower() in note_text.lower():
                    outside.append(f"   🧠 {note_text}")
                else:
                    outside.append(f"   🧠 {note_text} · {chip}")
            else:
                outside.append(f"   🧠 {chip}")
            if isinstance(quote, str) and quote.strip():
                outside.append(f'   "{quote.strip()}"')
    return "\n".join(outside), "\n".join(choice_block)


def _render_sheet_expandable(
    definitions: list[dict[str, Any]],
    values: dict[str, Any],
) -> str:
    """Render asks outside plus quoted choice lines, escaped separately."""
    from sase_telegram.formatting import wrap_expandable_blockquote as _wrap

    outside, choices = _split_expandable_parts(definitions, values)
    escaped_outside = escape_markdown_v2(outside)
    if not choices.strip():
        return escaped_outside
    # Wrap only the escaped choice block; asks and memory stay outside.
    quoted = _wrap(escape_markdown_v2(choices))
    return f"{escaped_outside}\n{quoted}"


def render_decision_sheet(
    definitions: list[dict[str, Any]],
    draft: dict[str, Any] | None = None,
    revision: int = 1,
    *,
    budget: int = DECISION_SHEET_BUDGET,
) -> str:
    """Render the static Decisions sheet within *budget* characters.

    Budgets are enforced by rendering whole fields/lines, never by slicing
    MarkdownV2 strings. Exactly three ordered degradations: drop labels on
    non-default choices, drop the remaining choice labels, then wrap only
    the choice lines in real expandable MarkdownV2 blockquotes. Every ask,
    starred default, why, memory, and quote survives every stage.
    """
    if not definitions:
        return ""
    # Static sheet shows effective defaults; live values live in the keyboard.
    _values = current_values(
        definitions, draft if draft is not None else effective_values(definitions)
    )
    sheet = sheet_for(definitions, _values, revision)
    frozen: list[dict[str, Any]] = definitions
    if isinstance(sheet, dict) and isinstance(sheet.get("rows"), list):
        # Prefer the sheet's rows when the core API is available so the
        # Telegram text matches the shared Decision Sheet record.
        frozen = _sheet_rows_to_definitions(sheet)
        if not frozen:
            frozen = definitions
    candidates = [
        _render_sheet_text(frozen, _values),
        _render_sheet_text(frozen, _values, drop_non_default_labels=True),
        _render_sheet_text(frozen, _values, drop_all_labels=True),
    ]
    for candidate in candidates:
        escaped = escape_markdown_v2(candidate)
        if len(escaped) <= budget:
            return escaped
    # Third and final degradation: only the choice lines enter a real
    # expandable blockquote; asks and memory stay outside. Expandable
    # markup does not shrink serialized text, so protected content alone
    # past the trigger is preserved whole rather than degraded further.
    try:
        return _render_sheet_expandable(frozen, _values)
    except Exception:
        return escape_markdown_v2(
            _render_sheet_text(frozen, _values, drop_all_labels=True)
        )


def _sheet_rows_to_definitions(sheet: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert Decision Sheet rows back to definition-like dicts for text."""
    rows = sheet.get("rows")
    if not isinstance(rows, list):
        return []
    definitions: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        memory = row.get("memory")
        definition: dict[str, Any] = {
            "id": row.get("id", ""),
            "kind": row.get("kind", "toggle"),
            "ask": row.get("ask", ""),
            "why": row.get("why"),
            "choices": row.get("choices", []),
            "default": row.get("default"),
        }
        if memory is not None:
            definition["memory"] = memory
        definitions.append(definition)
    return definitions


__all__ = [
    "render_decision_sheet",
    "strip_decision_bookkeeping",
]
