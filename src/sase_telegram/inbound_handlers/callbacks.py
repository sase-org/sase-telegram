"""Callback-query router and race guard."""

from __future__ import annotations

import time
from typing import Any
from sase_telegram import telegram_client
from sase_telegram.callback_data import decode
from sase_telegram.inbound_handlers.common import _answer_resolved_callback
from sase_telegram.inbound_handlers.questions import _handle_question_callback
from sase_telegram.inbound_handlers.beads import _handle_bead_callback
from sase_telegram.inbound_handlers.agent_actions import (
    _KILL_SELECTION_CHOICE,
    _handle_kill_from_callback,
    _handle_kill_selection_from_callback,
    _handle_retry_from_callback,
)
from sase_telegram.inbound_handlers.agent_list import _handle_list_callback
from sase_telegram.inbound_handlers.agent_show import _handle_show_callback
from sase_telegram.inbound_handlers.gate_callbacks import _handle_gate_callback

import logging

log = logging.getLogger(__name__)


def _shared_action_resolution(prefix: str) -> str | None:
    """Return the shared-store resolution for a notification prefix.

    Returns ``"already_handled"`` or ``"stale"`` when the shared host store
    (with migrated records merged in) reports the action is no longer actionable,
    or ``None`` when the store has no opinion or is unavailable.
    """
    try:
        from sase.notifications.pending_actions import (
            read_pending_action_store,
            resolve_prefix,
        )
    except Exception:
        return None
    try:
        identity = resolve_prefix(prefix)
        if identity.resolution in {"missing", "ambiguous_prefix", "duplicate_full_id"}:
            return None
        store = read_pending_action_store(include_legacy=True)
    except Exception:
        log.warning("Failed to resolve shared pending-action state", exc_info=True)
        return None

    entry = next(
        (
            value
            for value in store.get("actions", {}).values()
            if isinstance(value, dict)
            and value.get("notification_id") == identity.notification_id
        ),
        None,
    )
    if not isinstance(entry, dict):
        return None
    state = entry.get("state")
    if state in {"already_handled", "stale"}:
        return str(state)
    deadline = entry.get("stale_deadline_unix")
    if isinstance(deadline, (int, float)) and deadline <= time.time():
        return "stale"
    return None


def _resolve_callback_already_handled(
    data_str: str, pending: dict[str, Any]
) -> tuple[str, dict[str, Any], str] | None:
    """Return ``(prefix, action, resolution)`` when a callback is already resolved.

    Only notification-backed callbacks (gate/question) with a live pending
    record are guarded — agent management callbacks and unknown actions fall
    through to the existing handlers.
    """
    try:
        cb = decode(data_str)
    except ValueError:
        return None
    if cb.action_type not in {"gate", "question"}:
        return None
    action = pending.get(cb.notif_id_prefix)
    if action is None:
        return None
    resolution = _shared_action_resolution(cb.notif_id_prefix)
    if resolution is None:
        return None
    return cb.notif_id_prefix, action, resolution


def _handle_callback(callback_query: Any, pending: dict[str, Any]) -> None:
    """Handle an inline keyboard button press."""
    data_str: str = callback_query.data

    # Handle kill/retry callbacks (agent management, not notification-based)
    try:
        cb = decode(data_str)
        if cb.action_type == "kill":
            if cb.choice == _KILL_SELECTION_CHOICE:
                _handle_kill_selection_from_callback(callback_query, cb.notif_id_prefix)
            else:
                _handle_kill_from_callback(callback_query, cb.notif_id_prefix)
            return
        if cb.action_type == "retry":
            _handle_retry_from_callback(callback_query, cb.notif_id_prefix)
            return
        if cb.action_type == "bead":
            _handle_bead_callback(callback_query, cb.notif_id_prefix)
            return
        if cb.action_type == "list":
            _handle_list_callback(callback_query, cb.notif_id_prefix, cb.choice)
            return
        if cb.action_type == "show":
            _handle_show_callback(callback_query, cb.notif_id_prefix, cb.choice)
            return
    except ValueError:
        pass

    # Race guard: if this notification action was resolved outside Telegram
    # (auto-approved plan, or handled in the TUI/CLI/mobile), do not start a
    # feedback flow or write a competing response. Dismiss the defunct keyboard
    # and answer the user deterministically.
    guard = _resolve_callback_already_handled(data_str, pending)
    if guard is not None:
        guard_prefix, guard_action, guard_resolution = guard
        _answer_resolved_callback(
            callback_query, guard_action, guard_prefix, guard_resolution
        )
        return

    try:
        decoded = decode(data_str)
        if decoded.action_type == "question":
            _handle_question_callback(callback_query, pending)
            return
        if decoded.action_type == "gate":
            _handle_gate_callback(callback_query, pending)
            return
    except ValueError:
        telegram_client.answer_callback_query(callback_query.id, "Invalid callback")
        return
    telegram_client.answer_callback_query(
        callback_query.id, "This action has already been handled"
    )
