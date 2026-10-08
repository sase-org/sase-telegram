"""Settled review cards for Telegram decision plans (sase-1hi.10.6).

One receipt path for Telegram completion and external settlement. Renders
accepted values from authoritative normalized inputs/results or the stamped
plan, never from a Telegram draft or a hash-only receipt.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from sase_telegram.gate_flow import GateView
from sase_telegram.plan_decisions import sheet_for, summary_for


def _facade() -> Any | None:
    try:
        import importlib

        return importlib.import_module("sase.sdd.plan_decisions")
    except Exception:
        return None


def authoritative_values(view: GateView) -> dict[str, Any] | None:
    """Return accepted decision values from authoritative sources."""
    if not view.decisions:
        return None
    response = _load_json(view.bundle_path / "response.json")
    if isinstance(response, dict):
        selected = response.get("selected_option_ids")
        if isinstance(selected, list) and any(
            str(s) in ("reject", "feedback") for s in selected
        ):
            # Reject and feedback are distinct outcomes; they may carry no
            # accepted decision values. Return {} so callers render their
            # own headers instead of treating them as approvals.
            if str(selected[0]) == "reject":
                return {}
            values = _values_from_response(view, response)
            return values if values is not None else {}
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
    mod = _facade()
    if mod is None or not hasattr(mod, "effective_response_input"):
        return None
    selected = response.get("selected_option_ids")
    if not isinstance(selected, list) or not selected:
        return None
    # Examine every selected decision-bearing option, not just the first.
    merged: dict[str, Any] = {}
    found_any = False
    for option_id in selected:
        oid = str(option_id)
        if oid in ("reject",):
            continue
        try:
            inputs = mod.effective_response_input(response, oid)
        except Exception:
            continue
        if not isinstance(inputs, dict):
            continue
        for item in view.decisions:
            decision_id = str(item.get("id", ""))
            key = f"decision_{decision_id}"
            if key in inputs:
                merged[decision_id] = inputs[key]
                found_any = True
    if not found_any:
        return None
    return merged


def _values_from_stamped_plan(view: GateView) -> dict[str, Any] | None:
    mod = _facade()
    if mod is None or not hasattr(mod, "load_stamped_decisions"):
        return None
    plan_file = _plan_file_for_view(view)
    if plan_file is None:
        return None
    try:
        stamped = mod.load_stamped_decisions(str(plan_file))
    except Exception:
        return None
    if stamped is None:
        return None
    try:
        values = dict(getattr(stamped, "values", {}))
    except Exception:
        return None
    return values


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
        # Frozen bundle fallback: request.json may carry the plan path
        # under payload or the bundle's own plan.md may be stamped.
        try:
            fallback = view.bundle_path / "plan.md"
            if fallback.is_file():
                return fallback
        except Exception:
            pass
    return None


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def approval_verdict(view: GateView, response: dict[str, Any] | None = None) -> str:
    """Return the true approval verdict for the summary sentence."""
    kind = str(getattr(view, "kind", "") or "")
    if kind == "epic_plan":
        return "epic launch"
    selected: list[str] = []
    if isinstance(response, dict):
        raw = response.get("selected_option_ids")
        if isinstance(raw, list):
            selected = [str(s) for s in raw]
    if "approve" in selected and "commit" in selected:
        return "coder + commit"
    if "approve" in selected:
        return "coder"
    if "commit" in selected:
        return "commit"
    # Default for tale callers without a response (tests, recovery).
    return "coder + commit" if kind != "epic_plan" else "epic launch"


def decider_surface(
    response: dict[str, Any] | None = None,
    stamped: Any | None = None,
) -> tuple[str, str]:
    """Return (decider, surface) from durable response facts.

    Maps reviewer/Telegram to you via Telegram, tui to via ACE, CLI to
    via CLI, and auto_resolution to auto; retains agent attribution when
    recorded. Never guesses Telegram for unknown provenance.
    """
    caller = ""
    source = ""
    if isinstance(response, dict):
        caller = str(response.get("caller", "") or "")
        source = str(response.get("source", "") or "")
    decided_by = ""
    decided_via = ""
    try:
        if stamped is not None:
            decided_by = str(getattr(stamped, "decided_by", "") or "")
            decided_via = str(getattr(stamped, "decided_via", "") or "")
    except Exception:
        pass
    # Stamped recovery takes precedence when the response is silent.
    if not caller and decided_by:
        caller = decided_by
    if not source and decided_via:
        source = decided_via
    # Agent attribution is retained explicitly.
    if caller == "agent":
        return ("agent", _surface_word(source) or "via CLI")
    if source in ("telegram", "Telegram") or caller == "reviewer":
        # Reviewer via Telegram is the human tapping the card.
        if source in ("", "telegram", "Telegram"):
            return ("you", "via Telegram")
        return ("you", _surface_word(source))
    if source == "tui" or decided_via == "tui":
        return ("you", "via ACE")
    if source in ("cli", "CLI"):
        return ("you", "via CLI")
    if source == "auto_resolution" or caller == "auto":
        return ("auto", "auto")
    if caller and source:
        return (caller, _surface_word(source))
    if caller:
        return (caller, _surface_word(source) or "via CLI")
    return ("you", "via Telegram") if source == "telegram" else ("unknown", "via CLI")


def _surface_word(source: str) -> str:
    mapping = {
        "telegram": "via Telegram",
        "Telegram": "via Telegram",
        "tui": "via ACE",
        "cli": "via CLI",
        "CLI": "via CLI",
        "auto_resolution": "auto",
        "auto": "auto",
        "mobile": "via mobile",
    }
    return mapping.get(str(source), f"via {source}" if source else "")


def format_when(response: dict[str, Any] | None = None) -> str:
    """Format response time from responded_at_unix consistently."""
    if not isinstance(response, dict):
        return ""
    raw = response.get("responded_at_unix")
    try:
        ts = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    try:
        dt = datetime.datetime.fromtimestamp(ts).astimezone()
        return dt.strftime("%H:%M")
    except Exception:
        return ""


def _display_toggle(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _change_suffix(definition: dict[str, Any], value: Any) -> str:
    default = definition.get("default")
    if value == default:
        return " ★"
    if isinstance(default, bool) or isinstance(value, bool):
        default_word = _display_toggle(default)
        return f" ● (★ {default_word})"
    return f" ● (★ {default})"


def receipt_text(
    view: GateView,
    values: dict[str, Any],
    *,
    verdict: str = "Tale",
    decider: str = "you",
    surface: str = "Telegram",
    when: str = "",
    response: dict[str, Any] | None = None,
    provisional: bool = False,
) -> str:
    """Render the answered receipt with verdict, values, and full summary."""
    definitions = [dict(item) for item in (view.decisions or ())]
    # Load the durable response when the caller did not pass it.
    if response is None:
        response = _load_json(view.bundle_path / "response.json")
        if not isinstance(response, dict):
            response = None
    selected: list[str] = []
    if isinstance(response, dict) and isinstance(
        response.get("selected_option_ids"), list
    ):
        selected = [str(s) for s in response["selected_option_ids"]]
    # Reject and feedback are distinct outcomes with their own headers,
    # including when they have no accepted decision values.
    if "reject" in selected or verdict == "Rejected":
        header = f"❌ Rejected · {decider} via {surface}"
        if when:
            header += f" · {when}"
        elif isinstance(response, dict):
            w = format_when(response)
            if w:
                header += f" · {w}"
        return header
    if "feedback" in selected or verdict == "Feedback":
        header = f"💬 Feedback sent · {decider} via {surface}"
        if when:
            header += f" · {when}"
        elif isinstance(response, dict):
            w = format_when(response)
            if w:
                header += f" · {w}"
        lines = [header]
        if values:
            lines.append("Provisional values (not accepted):")
            for number, definition in enumerate(definitions, start=1):
                decision_id = str(definition.get("id", ""))
                if decision_id not in values:
                    continue
                raw = values[decision_id]
                shown = (
                    _display_toggle(raw)
                    if str(definition.get("kind", "")) != "choice"
                    else str(raw)
                )
                lines.append(f"{number}. {decision_id} → {shown} (provisional)")
        return "\n".join(lines)

    # Approval path: Tale vs Epic from the gate kind; true verdict for summary.
    is_epic = str(getattr(view, "kind", "")) == "epic_plan"
    kind_word = "Epic" if is_epic else "Tale"
    true_verdict = approval_verdict(view, response)
    # Resolve decider/surface/time from durable facts when available.
    if response is not None:
        d, s = decider_surface(response)
        # Preserve explicit caller args only when they carry real provenance;
        # otherwise prefer durable facts. Tests pass decider/surface defaults.
        if (decider, surface) == ("you", "Telegram") or (decider, surface) == (
            "you",
            "via Telegram",
        ):
            decider, surface = d, s
        if not when:
            when = format_when(response)
    summary_verdict = true_verdict
    sheet = sheet_for(definitions, values, int(getattr(view, "review_revision", 1)))
    summary = ""
    if isinstance(sheet, dict):
        try:
            summary = summary_for(sheet, summary_verdict, "full")
        except Exception:
            summary = ""
    header = f"✅ {kind_word} approved · {decider} {surface}"
    if when:
        header += f" · {when}"
    lines = [header]
    for number, definition in enumerate(definitions, start=1):
        decision_id = str(definition.get("id", ""))
        raw_default = definition.get("default")
        value = values.get(decision_id, raw_default)
        if str(definition.get("kind", "")) == "choice":
            shown = str(value)
        else:
            shown = _display_toggle(value)
        suffix = _change_suffix(definition, value)
        brain = ""
        memory = definition.get("memory")
        if isinstance(memory, dict):
            selectors = memory.get("selectors", [])
            note = str(selectors[0]) if selectors else "memory"
            brain = f" 🧠 {note}"
        elif memory is not None:
            brain = " 🧠 memory"
        lines.append(f"{number}. {decision_id} → {shown}{suffix}{brain}")
    if summary:
        lines.append(f"→ {summary}" if not summary.startswith("→") else summary)
    else:
        # Fallback still names the true verdict.
        lines.append(f"→ {true_verdict}")
    return "\n".join(lines)


def launch_failed_text(base_receipt: str) -> str:
    """Retain immutable accepted values with a coder-start failure note."""
    return (
        f"{base_receipt}\nApproved with these choices · coder could not start · retry"
    )


__all__ = [
    "approval_verdict",
    "authoritative_values",
    "decider_surface",
    "format_when",
    "launch_failed_text",
    "receipt_text",
]
