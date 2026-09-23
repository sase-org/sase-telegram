"""Idempotent supervision for the Telegram inbound long-poll receiver.

The receiver itself (the persistent ``getUpdates`` loop) lives in
``scripts/sase_tg_inbound.py``, dispatching to the update handlers in
``inbound_handlers``.
This module only owns *launching* it: every ~5-second job tick calls
:func:`ensure_receiver_running`, which is cheap and non-blocking because a
receiver already active for this bot replays the same durable proc row
instead of spawning a second one -- see ``request_fingerprint`` below. SASE's
proc supervisor does not auto-relaunch a crashed supervised proc, so this
per-tick re-arm from the still-ticking job is what gives the receiver its
restart resilience.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from sase_telegram.executables import resolve_console_script

if TYPE_CHECKING:
    from sase.procs import Proc

#: Console-script entry point (see pyproject.toml ``[project.scripts]``),
#: re-invoked with ``--receiver`` so the spawned proc runs the persistent
#: long-poll loop instead of one local-cleanup tick.
_RECEIVER_SCRIPT = "sase_job_tg_inbound"
_RECEIVER_ORIGIN = "telegram-receiver"
_RECEIVER_LAUNCH_FAILURE_BACKOFF = timedelta(seconds=300)
_RECEIVER_LAUNCH_FAILURE_DEDUP_KEY = "telegram-receiver-launch-failure"
_RECEIVER_LAUNCH_FAILURE_SENDER = "telegram"
_SERVICE_PROC_NAME = "telegram_receiver"


def receiver_identity() -> str:
    """Return a stable identity for the configured bot's receiver.

    Used as both the proc's ``request_fingerprint`` (so repeated ensure
    calls replay the same row) and its ``concurrency_keys`` entry (so two
    receivers for the same bot can never both be active). Falls back to a
    fixed placeholder when the chat id cannot be resolved (e.g. missing
    credentials) so callers still get a deterministic value to reserve
    against instead of raising here -- credential validity is checked by
    the caller before a receiver is ever ensured.
    """
    from sase_telegram import credentials

    try:
        chat_id = credentials.get_chat_id()
    except Exception:
        chat_id = "unconfigured"
    return f"telegram-receiver:{chat_id}"


def ensure_receiver_running(*, argv: Sequence[str] | None = None) -> Proc | None:
    """Idempotently ensure one long-poll receiver proc is active for this bot.

    A call while a receiver for this bot is still active replays the same
    durable proc row -- no new process, no duplicate ``getUpdates``
    consumer -- so this is safe (and expected) to call on every job tick.
    ``argv`` defaults to re-invoking this job's own entry point with
    ``--receiver``; tests substitute a harmless command so they can exercise
    the real durable single-owner reservation without actually polling
    Telegram.
    """
    from sase.procs import ProcSubmitRequest, submit_proc_request

    if _service_host_owns_receiver():
        return None

    identity = receiver_identity()
    if argv is None:
        failed = _newest_launch_failed_receiver(identity)
        if failed is not None:
            _notify_receiver_launch_failure(failed)
            if _in_launch_failure_backoff(failed):
                return failed
        request_argv = _receiver_argv()
    else:
        request_argv = list(argv)
    return submit_proc_request(
        ProcSubmitRequest(
            argv=request_argv,
            label="Telegram inbound long-poll receiver",
            cwd=str(Path.home()),
            origin=_RECEIVER_ORIGIN,
            concurrency_keys=[identity],
            request_fingerprint=identity,
            # The receiver is meant to run indefinitely; it manages its own
            # exit (disabled Telegram, invalid credentials) rather than
            # being bounded by a command or idle timeout.
            timeout_seconds=None,
            idle_timeout_seconds=None,
        )
    )


def canonical_receiver_argv() -> list[str]:
    """Return the canonical argv used to (re)start the long-poll receiver.

    Re-exec uses this same vector so a process that started under the legacy
    ``sase_chop_tg_inbound`` alias refreshes onto ``sase_job_tg_inbound``
    without spawning a second ``getUpdates`` consumer.
    """
    return _receiver_argv()


def _receiver_argv() -> list[str]:
    return [resolve_console_script(_RECEIVER_SCRIPT), "--receiver"]


def _service_host_owns_receiver() -> bool:
    try:
        from sase.service.config import load_service_config

        entry = load_service_config().get(_SERVICE_PROC_NAME)
    except Exception:
        return False
    return bool(entry is not None and entry.available)


def _newest_launch_failed_receiver(fingerprint: str) -> Proc | None:
    from sase.procs import TERMINAL_PROC_STATUSES
    from sase.procs.store import read_proc_snapshot

    snapshot = read_proc_snapshot()
    proc = next(
        (row for row in snapshot.procs if row.request_fingerprint == fingerprint),
        None,
    )
    if proc is None or proc.status not in TERMINAL_PROC_STATUSES:
        return None
    if _termination_reason(proc) != "launch-failure":
        return None
    return proc


def _termination_reason(proc: Proc) -> str | None:
    result = proc.result
    if isinstance(result, dict):
        reason = result.get("termination_reason")
        if isinstance(reason, str) and reason:
            return reason
    return proc.stop_reason


def _in_launch_failure_backoff(proc: Proc, *, now: datetime | None = None) -> bool:
    occurred_at = _proc_failure_time(proc)
    if occurred_at is None:
        return True
    current = now or datetime.now(UTC)
    return current - occurred_at < _RECEIVER_LAUNCH_FAILURE_BACKOFF


def _proc_failure_time(proc: Proc) -> datetime | None:
    for value in (proc.finished_at, proc.created_at):
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _notify_receiver_launch_failure(proc: Proc) -> None:
    from sase.notifications.models import Notification, normalize_notification_tags
    from sase.notifications.store import (
        read_current_notification_snapshot,
        upsert_notification,
    )

    snapshot = read_current_notification_snapshot(include_dismissed=True)
    notifications = getattr(snapshot, "notifications", snapshot)
    if any(
        notification.sender == _RECEIVER_LAUNCH_FAILURE_SENDER
        and notification.dedup_key == _RECEIVER_LAUNCH_FAILURE_DEDUP_KEY
        for notification in notifications
    ):
        return

    timestamp = datetime.now(UTC).isoformat()
    message = _one_line(proc.message or _result_message(proc) or "unknown launch error")
    plus_one_note = f"Proc {proc.proc_id}: {message}"
    log_path = proc.log_path
    upsert_notification(
        Notification(
            id=str(uuid4()),
            timestamp=timestamp,
            sender=_RECEIVER_LAUNCH_FAILURE_SENDER,
            notes=[
                "Telegram inbound receiver cannot start.",
                f"Proc {proc.proc_id}: {message}",
                f"Log: {log_path}",
            ],
            files=[log_path] if log_path else [],
            tags=normalize_notification_tags(["telegram", "receiver", "error"]),
            dedup_key=_RECEIVER_LAUNCH_FAILURE_DEDUP_KEY,
        ),
        plus_one_note=plus_one_note,
    )


def _result_message(proc: Proc) -> str | None:
    result = proc.result
    if isinstance(result, dict):
        message = result.get("message")
        if isinstance(message, str) and message:
            return message
    return None


def _one_line(value: str) -> str:
    return " ".join(value.split())


__all__ = [
    "canonical_receiver_argv",
    "ensure_receiver_running",
    "receiver_identity",
]
