"""Inbound job entry point: poll Telegram for user actions.

Update handlers live in ``sase_telegram.inbound_handlers``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from sase_telegram import credentials, pending_actions, telegram_client
from sase_telegram.credentials import TelegramCredentialError
from sase_telegram.enabled import is_telegram_enabled
from sase_telegram.receiver import canonical_receiver_argv, ensure_receiver_running
from sase_telegram.receiver_runtime import (
    RuntimeGeneration,
    RuntimeScanError,
    observe_runtime_generation,
    wait_for_settled_generation,
)
from sase_telegram.custom_commands import CustomCommand, load_custom_commands
from sase_telegram.inbound import find_externally_handled, get_last_offset
from sase_telegram.inbound_handlers.keyboard_cleanup import (
    _retry_pending_keyboard_cleanups,
    _dismiss_resolved_button,
    _find_shared_handled_transports,
)
from sase_telegram.inbound_handlers.gate_completions import _send_ready_gate_completions
from sase_telegram.inbound_handlers.update_command import _send_ready_update_completions
from sase_telegram.inbound_handlers.images import _flush_ready_media_groups
from sase_telegram.inbound_handlers.commands import _register_commands_if_needed
from sase_telegram.inbound_handlers.dispatch import (
    _poll_and_dispatch_updates,
    _dispatch_fetched_updates,
)

log = logging.getLogger(__name__)


#: getUpdates long-poll timeout for the persistent receiver (--receiver).
#: python-telegram-bot extends its own HTTP read timeout by this amount
#: automatically (Bot.get_updates), so this does not need a matching
#: telegram_client change.
_RECEIVER_POLL_TIMEOUT_SECONDS = 30


#: Backoff before retrying the receiver's poll loop after an unexpected
#: (non-Telegram-retryable) failure, e.g. a bug in a handler or a store
#: error -- telegram_client.get_updates already retries rate limits/network
#: errors internally before raising here.
_RECEIVER_ERROR_BACKOFF_SECONDS = 5.0


#: Exit code when the receiver refuses to poll because its chat id is not
#: configured (EX_CONFIG). Non-zero so the service host applies its
#: `restart: on-failure` backoff and shows the proc as failing.
_RECEIVER_CHAT_ID_MISSING_EXIT_CODE = 78


#: Exit code when the receiver cannot resolve its bot token (EX_TEMPFAIL).
#: Retryable so the service host applies its `restart: on-failure` backoff
#: until the credential is usable (for example gpg-agent after login)
#: instead of ending the episode as a clean exit.
_RECEIVER_CREDENTIALS_UNAVAILABLE_EXIT_CODE = 75


_RECEIVER_CHAT_ID_MISSING_DEDUP_KEY = "telegram-receiver-chat-id-missing"


_RECEIVER_CHAT_ID_MISSING_SENDER = "telegram"


def _print_inbound_summary(
    *,
    offset: int | None,
    next_offset: int | None,
    update_count: int,
    callback_count: int,
    text_count: int,
    photo_count: int,
    document_count: int,
    unsupported_count: int,
    ready_completions_sent: int,
    pending_actions_cleaned: int,
    reason: str | None = None,
) -> None:
    parts = [
        "tg_inbound:",
        f"updates={update_count}",
        f"callbacks={callback_count}",
        f"text={text_count}",
        f"photos={photo_count}",
        f"documents={document_count}",
        f"unsupported={unsupported_count}",
        f"ready_completions_sent={ready_completions_sent}",
        f"pending_actions_cleaned={pending_actions_cleaned}",
        f"offset={offset if offset is not None else '-'}",
        f"next_offset={next_offset if next_offset is not None else '-'}",
    ]
    if reason:
        parts.append(f"reason={reason}")
    print(" ".join(parts))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sase_job_tg_inbound",
        description="Poll Telegram for user action responses",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process pending updates once and exit (no long-polling)",
    )
    parser.add_argument(
        "--receiver",
        action="store_true",
        help=(
            "Run the persistent long-poll receiver loop (internal: launched "
            "by ensure_receiver_running, not intended for direct/manual use)"
        ),
    )
    parser.add_argument(
        "--context",
        default=None,
        help="Optional AXE job context string",
    )
    return parser.parse_args(argv)


def _run_pre_poll_cleanup(
    custom_commands: dict[str, CustomCommand] | None,
) -> tuple[int, int]:
    """Register commands, clean stale actions, and deliver ready completions.

    Returns ``(stale_pending_count, ready_completions_sent)``.
    """
    _register_commands_if_needed(custom_commands)
    stale_pending = pending_actions.cleanup_stale()
    ready_completions_sent = (
        _send_ready_update_completions() + _send_ready_gate_completions()
    )
    # Retry any inline-keyboard removal whose Telegram API edit durably
    # failed on an earlier tick, independent of this tick's own updates.
    _retry_pending_keyboard_cleanups()
    return len(stale_pending), ready_completions_sent


def _run_post_poll_cleanup() -> int:
    """Dismiss buttons for actions resolved outside Telegram; return count."""
    _flush_ready_media_groups()

    # Clean up pending actions handled by the TUI (remove stale buttons).
    # Legacy filesystem checks run first as the fallback for records that do
    # not yet carry shared transport state.
    handled = find_externally_handled(pending_actions.list_all())
    handled_prefixes: set[str] = set()
    for prefix, message_id, chat_id in handled:
        _dismiss_resolved_button(prefix, message_id, chat_id)
        handled_prefixes.add(prefix)

    # Cross-surface cleanup via the shared pending-action store: covers plans
    # resolved outside the Telegram callback path (auto-approved `%auto` plans,
    # or actions handled in the TUI/CLI/mobile) whose keyboard outlived them.
    # Gated on the live Telegram pending records so each is dismissed once.
    live_prefixes = set(pending_actions.list_all())
    for prefix, message_id, chat_id in _find_shared_handled_transports(live_prefixes):
        if prefix in handled_prefixes:
            continue
        _dismiss_resolved_button(prefix, message_id, chat_id)
        handled_prefixes.add(prefix)

    return len(handled_prefixes)


def _notify_receiver_chat_id_missing() -> None:
    """Upsert one deduped notification about the missing receiver chat id.

    Modeled on :func:`sase_telegram.receiver._notify_receiver_launch_failure`
    so a service-host restart loop cannot pile up duplicates. The outbound
    job still has the chat id, so this notification also reaches Telegram.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from sase.notifications.models import Notification, normalize_notification_tags
    from sase.notifications.store import (
        read_current_notification_snapshot,
        upsert_notification,
    )

    snapshot = read_current_notification_snapshot(include_dismissed=True)
    notifications = getattr(snapshot, "notifications", snapshot)
    if any(
        notification.sender == _RECEIVER_CHAT_ID_MISSING_SENDER
        and notification.dedup_key == _RECEIVER_CHAT_ID_MISSING_DEDUP_KEY
        for notification in notifications
    ):
        return

    timestamp = datetime.now(UTC).isoformat()
    notes = [
        "Telegram inbound receiver cannot start: SASE_TELEGRAM_BOT_CHAT_ID "
        "is not configured.",
        "Set it under service.procs.telegram_receiver.env (the service host "
        "does not inherit AXE routine env).",
    ]
    plus_one_note = "Telegram receiver missing chat id: SASE_TELEGRAM_BOT_CHAT_ID unset"
    upsert_notification(
        Notification(
            id=str(uuid4()),
            timestamp=timestamp,
            sender=_RECEIVER_CHAT_ID_MISSING_SENDER,
            notes=notes,
            files=[],
            tags=normalize_notification_tags(["telegram", "receiver", "error"]),
            dedup_key=_RECEIVER_CHAT_ID_MISSING_DEDUP_KEY,
        ),
        plus_one_note=plus_one_note,
    )


