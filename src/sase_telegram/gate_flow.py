"""Server-side progress for Telegram notification-gate interactions."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sase.notification_gates.hashing import load_and_verify_bundle
from sase.notification_gates.models import GateChoice, GateError, GateFeedbackMode

PROGRESS_FILENAME = "telegram_gate_progress.json"


@dataclass(frozen=True)
class GateView:
    """Verified gate data needed by Telegram formatting and callbacks."""

    bundle_path: Path
    request_id: str
    kind: str
    choices: tuple[GateChoice, ...]


@dataclass(frozen=True)
class GateProgress:
    """Telegram-private terminal-choice and add-on selection state."""

    choice_id: str | None = None
    selected_extra_ids: tuple[str, ...] = ()
    active_message_id: int | None = None
    chat_id: str | None = None


def load_gate_view(
    action_data: Mapping[str, Any], *, expected_kind: str | None = None
) -> GateView:
    """Load and verify the neutral gate referenced by notification action data."""
    raw_bundle = action_data.get("bundle_path")
    if not isinstance(raw_bundle, str) or not raw_bundle.strip():
        raise GateError(
            "missing_gate", "bundle_path", "notification has no neutral gate bundle"
        )
    action_by_kind = {
        "custom": "CustomGate",
        "epic_plan": "EpicApproval",
        "hitl": "HITL",
        "plan": "PlanApproval",
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
            "missing_gate", "bundle_path", "notification has no neutral gate bundle"
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
    raw_choices = envelope.get("choices")
    if not isinstance(raw_choices, list) or not raw_choices:
        raise GateError("invalid_request", "choices", "gate has no terminal choices")
    default_feedback: GateFeedbackMode = (
        "optional" if adapter.kind == "custom" else "disabled"
    )
    choices = tuple(
        GateChoice.from_mapping(raw, index, default_feedback=default_feedback)
        for index, raw in enumerate(raw_choices)
    )
    return GateView(
        bundle_path=bundle_path,
        request_id=str(envelope.get("request_id") or bundle_path.name),
        kind=adapter.kind,
        choices=choices,
    )


def progress_path(view: GateView) -> Path:
    """Return the Telegram-private progress path for a verified gate."""
    return view.bundle_path / PROGRESS_FILENAME


def initial_progress(
    view: GateView,
    *,
    choice_id: str | None = None,
    active_message_id: int | None = None,
    chat_id: str | None = None,
) -> GateProgress:
    """Create progress, selecting the choice's default add-ons when requested."""
    progress = GateProgress(
        active_message_id=active_message_id,
        chat_id=chat_id,
    )
    if choice_id is None:
        return progress
    return select_choice(view, progress, choice_id)


def load_progress(
    view: GateView,
    *,
    default_choice_id: str | None = None,
    active_message_id: int | None = None,
    chat_id: str | None = None,
) -> GateProgress:
    """Load saved progress, recovering safely from stale or malformed state."""
    normalized_default = (
        default_choice_id
        if default_choice_id is not None
        and choice_for_id(view, default_choice_id) is not None
        else None
    )
    path = progress_path(view)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return initial_progress(
            view,
            choice_id=normalized_default,
            active_message_id=active_message_id,
            chat_id=chat_id,
        )
    if not isinstance(raw, dict):
        return initial_progress(
            view,
            choice_id=normalized_default,
            active_message_id=active_message_id,
            chat_id=chat_id,
        )

    saved_message_id = _optional_int(raw.get("active_message_id")) or active_message_id
    saved_chat_id = str(raw["chat_id"]) if raw.get("chat_id") is not None else chat_id
    choice_id = raw.get("choice_id")
    if not isinstance(choice_id, str) or choice_for_id(view, choice_id) is None:
        return initial_progress(
            view,
            choice_id=normalized_default,
            active_message_id=saved_message_id,
            chat_id=saved_chat_id,
        )
    selected = raw.get("selected_extra_ids")
    selected_ids = (
        tuple(str(item) for item in selected)
        if isinstance(selected, list)
        and all(isinstance(item, str) for item in selected)
        else ()
    )
    progress = GateProgress(
        choice_id=choice_id,
        selected_extra_ids=selected_ids,
        active_message_id=saved_message_id,
        chat_id=saved_chat_id,
    )
    choice = choice_for_id(view, choice_id)
    assert choice is not None
    valid_ids = {extra.id for extra in choice.extras}
    filtered = tuple(
        extra.id
        for extra in choice.extras
        if extra.id in selected_ids and extra.id in valid_ids
    )
    return replace(progress, selected_extra_ids=filtered)


def save_progress(view: GateView, progress: GateProgress) -> None:
    """Atomically persist Telegram gate progress next to the gate envelope."""
    path = progress_path(view)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "choice_id": progress.choice_id,
        "selected_extra_ids": list(progress.selected_extra_ids),
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


def choice_for_id(view: GateView, choice_id: str) -> GateChoice | None:
    """Return a verified choice by id."""
    return next((choice for choice in view.choices if choice.id == choice_id), None)


def choice_for_token(view: GateView, token: str) -> GateChoice | None:
    """Resolve a compact ``c<index>`` callback token."""
    index = _token_index(token, "c")
    if index is None or index >= len(view.choices):
        return None
    return view.choices[index]


def select_choice(
    view: GateView, progress: GateProgress, choice_id: str
) -> GateProgress:
    """Select one terminal choice and restore its default add-ons."""
    choice = choice_for_id(view, choice_id)
    if choice is None:
        raise ValueError(f"unknown gate choice: {choice_id}")
    return replace(
        progress,
        choice_id=choice.id,
        selected_extra_ids=tuple(
            extra.id for extra in choice.extras if extra.default_selected
        ),
    )


def toggle_extra(
    view: GateView, progress: GateProgress, token: str
) -> tuple[GateProgress, bool]:
    """Toggle one compact ``x<index>`` add-on and return its new state."""
    if progress.choice_id is None:
        raise ValueError("select a gate choice before toggling add-ons")
    choice = choice_for_id(view, progress.choice_id)
    if choice is None:
        raise ValueError("selected gate choice is unavailable")
    index = _token_index(token, "x")
    if index is None or index >= len(choice.extras):
        raise ValueError("unknown gate add-on")
    target = choice.extras[index].id
    selected = set(progress.selected_extra_ids)
    if target in selected:
        selected.remove(target)
        enabled = False
    else:
        selected.add(target)
        enabled = True
    ordered = tuple(extra.id for extra in choice.extras if extra.id in selected)
    return replace(progress, selected_extra_ids=ordered), enabled


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
