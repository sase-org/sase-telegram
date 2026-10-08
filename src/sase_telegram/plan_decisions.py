"""Telegram Plan Decisions adapter and rendering helpers (sase-1hi.7).

Consume decisions only through ``sase.sdd.plan_decisions``. Feature-detect
the public Decision Sheet and summary API, honor ``is_enabled()`` where
present, and fall back to today's behavior when the installed SASE lacks it.
"""

from __future__ import annotations

import re
from typing import Any

DECISION_SHEET_BUDGET = 1800
STALE_TEXT = "This plan changed since this card was shown."
REFRESH_LABEL = "↻ Refresh review"
RESET_LABEL = "↺ Reset"
BACK_LABEL = "↩ Back"

RECEIPT_TAG = "plan_decisions_receipt"

_DECISION_SET_RE = re.compile(r"^d(\d+)=(.+)r(\d+)$")
_DECISION_OPEN_RE = re.compile(r"^d(\d+)>r(\d+)$")
_DECISION_BACK_RE = re.compile(r"^d<r(\d+)$")
_DECISION_RESET_RE = re.compile(r"^dzr(\d+)$")
_DECISION_REFRESH_RE = re.compile(r"^dRr(\d+)$")
_BOUND_SUFFIX_RE = re.compile(
    r"^(?P<base>[csf]\d+|x\d+(?:=[01])?|i\d+(?:k|d|c|v\d+))r(?P<rev>\d+)$"
)


def _plan_decisions_module() -> Any | None:
    try:
        import importlib

        return importlib.import_module("sase.sdd.plan_decisions")
    except Exception:
        return None


def decisions_available() -> bool:
    """Return whether the installed SASE exposes the Decision Sheet API."""
    mod = _plan_decisions_module()
    if mod is None:
        return False
    try:
        if hasattr(mod, "is_enabled") and not bool(mod.is_enabled()):
            return False
    except Exception:
        return False
    for name in ("sheet_binding", "summary_binding"):
        if not hasattr(mod, name):
            return False
    return True


def sheet_for(
    definitions: list[dict[str, Any]],
    values: dict[str, Any],
    revision: int = 0,
) -> dict[str, Any] | None:
    """Build a Decision Sheet, returning None when unavailable."""
    mod = _plan_decisions_module()
    if mod is None:
        return None
    try:
        if hasattr(mod, "is_enabled") and not bool(mod.is_enabled()):
            return None
        return dict(mod.sheet_binding(definitions, dict(values), int(revision)))
    except Exception:
        return None


def summary_for(sheet: dict[str, Any], verdict: str, form: str) -> str:
    """Render the shared summary sentence, best-effort."""
    mod = _plan_decisions_module()
    if mod is None:
        return ""
    try:
        return str(mod.summary_binding(sheet, verdict, form))
    except Exception:
        return ""


