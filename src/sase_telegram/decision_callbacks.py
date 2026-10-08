"""Decision-plan callback handling for Telegram (sase-1hi.7).

Revision-bound tokens, explicit sets, stale-card refresh, and submission
merging. Editing, back, and reset never submit a process.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sase_telegram.gate_flow import GateProgress, GateView, save_progress
from sase_telegram.plan_decisions import (
    STALE_TEXT,
    current_values,
    decode_set_value,
    encode_refresh_token,
    parse_decision_token,
    split_bound_token,
)


def check_revision(
    view: GateView, progress: GateProgress, token_revision: int | None
) -> bool:
    """Return whether *token_revision* matches displayed and envelope state."""
    if not view.decisions:
        return True
    if token_revision is None:
        return False
    displayed = (
        progress.displayed_revision
        if progress.displayed_revision is not None
        else view.review_revision
    )
    return int(token_revision) == int(displayed) == int(view.review_revision)


def stale_response() -> tuple[str, str]:
    """Return the exact stale answer text and refresh label."""
    return STALE_TEXT, "↻ Refresh review"


def _declared_decision_keys(view: GateView, option_id: str) -> set[str]:
    """Return the ``decision_*`` input keys one option actually declares."""
    from sase_telegram.gate_flow import option_for_id

    option = option_for_id(view, option_id)
    if option is None:
        return set()
    schema = getattr(option, "input_schema", {}) or {}
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    if not isinstance(props, dict):
        return set()
    return {str(k) for k in props if str(k).startswith("decision_")}


def build_selected_option_inputs(
    view: GateView,
    progress: GateProgress,
    selected_option_ids: tuple[str, ...] | list[str],
    base_inputs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build ``option_inputs`` containing only selected schemas.

    Starts from the actual selected option ids (never manufacturing
    approve/commit entries). For each selected option, merges only the
    corresponding ``decision_*`` fields its raw ``input_schema.properties``
    declares. Ordinary declared inputs from *base_inputs* are preserved.
    Reject receives an empty object; feedback receives its own provisional
    vector. When approve and commit are both selected, both receive
    identical decision values.
    """
    from sase_telegram.plan_decisions import current_values

    definitions = [dict(item) for item in (view.decisions or ())]
    draft = dict(progress.decision_values or {}) if progress is not None else {}
    full_vector = current_values(definitions, draft) if definitions else {}
    full_decision_inputs = {f"decision_{k}": v for k, v in full_vector.items()}
    selected = tuple(selected_option_ids or ())
    out: dict[str, dict[str, Any]] = {}
    for option_id in selected:
        if option_id == "reject":
            out[option_id] = {}
            continue
        declared = _declared_decision_keys(view, option_id)
        merged: dict[str, Any] = {}
        if base_inputs is not None and option_id in base_inputs:
            merged.update(dict(base_inputs[option_id] or {}))
        for key in declared:
            if key in full_decision_inputs:
                merged[key] = full_decision_inputs[key]
        # Drop any undeclared decision_* that leaked via base inputs.
        for key in list(merged):
            if str(key).startswith("decision_") and key not in declared:
                merged.pop(key, None)
        out[option_id] = merged
    return out


def apply_decision_token(
    view: GateView, progress: GateProgress, token: str
) -> tuple[GateProgress, str, bool]:
    """Apply one decision token to *progress*.

    Returns ``(progress, toast, submitted)`` where ``submitted`` is always
    False: edits never submit. Raises ``ValueError`` on malformed tokens
    and ``StaleReview`` on revision mismatch.
    """
    parsed = parse_decision_token(token)
    if parsed is None:
        raise ValueError("not a decision token")
    # Explicit refresh recovers the whole review: handle it before the
    # displayed-revision check so a stale card can still refresh.
    if str(parsed.get("kind", "")) == "refresh":
        return _apply_refresh_token(view, progress, int(parsed["revision"]))
    revision = int(parsed["revision"])
    if not check_revision(view, progress, revision):
        raise StaleReview(STALE_TEXT)
    definitions = [dict(item) for item in view.decisions]
    draft = dict(progress.decision_values or {})
    kind = str(parsed["kind"])
    if kind == "open":
        index = int(parsed["index"])
        if not (0 <= index < len(definitions)):
            raise ValueError("unknown decision")
        if str(definitions[index].get("kind", "")) != "choice":
            raise ValueError("not a choice")
        ask = str(definitions[index].get("ask", ""))
        updated = replace(progress, open_choice_index=index)
        save_progress(view, updated)
        return updated, ask or "Choose a value", False
    if kind == "back":
        updated = replace(progress, open_choice_index=None)
        save_progress(view, updated)
        return updated, "Back to review", False
    if kind == "reset":
        updated = replace(progress, decision_values=None, open_choice_index=None)
        save_progress(view, updated)
        return updated, "Reset to defaults", False
    # set
    index = int(parsed["index"])
    value = decode_set_value(definitions, index, str(parsed["value"]))
    if value is None:
        raise ValueError("unknown decision value")
    decision_id = str(definitions[index].get("id", ""))
    draft[decision_id] = value
    from sase_telegram.decision_keyboard import toast_for_set

    toast = toast_for_set(definitions, index, value)
    updated = replace(progress, decision_values=draft, open_choice_index=None)
    save_progress(view, updated)
    return updated, toast, False


