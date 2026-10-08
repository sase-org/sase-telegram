"""Settled review cards for Telegram decision plans (sase-1hi.7).

One receipt path for Telegram completion and external settlement. Renders
accepted values from authoritative normalized inputs/results or the stamped
plan, never from a Telegram draft or a hash-only receipt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sase_telegram.gate_flow import GateView
from sase_telegram.plan_decisions import sheet_for, summary_for


def authoritative_values(view: GateView) -> dict[str, Any] | None:
    """Return accepted decision values from authoritative sources."""
    if not view.decisions:
        return None
    response = _load_json(view.bundle_path / "response.json")
    if isinstance(response, dict):
        values = _values_from_response(view, response)
        if values is not None:
            return values
    stamped = _values_from_stamped_plan(view)
    if stamped is not None:
        return stamped
    return None


def _values_from_response(
    view: GateView, response: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        from sase.notification_gates.model_results import effective_response_input
    except Exception:
        return None
    selected = response.get("selected_option_ids")
    if not isinstance(selected, list) or not selected:
        return None
    first = str(selected[0])
    try:
        inputs = effective_response_input(response, first)
    except Exception:
        return None
    values: dict[str, Any] = {}
    for item in view.decisions:
        decision_id = str(item.get("id", ""))
        key = f"decision_{decision_id}"
        if key in inputs:
            values[decision_id] = inputs[key]
    if not values:
        return None
    return values


def _values_from_stamped_plan(view: GateView) -> dict[str, Any] | None:
    try:
        from sase.sdd.plan_decision_handoff import load_stamped_decisions
    except Exception:
        return None
    plan_file = _plan_file_for_view(view)
    if plan_file is None:
        return None
    try:
        stamped = load_stamped_decisions(plan_file)
    except Exception:
        return None
    if stamped is None:
        return None
    return dict(stamped.values)


def _plan_file_for_view(view: GateView) -> Path | None:
    for candidate in (view.bundle_path / "request.json",):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        action_data = payload.get("action_data") if isinstance(payload, dict) else None
        if isinstance(action_data, dict):
            raw = action_data.get("original_plan_file")
            if isinstance(raw, str) and raw:
                path = Path(raw).expanduser()
                if path.is_file():
                    return path
    return None


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def receipt_text(
    view: GateView,
    values: dict[str, Any],
    *,
    verdict: str = "Tale",
    decider: str = "you",
    surface: str = "Telegram",
    when: str = "",
) -> str:
    """Render the answered receipt with verdict, values, and full summary."""
    definitions = [dict(item) for item in view.decisions]
    sheet = sheet_for(definitions, values, view.review_revision)
    summary = ""
    if isinstance(sheet, dict):
        summary = summary_for(sheet, "tale", "full")
    header = f"✅ {verdict} approved · {decider} via {surface}"
    if when:
        header += f" · {when}"
    lines = [header]
    for number, definition in enumerate(definitions, start=1):
        decision_id = str(definition.get("id", ""))
        value = values.get(decision_id, definition.get("default"))
        default = definition.get("default")
        changed = " ● (★ default)" if value != default else " ★"
        brain = ""
        memory = definition.get("memory")
        if memory is not None and isinstance(memory, dict):
            selectors = memory.get("selectors", [])
            note = str(selectors[0]) if selectors else "memory"
            brain = f" 🧠 {note}"
        lines.append(f"{number}. {decision_id} → {value}{changed}{brain}")
    if summary:
        lines.append(summary)
    return "\n".join(lines)


def launch_failed_text(base_receipt: str) -> str:
    """Retain immutable accepted values with a coder-start failure note."""
    return (
        f"{base_receipt}\nApproved with these choices · coder could not start · retry"
    )


__all__ = [
    "authoritative_values",
    "launch_failed_text",
    "receipt_text",
]
