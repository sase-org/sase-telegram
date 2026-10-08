"""Durable inline-keyboard removal retries."""

from __future__ import annotations

import time
from pathlib import Path
from sase_telegram import pending_actions, telegram_client
from sase_telegram.inbound import (
    clear_awaiting_feedback_by_prefix,
    find_shared_handled_transports,
)
from sase_telegram.inbound_handlers.common import _load_json_file, _atomic_write_json

import logging

log = logging.getLogger(__name__)


# Durable retry record for one inline-keyboard removal whose Telegram API
# edit failed after `telegram_client`'s own bounded rate-limit/network
# retries were exhausted -- see `_dismiss_button_with_retry`.
_GATE_KEYBOARD_CLEANUP_DIR = (
    Path.home() / ".sase" / "telegram" / "gate_keyboard_cleanup"
)


def _keyboard_cleanup_retry_path(prefix: str) -> Path:
    return _GATE_KEYBOARD_CLEANUP_DIR / f"{prefix}.json"


def _persist_keyboard_cleanup_pending(
    prefix: str, chat_id: str, message_id: int
) -> None:
    record = {
        "prefix": prefix,
        "chat_id": chat_id,
        "message_id": message_id,
        "created_at": time.time(),
    }
    try:
        _GATE_KEYBOARD_CLEANUP_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(_keyboard_cleanup_retry_path(prefix), record)
    except OSError:
        log.warning(
            "Failed to persist gate keyboard cleanup retry context", exc_info=True
        )


def _clear_keyboard_cleanup_pending(prefix: str) -> None:
    _keyboard_cleanup_retry_path(prefix).unlink(missing_ok=True)


def persist_keyboard_cleanup_pending(
    prefix: str, chat_id: str, message_id: int
) -> None:
    """Public wrapper for durable keyboard-cleanup retries."""
    return _persist_keyboard_cleanup_pending(prefix, chat_id, message_id)


def clear_keyboard_cleanup_pending(prefix: str) -> None:
    """Public wrapper clearing a durable keyboard-cleanup retry."""
    return _clear_keyboard_cleanup_pending(prefix)


def dismiss_button_with_retry(
    prefix: str, chat_id: str | None, message_id: int | None
) -> None:
    """Public wrapper removing one inline keyboard with durable retry."""
    return _dismiss_button_with_retry(prefix, chat_id, message_id)


def _dismiss_button_with_retry(
    prefix: str, chat_id: str | None, message_id: int | None
) -> None:
    """Remove one inline keyboard, retrying durably if the edit itself fails.

    ``telegram_client.edit_message_reply_markup`` already retries transient
    rate-limit/network failures internally; this covers what happens once
    those bounded retries are exhausted (or the failure is not transient).
    A tombstone is written *before* attempting the edit and cleared only on
    success, so a failed edit is retried on a later poll tick
    (``_retry_pending_keyboard_cleanups``) instead of the stale keyboard
    silently outliving all record of needing cleanup -- the local
    pending-action record is safe to drop either way, since a stale tap
    already answers "already handled".
    """
    if message_id is None or chat_id is None:
        return
    _persist_keyboard_cleanup_pending(prefix, chat_id, message_id)
    try:
        telegram_client.edit_message_reply_markup(
            chat_id, message_id, reply_markup=None
        )
    except Exception:
        log.warning("Failed to dismiss gate keyboard; will retry", exc_info=True)
        return
    _clear_keyboard_cleanup_pending(prefix)


def _retry_pending_keyboard_cleanups() -> int:
    """Retry keyboard-removal edits that durably failed on an earlier tick."""
    retried = 0
    try:
        pending_paths = sorted(_GATE_KEYBOARD_CLEANUP_DIR.glob("*.json"))
    except OSError:
        log.warning("Failed to scan gate keyboard cleanup retries", exc_info=True)
        return retried
    for pending_path in pending_paths:
        record = _load_json_file(pending_path)
        if not isinstance(record, dict):
            pending_path.unlink(missing_ok=True)
            continue
        chat_id = record.get("chat_id")
        message_id = record.get("message_id")
        if not isinstance(chat_id, str) or not isinstance(message_id, int):
            pending_path.unlink(missing_ok=True)
            continue
        prefix = str(record.get("prefix") or "")
        # Decision plans retry the receipt edit itself, not merely erasing
        # controls, so a failed card edit is not lost.
        if prefix:
            try:
                if _settle_externally_resolved_decision(prefix, message_id, chat_id):
                    # Handled (or re-persisted) via the receipt path.
                    # Count only when the retry record is gone.
                    if not _keyboard_cleanup_retry_path(prefix).exists():
                        retried += 1
                    continue
            except Exception:
                pass
        try:
            telegram_client.edit_message_reply_markup(
                chat_id, message_id, reply_markup=None
            )
        except Exception:
            log.warning("Retrying gate keyboard cleanup failed again", exc_info=True)
            continue
        pending_path.unlink(missing_ok=True)
        retried += 1
    return retried


