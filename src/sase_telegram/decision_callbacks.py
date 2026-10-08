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
    if kind == "refresh":
        # Explicit refresh reloads current prose/sheet, retains valid
        # drafts, persists the new revision, and requires another tap.
        values = current_values(definitions, draft)
        cleaned = {
            key: value
            for key, value in values.items()
            if _still_valid(definitions, key, value)
        }
        # Only non-default values persist as drafts.
        from sase_telegram.plan_decisions import effective_values

        defaults = effective_values(definitions)
        draft_out = {
            key: value for key, value in cleaned.items() if value != defaults.get(key)
        }
        updated = replace(
            progress,
            displayed_revision=view.review_revision,
            decision_values=draft_out or None,
            open_choice_index=None,
        )
        save_progress(view, updated)
        return updated, "Review refreshed", False
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
    "check_revision",
    "decision_inputs_for",
    "displayed_revision",
    "refresh_token_for",
    "split_bound_token",
    "stale_response",
]