def _run_once(custom_commands: dict[str, CustomCommand] | None) -> int:
    """Poll once (no long-polling) and exit -- diagnostics/tests (--once)."""
    credentials.get_chat_id()
    stale_count, ready_completions_sent = _run_pre_poll_cleanup(custom_commands)
    offset = get_last_offset()
    result = _poll_and_dispatch_updates(
        offset, timeout=0, custom_commands=custom_commands
    )
    handled_count = _run_post_poll_cleanup()
    _print_inbound_summary(
        offset=offset,
        next_offset=result.next_offset,
        update_count=len(result.updates),
        callback_count=result.counts["callback"],
        text_count=result.counts["text"],
        photo_count=result.counts["photo"],
        document_count=result.counts["document"],
        unsupported_count=result.counts["unsupported"],
        ready_completions_sent=ready_completions_sent,
        pending_actions_cleaned=stale_count + handled_count,
        reason=None if result.updates else "no_updates",
    )
    return 0


def _run_chop_tick(custom_commands: dict[str, CustomCommand] | None) -> int:
    """Default (bare) job invocation: local cleanup plus ensure-receiver.

    Network polling now belongs to the persistent ``--receiver`` proc, not
    this short-lived tick, so this keeps the existing five-second cleanup
    cadence (pending-action cleanup, keyboard-removal retries, completion
    delivery) fully independent of however long the receiver's long poll is
    currently waiting. Raises :class:`TelegramCredentialError` exactly as
    the old always-polling ``main`` did (via ``get_updates``), so the
    disabled-credential contract ``scripts.inbound_main`` relies on is
    unchanged.
    """
    credentials.get_bot_token()
    credentials.get_chat_id()
    stale_count, ready_completions_sent = _run_pre_poll_cleanup(custom_commands)
    handled_count = _run_post_poll_cleanup()
    ensure_receiver_running()
    _print_inbound_summary(
        offset=None,
        next_offset=None,
        update_count=0,
        callback_count=0,
        text_count=0,
        photo_count=0,
        document_count=0,
        unsupported_count=0,
        ready_completions_sent=ready_completions_sent,
        pending_actions_cleaned=stale_count + handled_count,
        reason="receiver_ensured",
    )
    return 0


