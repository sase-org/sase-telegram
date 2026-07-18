"""Server-side progress for Telegram notification-gate interactions."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sase.notification_gates.hashing import load_and_verify_bundle
from sase.notification_gates.models import (
    GateError,
    GateFeedbackMode,
    GateGroup,
    GateOption,
)

PROGRESS_FILENAME = "telegram_gate_progress.json"


@dataclass(frozen=True)
class GateView:
    """Verified gate data needed by Telegram formatting and callbacks."""

    bundle_path: Path
    request_id: str
    kind: str
    options: tuple[GateOption, ...]
    groups: tuple[GateGroup, ...]
    branches: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class GateProgress:
    """Telegram-private option selection and expanded-group state."""

    selected_option_ids: tuple[str, ...] = ()
    expanded_branch_index: int | None = None
    active_message_id: int | None = None
    chat_id: str | None = None


def load_gate_view(
    action_data: Mapping[str, Any], *, expected_kind: str | None = None
) -> GateView:
    """Load and verify the v2 gate referenced by notification action data."""
    raw_bundle = action_data.get("bundle_path")
    if not isinstance(raw_bundle, str) or not raw_bundle.strip():
        raise GateError(
            "missing_gate", "bundle_path", "notification has no gate bundle"
        )
    action_by_kind = {
        "custom": "CustomGate",
        "epic_plan": "EpicApproval",
        "hitl": "HITL",
        "launch": "LaunchApproval",
        "plan": "PlanApproval",
        "question": "UserQuestion",
    }
    action = action_by_kind.get(expected_kind or str(action_data.get("request_kind")))
    if action is None:
        raise GateError("invalid_request", "kind", "unsupported Telegram gate kind")
    from sase.notification_gates.paths import resolve_action_bundle

    normalized_action_data = {
        str(key): str(value) for key, value in action_data.items()
    }
    bundle = resolve_action_bundle(action, normalized_action_data)
    if bundle is None or bundle.legacy:
        raise GateError(
            "missing_gate", "bundle_path", "notification has no v2 gate bundle"
        )
    bundle_path = bundle.root
    if bundle_path.resolve(strict=False) != Path(raw_bundle).expanduser().resolve(
        strict=False
    ):
        raise GateError(
            "invalid_request",
            "bundle_path",
            "notification gate identity does not match its bundle path",
        )
    envelope, adapter = load_and_verify_bundle(bundle_path)
    if expected_kind is not None and adapter.kind != expected_kind:
        raise GateError(
            "invalid_request",
            "kind",
            f"expected a {expected_kind} gate, found {adapter.kind}",
        )
    raw_options = envelope.get("options")
    raw_groups = envelope.get("groups")
    raw_branches = envelope.get("branches")
    if not isinstance(raw_options, list) or not raw_options:
        raise GateError("invalid_request", "options", "gate has no options")
    if not isinstance(raw_groups, list) or not isinstance(raw_branches, list):
        raise GateError(
            "invalid_request", "branches", "gate branch metadata is missing"
        )
    default_feedback: GateFeedbackMode = (
        "optional" if adapter.kind == "custom" else "disabled"
    )
    options = tuple(
        GateOption.from_mapping(raw, index, default_feedback=default_feedback)
        for index, raw in enumerate(raw_options)
    )
    groups = tuple(
        GateGroup.from_mapping(raw, index) for index, raw in enumerate(raw_groups)
    )
    branches = tuple(
        tuple(str(option_id) for option_id in branch)
        for branch in raw_branches
        if isinstance(branch, list)
    )
    if not branches or len(branches) != len(raw_branches):
        raise GateError("invalid_request", "branches", "gate has invalid branches")
    return GateView(
        bundle_path=bundle_path,
        request_id=str(envelope.get("request_id") or bundle_path.name),
        kind=adapter.kind,
        options=options,
        groups=groups,
        branches=branches,
    )


def progress_path(view: GateView) -> Path:
    """Return the Telegram-private progress path for a verified gate."""
    return view.bundle_path / PROGRESS_FILENAME


def initial_progress(
    view: GateView,
    *,
    active_message_id: int | None = None,
    chat_id: str | None = None,
) -> GateProgress:
    """Create progress with the sole AND group expanded, when present."""
    group_indexes = and_branch_indexes(view)
    expanded = group_indexes[0] if len(group_indexes) == 1 else None
    selected = default_selection(view, expanded) if expanded is not None else ()
    return GateProgress(
        selected_option_ids=selected,
        expanded_branch_index=expanded,
        active_message_id=active_message_id,
        chat_id=chat_id,
    )


def load_progress(
    view: GateView,
    *,
    active_message_id: int | None = None,
    chat_id: str | None = None,
) -> GateProgress:
    """Load saved progress, recovering safely from stale or malformed state."""
    fallback = initial_progress(
        view,
        active_message_id=active_message_id,
        chat_id=chat_id,
    )
    path = progress_path(view)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    if not isinstance(raw, dict):
        return fallback

    saved_message_id = _optional_int(raw.get("active_message_id")) or active_message_id
    saved_chat_id = str(raw["chat_id"]) if raw.get("chat_id") is not None else chat_id
    group_indexes = and_branch_indexes(view)
    raw_expanded = _optional_int(raw.get("expanded_branch_index"))
    if len(group_indexes) == 1:
        expanded = group_indexes[0]
    elif raw_expanded in group_indexes:
        expanded = raw_expanded
    else:
        expanded = None
    if expanded is None:
        selected_ids: tuple[str, ...] = ()
    else:
        selected = raw.get("selected_option_ids")
        if not (
            isinstance(selected, list)
            and all(isinstance(item, str) for item in selected)
        ):
            selected_ids = default_selection(view, expanded)
        else:
            selected_set = set(selected)
            selected_ids = tuple(
                option_id
                for option_id in view.branches[expanded]
                if option_id in selected_set
            )
    return GateProgress(
        selected_option_ids=selected_ids,
        expanded_branch_index=expanded,
        active_message_id=saved_message_id,
        chat_id=saved_chat_id,
    )


def save_progress(view: GateView, progress: GateProgress) -> None:
    """Atomically persist Telegram gate progress next to the gate envelope."""
    path = progress_path(view)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "selected_option_ids": list(progress.selected_option_ids),
        "expanded_branch_index": progress.expanded_branch_index,
        "active_message_id": progress.active_message_id,
        "chat_id": progress.chat_id,
    }
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def clear_progress(view: GateView) -> None:
    """Remove Telegram-private state after resolution or cancellation."""
    progress_path(view).unlink(missing_ok=True)


def option_for_id(view: GateView, option_id: str) -> GateOption | None:
    """Return a verified option by id."""
    return next((option for option in view.options if option.id == option_id), None)


def option_index(view: GateView, option_id: str) -> int:
    """Return the stable envelope index for one option id."""
    for index, option in enumerate(view.options):
        if option.id == option_id:
            return index
    raise ValueError(f"unknown gate option: {option_id}")


def branch_for_token(
    view: GateView, token: str, *, prefix: str
) -> tuple[int, tuple[str, ...]] | None:
    """Resolve a compact branch token such as ``c0`` or ``s1``."""
    index = _token_index(token, prefix)
    if index is None or index >= len(view.branches):
        return None
    return index, view.branches[index]


def option_for_token(view: GateView, token: str) -> GateOption | None:
    """Resolve a compact ``x<index>`` option token."""
    index = _token_index(token, "x")
    if index is None or index >= len(view.options):
        return None
    return view.options[index]


def and_branch_indexes(view: GateView) -> tuple[int, ...]:
    """Return query-order indexes of every AND branch."""
    return tuple(index for index, branch in enumerate(view.branches) if len(branch) > 1)


def group_for_branch(view: GateView, branch: Sequence[str]) -> GateGroup | None:
    """Return submit metadata for one AND branch."""
    members = tuple(branch)
    return next((group for group in view.groups if group.options == members), None)


def default_selection(view: GateView, branch_index: int) -> tuple[str, ...]:
    """Return default-selected members of one AND branch in query order."""
    branch = view.branches[branch_index]
    by_id = {option.id: option for option in view.options}
    return tuple(option_id for option_id in branch if by_id[option_id].default_selected)


def expand_branch(
    view: GateView, progress: GateProgress, branch_index: int
) -> GateProgress:
    """Expand one AND branch and restore its configured default selection."""
    if branch_index not in and_branch_indexes(view):
        raise ValueError("only an AND branch can be expanded")
    return replace(
        progress,
        expanded_branch_index=branch_index,
        selected_option_ids=default_selection(view, branch_index),
    )


def toggle_option(
    view: GateView, progress: GateProgress, token: str
) -> tuple[GateProgress, bool]:
    """Toggle one compact ``x<index>`` group member and return its new state."""
    expanded = progress.expanded_branch_index
    if expanded is None:
        raise ValueError("open a gate group before toggling options")
    option = option_for_token(view, token)
    if option is None or option.id not in view.branches[expanded]:
        raise ValueError("unknown gate option")
    selected = set(progress.selected_option_ids)
    if option.id in selected:
        selected.remove(option.id)
        enabled = False
    else:
        selected.add(option.id)
        enabled = True
    ordered = tuple(
        option_id for option_id in view.branches[expanded] if option_id in selected
    )
    return replace(progress, selected_option_ids=ordered), enabled


def feedback_mode(
    view: GateView, selected_option_ids: Sequence[str]
) -> GateFeedbackMode:
    """Return the strongest feedback mode among selected options."""
    ranks: dict[GateFeedbackMode, int] = {
        "disabled": 0,
        "optional": 1,
        "required": 2,
    }
    options = [option_for_id(view, option_id) for option_id in selected_option_ids]
    available = [option.feedback for option in options if option is not None]
    return max(available, key=ranks.__getitem__) if available else "disabled"


def feedback_is_command_input(
    view: GateView, selected_option_ids: Sequence[str]
) -> bool:
    """Return whether a selected option requires feedback in its command input."""
    for option_id in selected_option_ids:
        option = option_for_id(view, option_id)
        if option is None:
            continue
        required = option.input_schema.get("required")
        if isinstance(required, list) and "feedback" in required:
            return True
    return False


def _token_index(token: str, prefix: str) -> int | None:
    if not token.startswith(prefix):
        return None
    try:
        value = int(token[len(prefix) :])
    except ValueError:
        return None
    return value if value >= 0 else None


def _optional_int(value: object) -> int | None:
    if not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
