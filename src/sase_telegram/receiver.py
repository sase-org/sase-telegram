"""Idempotent supervision for the Telegram inbound long-poll receiver.

The receiver itself (the persistent ``getUpdates`` loop) lives in
``scripts/sase_tg_inbound.py`` next to the update handlers it dispatches to.
This module only owns *launching* it: every ~5-second chop tick calls
:func:`ensure_receiver_running`, which is cheap and non-blocking because a
receiver already active for this bot replays the same durable proc row
instead of spawning a second one -- see ``request_fingerprint`` below. SASE's
proc supervisor does not auto-relaunch a crashed supervised proc, so this
per-tick re-arm from the still-ticking chop is what gives the receiver its
restart resilience.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sase.procs import Proc

#: Console-script entry point (see pyproject.toml ``[project.scripts]``),
#: re-invoked with ``--receiver`` so the spawned proc runs the persistent
#: long-poll loop instead of one local-cleanup tick.
_RECEIVER_ARGV = ["sase_chop_tg_inbound", "--receiver"]
_RECEIVER_ORIGIN = "telegram-receiver"


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


def ensure_receiver_running(*, argv: Sequence[str] | None = None) -> Proc:
    """Idempotently ensure one long-poll receiver proc is active for this bot.

    A call while a receiver for this bot is still active replays the same
    durable proc row -- no new process, no duplicate ``getUpdates``
    consumer -- so this is safe (and expected) to call on every chop tick.
    ``argv`` defaults to re-invoking this chop's own entry point with
    ``--receiver``; tests substitute a harmless command so they can exercise
    the real durable single-owner reservation without actually polling
    Telegram.
    """
    from sase.procs import ProcSubmitRequest, submit_proc_request

    identity = receiver_identity()
    return submit_proc_request(
        ProcSubmitRequest(
            argv=list(argv) if argv is not None else _RECEIVER_ARGV,
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


__all__ = ["ensure_receiver_running", "receiver_identity"]