def _run_receiver() -> int:
    """Run the persistent long-poll receiver loop until told to stop.

    Self-terminates when Telegram becomes disabled or its credentials stop
    resolving, rather than requiring an external stop signal: the next
    enabled, credentialed job tick's ``ensure_receiver_running`` call
    re-arms a fresh receiver, so this is sufficient to respond to disabling
    Telegram or rotating/removing its credentials. Reloads custom commands
    each iteration (unlike ``load_custom_commands`` being loaded once by the
    short-lived job tick), since this process can run for a long time.

    The loop is generation-aware: if the installed SASE, plugin, or native
    runtime changes, the process re-execs the canonical
    ``sase_job_tg_inbound --receiver`` argv in place so the supervised
    process slot and ``getUpdates`` consumer stay unique. An update fetched
    across that boundary is not offset-advanced.
    """
    try:
        baseline = observe_runtime_generation()
    except RuntimeScanError:
        log.warning(
            "Telegram receiver runtime is unsettled at start; refreshing when settled"
        )
        return _refresh_receiver_runtime()
    log.info(
        "Starting Telegram long-poll receiver (timeout=%ds generation=%s)",
        _RECEIVER_POLL_TIMEOUT_SECONDS,
        baseline.digest,
    )
    while True:
        if not is_telegram_enabled():
            log.info("Telegram disabled; receiver exiting")
            return 0
        try:
            credentials.get_bot_token()
        except TelegramCredentialError as exc:
            log.warning("Telegram credentials unavailable; receiver retrying: %s", exc)
            return _RECEIVER_CREDENTIALS_UNAVAILABLE_EXIT_CODE
        try:
            credentials.get_chat_id()
        except TelegramCredentialError as exc:
            log.error(
                "Telegram receiver chat id is not configured; set it under "
                "service.procs.telegram_receiver.env: %s",
                exc,
            )
            _notify_receiver_chat_id_missing()
            return _RECEIVER_CHAT_ID_MISSING_EXIT_CODE

        if _runtime_requires_refresh(baseline):
            return _refresh_receiver_runtime()

        custom_commands = load_custom_commands()
        offset = get_last_offset()
        try:
            updates = telegram_client.get_updates(
                offset=offset, timeout=_RECEIVER_POLL_TIMEOUT_SECONDS
            )
        except Exception:
            log.warning("Receiver poll failed; retrying", exc_info=True)
            time.sleep(_RECEIVER_ERROR_BACKOFF_SECONDS)
            continue

        if _runtime_requires_refresh(baseline):
            if updates:
                log.info(
                    "Deferring %d update(s) across receiver runtime refresh",
                    len(updates),
                )
            return _refresh_receiver_runtime()

        result = _dispatch_fetched_updates(
            updates, offset=offset, custom_commands=custom_commands
        )
        if result.updates:
            _print_inbound_summary(
                offset=offset,
                next_offset=result.next_offset,
                update_count=len(result.updates),
                callback_count=result.counts["callback"],
                text_count=result.counts["text"],
                photo_count=result.counts["photo"],
                document_count=result.counts["document"],
                unsupported_count=result.counts["unsupported"],
                ready_completions_sent=0,
                pending_actions_cleaned=0,
                reason=None,
            )


def _runtime_requires_refresh(baseline: RuntimeGeneration) -> bool:
    """Return True when the loaded runtime is stale or cannot be scanned."""
    try:
        current = observe_runtime_generation()
    except RuntimeScanError:
        log.warning("Telegram receiver runtime scan failed; refreshing when settled")
        return True
    if current.digest != baseline.digest:
        log.info(
            "Telegram receiver runtime changed (%s -> %s)",
            baseline.digest,
            current.digest,
        )
        return True
    return False


def _refresh_receiver_runtime() -> int:
    """Wait for a settled generation, then re-exec the canonical receiver."""
    settled = wait_for_settled_generation(scan=observe_runtime_generation)
    log.info(
        "Refreshing Telegram receiver runtime (generation=%s)",
        settled.digest,
    )
    argv = canonical_receiver_argv()
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        log.error(
            "Failed to refresh Telegram receiver runtime: %s",
            " ".join(str(exc).split()),
        )
        return 1
    log.error("Failed to refresh Telegram receiver runtime: exec returned")
    return 1


def main(argv: list[str] | None = None) -> int:
    """Inbound Telegram job entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Suppress noisy httpx request logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = _parse_args(argv)
    if args.receiver:
        # Reloads custom commands itself each iteration; nothing to share.
        return _run_receiver()

    # Load once so registration and every update in this poll share one view.
    custom_commands = load_custom_commands()
    if args.once:
        return _run_once(custom_commands)
    return _run_chop_tick(custom_commands)


if __name__ == "__main__":
    sys.exit(main())