def effective_values(definitions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the core effective defaults for frozen definitions."""
    values: dict[str, Any] = {}
    for definition in definitions:
        decision_id = str(definition.get("id", ""))
        if not decision_id:
            continue
        default = definition.get("default")
        effective = definition.get("effective_default", default)
        values[decision_id] = effective
    return values


def current_values(
    definitions: list[dict[str, Any]],
    draft: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge a Telegram draft over effective defaults, validating keys."""
    base = effective_values(definitions)
    if not draft:
        return base
    by_id = {str(d.get("id", "")): d for d in definitions}
    merged = dict(base)
    for key, value in draft.items():
        definition = by_id.get(str(key))
        if definition is None:
            continue
        kind = str(definition.get("kind", ""))
        if kind == "choice":
            allowed = {
                str(c.get("key", ""))
                for c in (definition.get("choices", []) or [])
                if isinstance(c, dict)
            }
            if str(value) in allowed:
                merged[str(key)] = str(value)
        else:
            if isinstance(value, bool):
                merged[str(key)] = value
    return merged


def changed_count(definitions: list[dict[str, Any]], values: dict[str, Any]) -> int:
    """Count values differing from the planner default."""
    total = 0
    for definition in definitions:
        decision_id = str(definition.get("id", ""))
        default = definition.get("default")
        if decision_id in values and values[decision_id] != default:
            total += 1
    return total


def validate_value(
    definitions: list[dict[str, Any]], decision_id: str, value: Any
) -> bool:
    """Return whether *value* is a declared value for *decision_id*."""
    for definition in definitions:
        if str(definition.get("id", "")) != decision_id:
            continue
        kind = str(definition.get("kind", ""))
        if kind == "choice":
            allowed = {
                str(c.get("key", ""))
                for c in (definition.get("choices", []) or [])
                if isinstance(c, dict)
            }
            return str(value) in allowed
        return isinstance(value, bool)
    return False


def parse_decision_token(token: str) -> dict[str, Any] | None:
    """Parse a revision-bound decision token, or None when not one."""
    match = _DECISION_SET_RE.fullmatch(token)
    if match is not None:
        return {
            "kind": "set",
            "index": int(match.group(1)),
            "value": match.group(2),
            "revision": int(match.group(3)),
        }
    match = _DECISION_OPEN_RE.fullmatch(token)
    if match is not None:
        return {
            "kind": "open",
            "index": int(match.group(1)),
            "revision": int(match.group(2)),
        }
    match = _DECISION_BACK_RE.fullmatch(token)
    if match is not None:
        return {"kind": "back", "revision": int(match.group(1))}
    match = _DECISION_RESET_RE.fullmatch(token)
    if match is not None:
        return {"kind": "reset", "revision": int(match.group(1))}
    match = _DECISION_REFRESH_RE.fullmatch(token)
    if match is not None:
        return {"kind": "refresh", "revision": int(match.group(1))}
    return None


def is_decision_token(token: str) -> bool:
    """Return whether *token* is a decision-plan token."""
    return parse_decision_token(token) is not None


def split_bound_token(token: str) -> tuple[str, int | None]:
    """Split a revision-bound verdict/AND/input token into (base, revision)."""
    match = _BOUND_SUFFIX_RE.fullmatch(token)
    if match is None:
        return token, None
    return str(match.group("base")), int(match.group("rev"))


def encode_set_token(index: int, value_token: str, revision: int) -> str:
    """Encode an explicit set token such as ``d0=k1r4``."""
    return f"d{index}={value_token}r{revision}"


def encode_open_token(index: int, revision: int) -> str:
    """Encode a choice-open token such as ``d0>r4``."""
    return f"d{index}>r{revision}"


def encode_back_token(revision: int) -> str:
    """Encode the sub-keyboard back token."""
    return f"d<r{revision}"


def encode_reset_token(revision: int) -> str:
    """Encode the reset token."""
    return f"dzr{revision}"


def encode_refresh_token(revision: int) -> str:
    """Encode the stale-refresh token."""
    return f"dRr{revision}"


def value_token_for(
    definitions: list[dict[str, Any]], index: int, choice_key: str | None, value: Any
) -> str:
    """Return the value part of a set token for one decision."""
    definition = definitions[index]
    if str(definition.get("kind", "")) == "choice":
        choices = [str(c.get("key", "")) for c in definition.get("choices", [])]
        try:
            return f"k{choices.index(str(choice_key if choice_key is not None else value))}"
        except ValueError:
            return "k0"
    return "1" if bool(value) else "0"


def decode_set_value(
    definitions: list[dict[str, Any]], index: int, raw: str
) -> Any | None:
    """Resolve a set-token value part to a concrete decision value."""
    if not (0 <= index < len(definitions)):
        return None
    definition = definitions[index]
    if str(definition.get("kind", "")) == "choice":
        choices = [str(c.get("key", "")) for c in definition.get("choices", [])]
        candidate = raw[1:] if raw.startswith("k") else raw
        if candidate.isdigit():
            choice_index = int(candidate)
            if 0 <= choice_index < len(choices):
                return choices[choice_index]
            return None
        if candidate in choices:
            return candidate
        return None
    if raw == "1":
        return True
    if raw == "0":
        return False
    return None


__all__ = [
    "BACK_LABEL",
    "DECISION_SHEET_BUDGET",
    "RECEIPT_TAG",
    "REFRESH_LABEL",
    "RESET_LABEL",
    "STALE_TEXT",
    "changed_count",
    "current_values",
    "decisions_available",
    "decode_set_value",
    "effective_values",
    "encode_back_token",
    "encode_open_token",
    "encode_refresh_token",
    "encode_reset_token",
    "encode_set_token",
    "is_decision_token",
    "parse_decision_token",
    "sheet_for",
    "split_bound_token",
    "summary_for",
    "validate_value",
    "value_token_for",
]
