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
from sase.notification_gates.registry import adapter_for_kind

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
    decisions: tuple[dict[str, Any], ...] = ()
    review_revision: int = 1


@dataclass(frozen=True)
class GateProgress:
    """Telegram-private option selection and expanded-group state."""

    selected_option_ids: tuple[str, ...] = ()
    expanded_branch_index: int | None = None
    active_message_id: int | None = None
    chat_id: str | None = None
    input_option_ids: tuple[str, ...] = ()
    input_field_index: int | None = None
    input_values: dict[str, Any] | None = None
    input_feedback_requested: bool = False
    displayed_revision: int | None = None
    decision_values: dict[str, Any] | None = None
    open_choice_index: int | None = None
    source_message_id: int | None = None
    source_chat_id: str | None = None
    submitted_revision: int | None = None
    submitted_values: dict[str, Any] | None = None


def load_gate_view(
    action_data: Mapping[str, Any], *, expected_kind: str | None = None
) -> GateView:
    """Load and verify the v2 gate referenced by notification action data."""
    raw_bundle = action_data.get("bundle_path")
    if not isinstance(raw_bundle, str) or not raw_bundle.strip():
        raise GateError(
            "missing_gate", "bundle_path", "notification has no gate bundle"
        )
    raw_kind = expected_kind or action_data.get("request_kind")
    if not isinstance(raw_kind, str) or not raw_kind.strip():
        raise GateError("invalid_request", "kind", "Telegram gate kind is missing")
    requested_adapter = adapter_for_kind(raw_kind)
    if not requested_adapter.branch_actionable:
        raise GateError("invalid_request", "kind", "unsupported Telegram gate kind")
    from sase.notification_gates.paths import resolve_action_bundle

    normalized_action_data = {
        str(key): str(value) for key, value in action_data.items()
    }
    bundle = resolve_action_bundle(requested_adapter.action, normalized_action_data)
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
    if adapter.kind != requested_adapter.kind:
        raise GateError(
            "invalid_request",
            "kind",
            f"expected a {requested_adapter.kind} gate, found {adapter.kind}",
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
    options = tuple(
        GateOption.from_mapping(
            raw,
            index,
            default_feedback=adapter.default_feedback,
        )
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
    decisions = _frozen_decisions(envelope)
    review_revision = _envelope_revision(envelope)
    return GateView(
        bundle_path=bundle_path,
        request_id=str(envelope.get("request_id") or bundle_path.name),
        kind=adapter.kind,
        options=options,
        groups=groups,
        branches=branches,
        decisions=decisions,
        review_revision=review_revision,
    )


def _frozen_decisions(envelope: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return the envelope's frozen ``payload.decisions`` vector."""
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return ()
    raw = payload.get("decisions")
    if not isinstance(raw, list):
        return ()
    kept: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            kept.append({str(key): value for key, value in item.items()})
    return tuple(kept)


def _envelope_revision(envelope: Mapping[str, Any]) -> int:
    """Return the envelope's ``review_revision``, defaulting to 1."""
    raw = envelope.get("review_revision", 1)
    try:
        revision = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1
    return revision if revision >= 1 else 1


def _validated_decision_values(view: GateView, raw: Any) -> dict[str, Any] | None:
    """Validate saved drafts against frozen definitions.

    Malformed state never selects an undeclared choice or grants memory
    consent: unknown ids and invalid values are dropped.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return {}
    if not view.decisions:
        return {}
    by_id = {str(item.get("id", "")): item for item in view.decisions}
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        definition = by_id.get(str(key))
        if definition is None:
            continue
        kind = str(definition.get("kind", ""))
        if kind == "choice":
            allowed = {
                str(choice.get("key", ""))
                for choice in (definition.get("choices", []) or [])
                if isinstance(choice, dict)
            }
            if str(value) in allowed:
                cleaned[str(key)] = str(value)
        elif isinstance(value, bool):
            cleaned[str(key)] = value
    return cleaned


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
        displayed_revision=view.review_revision if view.decisions else None,
        decision_values=None,
        open_choice_index=None,
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
    input_option_ids, input_field_index, input_values, input_feedback_requested = (
        _load_input_block(view, raw)
    )
    displayed_revision, decision_values, open_choice_index = _load_decision_block(
        view, raw
    )
    source_message_id = _optional_int(raw.get("source_message_id"))
    raw_source_chat = raw.get("source_chat_id")
    source_chat_id = str(raw_source_chat) if raw_source_chat is not None else None
    submitted_revision = _optional_int(raw.get("submitted_revision"))
    submitted_values = _validated_decision_values(view, raw.get("submitted_values"))
    if raw.get("submitted_values") is None:
        submitted_values = None
        submitted_revision = _optional_int(raw.get("submitted_revision"))
    return GateProgress(
        selected_option_ids=selected_ids,
        expanded_branch_index=expanded,
        active_message_id=saved_message_id,
        chat_id=saved_chat_id,
        input_option_ids=input_option_ids,
        input_field_index=input_field_index,
        input_values=input_values,
        input_feedback_requested=input_feedback_requested,
        displayed_revision=displayed_revision,
        decision_values=decision_values,
        open_choice_index=open_choice_index,
        source_message_id=source_message_id,
        source_chat_id=source_chat_id,
        submitted_revision=submitted_revision,
        submitted_values=submitted_values,
    )


def _load_decision_block(
    view: GateView, raw: Mapping[str, Any]
) -> tuple[int | None, dict[str, Any] | None, int | None]:
    """Recover the decision draft block, keeping a stale revision.

    A saved revision is kept until the reviewer explicitly refreshes it.
    Malformed values never select an undeclared choice or grant consent.
    """
    if not view.decisions:
        return None, None, None
    raw_revision = _optional_int(raw.get("displayed_revision"))
    # Keep the stale saved revision; a missing value starts at the envelope.
    displayed = raw_revision if raw_revision is not None else view.review_revision
    cleaned = _validated_decision_values(view, raw.get("decision_values"))
    decision_values = dict(cleaned) if cleaned else None
    raw_open = _optional_int(raw.get("open_choice_index"))
    open_index: int | None = None
    if raw_open is not None and 0 <= raw_open < len(view.decisions):
        kind = str(view.decisions[raw_open].get("kind", ""))
        if kind == "choice":
            open_index = raw_open
    return displayed, decision_values, open_index


def _load_input_block(
    view: GateView, raw: Mapping[str, Any]
) -> tuple[tuple[str, ...], int | None, dict[str, Any] | None, bool]:
    """Recover the declared-input block, resetting to empty on any corruption."""
    empty: tuple[tuple[str, ...], int | None, dict[str, Any] | None, bool] = (
        (),
        None,
        None,
        False,
    )
    raw_option_ids = raw.get("input_option_ids")
    if not (
        isinstance(raw_option_ids, list)
        and raw_option_ids
        and all(isinstance(item, str) for item in raw_option_ids)
    ):
        return empty
    declared_ids = {option.id for option in view.options}
    if not all(option_id in declared_ids for option_id in raw_option_ids):
        return empty
    raw_index = raw.get("input_field_index")
    if not isinstance(raw_index, int) or isinstance(raw_index, bool) or raw_index < 0:
        return empty
    raw_values = raw.get("input_values")
    if raw_values is not None and not (
        isinstance(raw_values, dict) and all(isinstance(key, str) for key in raw_values)
    ):
        return empty
    return (
        tuple(raw_option_ids),
        raw_index,
        dict(raw_values) if isinstance(raw_values, dict) else None,
        raw.get("input_feedback_requested") is True,
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
        "input_option_ids": list(progress.input_option_ids),
        "input_field_index": progress.input_field_index,
        "input_values": progress.input_values,
        "input_feedback_requested": progress.input_feedback_requested,
        "displayed_revision": progress.displayed_revision,
        "decision_values": progress.decision_values,
        "open_choice_index": progress.open_choice_index,
        "source_message_id": progress.source_message_id,
        "source_chat_id": progress.source_chat_id,
        "submitted_revision": progress.submitted_revision,
        "submitted_values": progress.submitted_values,
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
