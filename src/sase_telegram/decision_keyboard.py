"""Live decision keyboard rows for Telegram plan reviews (sase-1hi.7)."""

from __future__ import annotations

from inspect import signature
from typing import Any

from telegram import InlineKeyboardButton

from sase_telegram import callback_data
from sase_telegram.gate_flow import GateProgress, GateView
from sase_telegram.plan_decisions import (
    changed_count,
    current_values,
    effective_values,
    encode_back_token,
    encode_open_token,
    encode_reset_token,
    encode_set_token,
    sheet_for,
    summary_for,
    value_token_for,
)

_RESET_LABEL = "↺ Reset"
_BACK_LABEL = "↩ Back"


def _supports_style() -> bool:
    try:
        return "style" in signature(InlineKeyboardButton).parameters
    except Exception:
        return False


def _button(text: str, cb: str, prefix: str) -> InlineKeyboardButton:
    encoded = callback_data.encode("gate", prefix, cb)
    if _supports_style():
        try:
            return InlineKeyboardButton(text, callback_data=encoded, style="success")  # type: ignore[call-arg]
        except Exception:
            return InlineKeyboardButton(text, callback_data=encoded)
    return InlineKeyboardButton(text, callback_data=encoded)


def _plain_button(text: str, prefix: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text, callback_data=callback_data.encode("gate", prefix, cb)
    )


def primary_button(text: str, cb: str, prefix: str) -> InlineKeyboardButton:
    """Primary Tale/Epic action with success styling when supported."""
    return _button(text, cb, prefix)


def primary_label(view: GateView, progress: GateProgress) -> str:
    """Return the decision-plan primary button label."""
    definitions = [dict(item) for item in view.decisions]
    draft = progress.decision_values
    values = current_values(definitions, draft)
    changes = changed_count(definitions, values)
    base = "Epic" if view.kind == "epic_plan" else "Tale"
    if changes == 0:
        label = f"✅ {base} · defaults"
    elif changes == 1:
        label = f"✅ {base} · 1 change"
    else:
        label = f"✅ {base} · {changes} changes"
    sheet = sheet_for(definitions, values, _displayed_revision(view, progress))
    if isinstance(sheet, dict):
        short = summary_for(sheet, "tale", "short")
        if "🧠" in str(short):
            label += " · 🧠"
    return label


def _displayed_revision(view: GateView, progress: GateProgress) -> int:
    if progress.displayed_revision is not None:
        return int(progress.displayed_revision)
    return int(view.review_revision)


def decision_rows(
    prefix: str, view: GateView, progress: GateProgress
) -> list[list[InlineKeyboardButton]]:
    """Return decision rows above the existing AND option controls."""
    if not view.decisions:
        return []
    definitions = [dict(item) for item in view.decisions]
    draft = progress.decision_values
    values = current_values(definitions, draft)
    defaults = effective_values(definitions)
    revision = _displayed_revision(view, progress)
    rows: list[list[InlineKeyboardButton]] = []
    if progress.open_choice_index is not None:
        rows.extend(
            _sub_keyboard(
                prefix,
                definitions,
                values,
                defaults,
                progress.open_choice_index,
                revision,
            )
        )
        return rows
    pending_toggles: list[InlineKeyboardButton] = []
    for index, definition in enumerate(definitions):
        decision_id = str(definition.get("id", ""))
        kind = str(definition.get("kind", ""))
        value = values.get(decision_id, definition.get("default"))
        changed = value != definition.get("default")
        mark = " ●" if changed else ""
        if kind == "choice":
            text = f"◉ {decision_id}: {value}{mark} ▾"
            rows.extend(_flush_toggles(pending_toggles))
            rows.append(
                [_plain_button(text, prefix, encode_open_token(index, revision))]
            )
        else:
            checked = "☑️" if bool(value) else "⬜"
            brain = "🧠 " if definition.get("memory") is not None else ""
            text = f"{checked} {brain}{decision_id}{mark}"
            value_token = "0" if bool(value) else "1"
            button = _plain_button(
                text, prefix, encode_set_token(index, value_token, revision)
            )
            # Pair toggle rows where labels fit.
            if len(text) <= 24 and len(pending_toggles) == 1:
                pending_toggles.append(button)
                rows.append(list(pending_toggles))
                pending_toggles = []
            elif len(text) <= 24:
                pending_toggles.append(button)
            else:
                rows.extend(_flush_toggles(pending_toggles))
                rows.append([button])
    rows.extend(_flush_toggles(pending_toggles))
    if changed_count(definitions, values) > 0:
        rows.append([_plain_button(_RESET_LABEL, prefix, encode_reset_token(revision))])
    return rows


def _flush_toggles(
    pending: list[InlineKeyboardButton],
) -> list[list[InlineKeyboardButton]]:
    if not pending:
        return []
    rows = [list(pending)]
    pending.clear()
    return rows


def _sub_keyboard(
    prefix: str,
    definitions: list[dict[str, Any]],
    values: dict[str, Any],
    defaults: dict[str, Any],
    open_index: int,
    revision: int,
) -> list[list[InlineKeyboardButton]]:
    if not (0 <= open_index < len(definitions)):
        return []
    definition = definitions[open_index]
    decision_id = str(definition.get("id", ""))
    current = values.get(decision_id)
    default = definition.get("default")
    rows: list[list[InlineKeyboardButton]] = []
    for choice in definition.get("choices", []) or []:
        if not isinstance(choice, dict):
            continue
        key = str(choice.get("key", ""))
        selected = "◉" if key == current else "○"
        star = " ★" if key == default else ""
        dot = " ●" if key == current and key != default else ""
        text = f"{selected} {key}{star}{dot}"
        value_token = value_token_for(definitions, open_index, key, key)
        rows.append(
            [
                _plain_button(
                    text, prefix, encode_set_token(open_index, value_token, revision)
                )
            ]
        )
    rows.append([_plain_button(_BACK_LABEL, prefix, encode_back_token(revision))])
    return rows


def toast_for_set(definitions: list[dict[str, Any]], index: int, value: Any) -> str:
    """Return the callback toast for one explicit value set."""
    if not (0 <= index < len(definitions)):
        return "Updated"
    definition = definitions[index]
    decision_id = str(definition.get("id", ""))
    if str(definition.get("kind", "")) == "choice":
        return f"{decision_id} → {value}"
    word = "yes" if bool(value) else "no"
    if definition.get("memory") is not None:
        selectors = definition.get("memory", {}).get("selectors", [])
        note = str(selectors[0]) if selectors else "memory"
        verb = "authorizes editing" if bool(value) else "leaves off"
        return f"🧠 {decision_id} {word} — {verb} {note}"
    return f"{decision_id} → {word}"


__all__ = [
    "decision_rows",
    "primary_button",
    "primary_label",
    "toast_for_set",
]
