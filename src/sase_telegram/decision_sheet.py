"""Static Decisions question sheet for Telegram plan reviews (sase-1hi.7)."""

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
        if kind == "core":
            parts.append(f"{label} (core · loaded every turn)")
        else:
            parts.append(f"{label} ({kind})")
    return ", ".join(parts)


def _render_sheet_text(
    definitions: list[dict[str, Any]],
    values: dict[str, Any],
    *,
    drop_non_default_labels: bool = False,
    drop_all_labels: bool = False,
) -> str:
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
            if isinstance(why, str) and why.strip() and not drop_all_labels:
                lines.append(f"   ★ {why.strip()}")
            elif isinstance(why, str) and why.strip() and drop_all_labels:
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
                lines.append(f"   🧠 {note_text} · {chip}")
            else:
                lines.append(f"   🧠 {chip}")
            if isinstance(quote, str) and quote.strip():
                lines.append(f'   "{quote.strip()}"')
    return "\n".join(lines)


def render_decision_sheet(
    definitions: list[dict[str, Any]],
    draft: dict[str, Any] | None = None,
    revision: int = 1,
    *,
    budget: int = DECISION_SHEET_BUDGET,
) -> str:
    """Render the static Decisions sheet within *budget* characters.

    Degrades in order: drop non-default labels, drop remaining labels,
    then wrap choice lines in an expandable blockquote. Every ask and its
    complete default line survive; never slices through a decision.
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
    # Final degrade: expandable blockquote for choice lines.
    smallest = escape_markdown_v2(candidates[-1])
    return f"> {smallest}" if len(smallest) <= budget else smallest[:budget]


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