def _dismiss_resolved_button(prefix: str, message_id: int, chat_id: str) -> None:
    """Remove a resolved action's inline keyboard and Telegram pending record.

    Cross-surface acceptance (auto-approved, or answered from the TUI/CLI/
    mobile) drives the same durable keyboard-cleanup retry a Telegram-native
    answer does -- see ``_dismiss_button_with_retry``.

    Decision plans use the unified receipt path: the card is edited once
    into its answered receipt with the keyboard removed in the same
    request. Markup cleanup never discards the card before that edit is
    queued; when accepted values are not yet readable the retry context is
    kept for a later tick.
    """
    if _settle_externally_resolved_decision(prefix, message_id, chat_id):
        return
    _dismiss_button_with_retry(prefix, chat_id, message_id)
    pending_actions.remove(prefix)
    clear_awaiting_feedback_by_prefix(prefix)


def _settle_externally_resolved_decision(
    prefix: str, message_id: int, chat_id: str
) -> bool:
    """Edit an externally settled decision card to its receipt.

    Return True when the prefix was a decision plan (handled here),
    False for generic gates (caller dismisses as before).
    """
    try:
        action = pending_actions.get(prefix)
    except Exception:
        return False
    if not isinstance(action, dict):
        return False
    action_data = action.get("action_data")
    if not isinstance(action_data, dict):
        return False
    try:
        from sase_telegram.gate_flow import clear_progress, load_gate_view
    except Exception:
        return False
    try:
        view = load_gate_view(action_data)
    except Exception:
        return False
    if not view.decisions:
        return False
    try:
        from sase_telegram.decision_receipt import (
            authoritative_values,
            decider_surface,
            format_when,
            receipt_text,
        )
    except Exception:
        return False
    try:
        values = authoritative_values(view)
    except Exception:
        values = None
    if values is None:
        # Keep retry context after transient gaps and receiver restarts.
        try:
            _persist_keyboard_cleanup_pending(prefix, chat_id, message_id)
        except Exception:
            pass
        return True
    try:
        import json as _json

        response = None
        try:
            response = _json.loads(
                (view.bundle_path / "response.json").read_text(encoding="utf-8")
            )
            if not isinstance(response, dict):
                response = None
        except (OSError, ValueError):
            response = None
        if isinstance(response, dict):
            decider, surface = decider_surface(response)
            when = format_when(response)
            text = receipt_text(
                view,
                values,
                decider=decider,
                surface=surface,
                when=when,
                response=response,
            )
        else:
            text = receipt_text(view, values)
    except Exception:
        return False
    try:
        telegram_client.edit_message_text(chat_id, message_id, text, reply_markup=None)
    except Exception as exc:
        # Treat already-identical as success; otherwise retry the receipt
        # edit itself, not merely erasing controls.
        try:
            lowered = str(exc).lower()
        except Exception:
            lowered = ""
        if "not modified" in lowered or "identical" in lowered:
            pass
        else:
            log.warning(
                "Failed to edit externally settled decision card", exc_info=True
            )
            try:
                _persist_keyboard_cleanup_pending(prefix, chat_id, message_id)
            except Exception:
                pass
            return True
        # Already-identical falls through to cleanup as success.
    try:
        clear_progress(view)
    except Exception:
        pass
    try:
        pending_actions.remove(prefix)
    except Exception:
        pass
    try:
        clear_awaiting_feedback_by_prefix(prefix)
    except Exception:
        pass
    try:
        _clear_keyboard_cleanup_pending(prefix)
    except Exception:
        pass
    return True


def _find_shared_handled_transports(
    live_prefixes: set[str],
) -> list[tuple[str, int, str]]:
    """Return Telegram messages whose shared pending action is resolved.

    Gated on ``live_prefixes`` (the prefixes that still have a Telegram pending
    record) so a resolved-but-retained shared row is dismissed exactly once
    rather than re-edited on every tick until it goes stale. Shared-store
    failures fall back to direct v2 gate and question completion detection.
    """
    try:
        from sase.notifications.pending_actions import read_pending_action_store
    except Exception:
        return []
    try:
        store = read_pending_action_store(include_legacy=True)
    except Exception:
        log.warning("Failed to read shared pending-action store", exc_info=True)
        return []
    return [
        candidate
        for candidate in find_shared_handled_transports(store, now=time.time())
        if candidate[0] in live_prefixes
    ]