def _current_definitions_and_revision(
    view: GateView,
) -> tuple[list[dict[str, Any]], int]:
    """Reload the verified current bundle for refresh, falling back to *view*."""
    fallback_defs = [dict(item) for item in view.decisions]
    fallback_rev = int(view.review_revision)
    try:
        from sase.notification_gates.hashing import load_and_verify_bundle
    except Exception:
        return fallback_defs, fallback_rev
    try:
        envelope, _adapter = load_and_verify_bundle(view.bundle_path)
    except Exception:
        return fallback_defs, fallback_rev
    try:
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        raw = payload.get("decisions") if isinstance(payload, dict) else None
        defs = (
            [dict(d) for d in raw if isinstance(d, dict)]
            if isinstance(raw, list)
            else []
        )
    except Exception:
        defs = []
    try:
        raw_rev = (
            envelope.get("review_revision", fallback_rev)
            if isinstance(envelope, dict)
            else fallback_rev
        )
        rev = int(raw_rev)
    except (TypeError, ValueError):
        rev = fallback_rev
    return (defs if defs else fallback_defs), rev


def _apply_refresh_token(
    view: GateView, progress: GateProgress, _token_revision: int
) -> tuple[GateProgress, str, bool]:
    """Recover the whole review on explicit refresh, ignoring stale revisions.

    Reloads the verified current bundle, retains only still-valid Telegram
    draft values, resets the open choice, and binds refreshed controls to
    the current revision. Never submits an answer.
    """
    from sase_telegram.plan_decisions import current_values, effective_values

    definitions, current_revision = _current_definitions_and_revision(view)
    draft = dict(progress.decision_values or {})
    values = current_values(definitions, draft)
    cleaned = {
        key: value
        for key, value in values.items()
        if _still_valid(definitions, key, value)
    }
    defaults = effective_values(definitions)
    draft_out = {
        key: value for key, value in cleaned.items() if value != defaults.get(key)
    }
    updated = replace(
        progress,
        displayed_revision=int(current_revision),
        decision_values=draft_out or None,
        open_choice_index=None,
    )
    save_progress(view, updated)
    return updated, "Review refreshed", False


def _still_valid(definitions: list[dict[str, Any]], key: str, value: Any) -> bool:
    for definition in definitions:
        if str(definition.get("id", "")) != key:
            continue
        if str(definition.get("kind", "")) == "choice":
            allowed = {
                str(c.get("key", ""))
                for c in (definition.get("choices", []) or [])
                if isinstance(c, dict)
            }
            return str(value) in allowed
        return isinstance(value, bool)
    return False


class StaleReview(ValueError):
    """A callback or submit referenced a revision that has moved."""


def decision_inputs_for(view: GateView, progress: GateProgress) -> dict[str, Any]:
    """Return the current decision vector as ``decision_<id>`` inputs."""
    if not view.decisions:
        return {}
    definitions = [dict(item) for item in view.decisions]
    values = current_values(definitions, progress.decision_values)
    return {f"decision_{key}": value for key, value in values.items()}


def displayed_revision(view: GateView, progress: GateProgress) -> int | None:
    """Return the revision the card displayed, if a decision plan."""
    if not view.decisions:
        return None
    if progress.displayed_revision is not None:
        return int(progress.displayed_revision)
    return int(view.review_revision)


def refresh_token_for(view: GateView) -> str:
    """Return the stale-refresh token for the current envelope revision."""
    return encode_refresh_token(int(view.review_revision))


__all__ = [
    "StaleReview",
    "apply_decision_token",
    "build_selected_option_inputs",
    "check_revision",
    "decision_inputs_for",
    "displayed_revision",
    "refresh_token_for",
    "split_bound_token",
    "stale_response",
]
