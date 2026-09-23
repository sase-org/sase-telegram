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
    """
    _dismiss_button_with_retry(prefix, chat_id, message_id)
    pending_actions.remove(prefix)
    clear_awaiting_feedback_by_prefix(prefix)


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
