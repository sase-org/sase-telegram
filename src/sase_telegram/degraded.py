"""Persisted degraded-state tracking for the Telegram outbound job and receiver.

A credential or network outage used to log one traceback per failed send or
poll: the Nov 14-15 gpg outage alone produced 408 identical
``TelegramCredentialError`` tracebacks. This module collapses repeated
failures into two named states per component (``outbound`` or ``receiver``):

* ``credential_unavailable`` -- :class:`TelegramCredentialError`;
* ``network_degraded`` -- ``TimedOut`` and ``NetworkError`` (5xx Bad Gateway
  included), but never ``BadRequest``, which subclasses ``NetworkError`` in
  python-telegram-bot.

The outbound job is a fresh process every few seconds and the receiver is
restarted by the service host after a credential exit, so each component's
state lives in a small JSON file under ``~/.sase/telegram/``. Entering a state
logs one WARNING without a traceback and raises one SASE notification;
further failures in the same state are counted and summarized at most once per
:data:`SUMMARY_WINDOW_SECONDS`; recovery logs one INFO line and settles the
notification (a ``+1`` note, then dismissed). Unclassified exceptions are not
handled here, so callers keep their tracebacks.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from telegram.error import BadRequest, NetworkError, TimedOut

from sase_telegram.credentials import TelegramCredentialError

log = logging.getLogger(__name__)

CREDENTIAL_UNAVAILABLE = "credential_unavailable"
NETWORK_DEGRADED = "network_degraded"
DEGRADED_STATES = (CREDENTIAL_UNAVAILABLE, NETWORK_DEGRADED)

OUTBOUND = "outbound"
RECEIVER = "receiver"
_COMPONENT_LABELS = {
    OUTBOUND: "Telegram outbound",
    RECEIVER: "Telegram receiver",
}

#: At most one summary line per window while a component stays degraded.
SUMMARY_WINDOW_SECONDS = 15 * 60

#: Directory holding the per-component ``degraded_<component>.json`` files.
STATE_DIR = Path.home() / ".sase" / "telegram"

NOTICE_SENDER = "telegram"
_NOTICE_DEDUP_PREFIX = "telegram-degraded"
_STATE_VERSION = 1
_MAX_DETAIL_CHARS = 300

Transition = Literal["entered", "changed", "summarized", "suppressed"]


def classify_failure(exc: BaseException) -> str | None:
    """Return the degraded state *exc* indicates, or ``None`` if unexpected.

    ``BadRequest`` is checked before its ``NetworkError`` parent: Telegram
    rejecting a request is not a network outage.
    """
    if isinstance(exc, TelegramCredentialError):
        return CREDENTIAL_UNAVAILABLE
    if isinstance(exc, BadRequest):
        return None
    if isinstance(exc, (TimedOut, NetworkError)):
        return NETWORK_DEGRADED
    return None


def describe_failure(exc: BaseException) -> str:
    """Return a one-line, bounded description of *exc* for logs and notices."""
    message = " ".join(str(exc).split())
    detail = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    if len(detail) > _MAX_DETAIL_CHARS:
        detail = detail[: _MAX_DETAIL_CHARS - 3] + "..."
    return detail


def notice_dedup_key(component: str, state: str) -> str:
    """Return the stable notification dedup key for *component* in *state*."""
    return f"{_NOTICE_DEDUP_PREFIX}:{component}:{state}"


def degraded_notice_component(notification: Any) -> str | None:
    """Return the component a degraded notice belongs to, else ``None``."""
    if getattr(notification, "sender", None) != NOTICE_SENDER:
        return None
    key = getattr(notification, "dedup_key", None)
    if not isinstance(key, str):
        return None
    prefix, _, rest = key.partition(":")
    component, _, state = rest.partition(":")
    if prefix != _NOTICE_DEDUP_PREFIX or state not in DEGRADED_STATES:
        return None
    return component or None


def skip_outbound_delivery(notification: Any) -> bool:
    """Return whether the outbound job must not deliver *notification*.

    The outbound never delivers its own degraded notices: it cannot while it
    is degraded, and recovery settles them before delivery works again. A
    receiver notice is delivered while it is still open and skipped once
    recovery has settled (dismissed) it.
    """
    component = degraded_notice_component(notification)
    if component is None:
        return False
    if component == OUTBOUND:
        return True
    return bool(getattr(notification, "dismissed", False))


def format_duration(seconds: float) -> str:
    """Return a compact human duration such as ``45s``, ``12m 30s``, ``3h 05m``."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m {secs:02d}s" if secs else f"{minutes}m"
    return f"{secs}s"


@dataclass(frozen=True)
class DegradedRecord:
    """One component's persisted degraded state."""

    state: str
    entered_at: float
    last_seen: float
    count: int
    last_summary_at: float
    suppressed: int
    detail: str
    notice_id: str | None = None

    def to_json(self, component: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"version": _STATE_VERSION, "component": component}
        payload.update(asdict(self))
        for key in ("entered_at", "last_seen", "last_summary_at"):
            payload[key] = _iso(payload[key])
        return payload

    @classmethod
    def from_json(cls, payload: Any) -> DegradedRecord | None:
        if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
            return None
        try:
            state = payload["state"]
            if state not in DEGRADED_STATES:
                return None
            notice_id = payload.get("notice_id")
            return cls(
                state=state,
                entered_at=_epoch(payload["entered_at"]),
                last_seen=_epoch(payload["last_seen"]),
                count=int(payload["count"]),
                last_summary_at=_epoch(payload["last_summary_at"]),
                suppressed=int(payload["suppressed"]),
                detail=str(payload.get("detail", "")),
                notice_id=notice_id if isinstance(notice_id, str) else None,
            )
        except (KeyError, TypeError, ValueError):
            return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


class DegradedTracker:
    """Track one component's degraded state across short-lived processes."""

    def __init__(self, component: str) -> None:
        if component not in _COMPONENT_LABELS:
            raise ValueError(f"unknown Telegram component: {component!r}")
        self.component = component
        self.label = _COMPONENT_LABELS[component]

    @property
    def path(self) -> Path:
        return STATE_DIR / f"degraded_{self.component}.json"

    def current(self) -> DegradedRecord | None:
        """Return the persisted state, or ``None`` when healthy or unreadable."""
        return self._load()

    def record_failure(
        self,
        state: str,
        exc: BaseException | None = None,
        *,
        detail: str | None = None,
        now: float | None = None,
    ) -> Transition:
        """Count one failure in *state*; log and notify only on transitions.

        Returns ``"entered"`` (was healthy), ``"changed"`` (was in another
        degraded state), ``"summarized"`` (a summary line was due), or
        ``"suppressed"`` (counted silently).
        """
        if state not in DEGRADED_STATES:
            raise ValueError(f"unknown degraded state: {state!r}")
        observed = time.time() if now is None else now
        if detail is None:
            detail = describe_failure(exc) if exc is not None else state
        with self._locked():
            previous = self._load()
            if previous is not None and previous.state == state:
                record, transition = self._continue(previous, detail, observed)
            else:
                record = DegradedRecord(
                    state=state,
                    entered_at=observed,
                    last_seen=observed,
                    count=1,
                    last_summary_at=observed,
                    suppressed=0,
                    detail=detail,
                )
                transition = "entered" if previous is None else "changed"
                self._log_transition(previous, record)
                if previous is not None:
                    self._settle(
                        previous,
                        f"changed to {state} after "
                        f"{format_duration(observed - previous.entered_at)} "
                        f"({_failures(previous.count)})",
                    )
                record = replace(record, notice_id=self._raise(record))
            self._save(record)
        return transition

    def record_success(
        self,
        *,
        states: Collection[str] | None = None,
        now: float | None = None,
    ) -> DegradedRecord | None:
        """Clear the degraded state after a successful Telegram operation.

        *states* limits which states this success proves recovered (a token
        lookup proves credentials, not the network); ``None`` clears any
        state. Returns the cleared record, or ``None`` when nothing changed.
        """
        if not self.path.exists():
            return None
        observed = time.time() if now is None else now
        with self._locked():
            previous = self._load()
            if previous is None:
                self._delete()
                return None
            if states is not None and previous.state not in states:
                return None
            duration = format_duration(observed - previous.entered_at)
            log.info(
                "%s recovered from %s after %s (%s)",
                self.label,
                previous.state,
                duration,
                _failures(previous.count),
            )
            self._settle(
                previous,
                f"recovered after {duration} ({_failures(previous.count)})",
            )
            self._delete()
        return previous

    def _continue(
        self, previous: DegradedRecord, detail: str, observed: float
    ) -> tuple[DegradedRecord, Transition]:
        record = replace(
            previous,
            last_seen=observed,
            count=previous.count + 1,
            suppressed=previous.suppressed + 1,
            detail=detail,
        )
        if observed - previous.last_summary_at < SUMMARY_WINDOW_SECONDS:
            return record, "suppressed"
        log.warning(
            "%s still %s after %s: %s since the last report, %s in total; last: %s",
            self.label,
            record.state,
            format_duration(observed - record.entered_at),
            _failures(record.suppressed),
            _failures(record.count),
            detail,
        )
        return replace(record, last_summary_at=observed, suppressed=0), "summarized"

    def _log_transition(
        self, previous: DegradedRecord | None, record: DegradedRecord
    ) -> None:
        window = format_duration(SUMMARY_WINDOW_SECONDS)
        if previous is None:
            log.warning(
                "%s entered %s: %s (repeats are summarized every %s)",
                self.label,
                record.state,
                record.detail,
                window,
            )
            return
        log.warning(
            "%s changed from %s to %s after %s (%s): %s "
            "(repeats are summarized every %s)",
            self.label,
            previous.state,
            record.state,
            format_duration(record.entered_at - previous.entered_at),
            _failures(previous.count),
            record.detail,
            window,
        )

    def _raise(self, record: DegradedRecord) -> str | None:
        try:
            return _raise_notice(self.component, self.label, record)
        except Exception as exc:
            # One line, not a traceback: when the disk is full this can repeat
            # on every tick because the state file cannot be saved either.
            log.warning(
                "Could not store the %s degraded notice: %s",
                self.label,
                describe_failure(exc),
            )
            return None

    def _settle(self, record: DegradedRecord, note: str) -> None:
        if record.notice_id is None:
            return
        try:
            _settle_notice(record.notice_id, note)
        except Exception as exc:
            log.warning(
                "Could not settle the %s degraded notice: %s",
                self.label,
                describe_failure(exc),
            )

    def _load(self) -> DegradedRecord | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.warning("Could not read %s: %s", self.path, exc)
            return None
        try:
            return DegradedRecord.from_json(json.loads(raw))
        except json.JSONDecodeError:
            return None

    def _save(self, record: DegradedRecord) -> None:
        payload = json.dumps(record.to_json(self.component), sort_keys=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload + "\n")
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            log.warning("Could not write %s: %s", self.path, exc)

    def _delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not remove %s: %s", self.path, exc)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize read-modify-write cycles across processes, best effort."""
        lock_path = self.path.with_suffix(".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
        except OSError:
            yield
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _failures(count: int) -> str:
    return f"{count} failure" if count == 1 else f"{count} failures"


def _state_title(state: str) -> str:
    if state == CREDENTIAL_UNAVAILABLE:
        return "the bot credential is unavailable"
    return "Telegram is unreachable (timeouts or network errors)"


def _raise_notice(component: str, label: str, record: DegradedRecord) -> str:
    """Create (or ``+1`` a still-open) degraded notice; return its row id."""
    from sase.notifications.models import Notification, normalize_notification_tags
    from sase.notifications.store import append_notification, upsert_notification

    notification = Notification(
        id=str(uuid4()),
        timestamp=_iso(record.entered_at),
        sender=NOTICE_SENDER,
        notes=[
            f"{label} is degraded: {_state_title(record.state)} ({record.state}).",
            record.detail,
            f"Repeats are summarized in the log every "
            f"{format_duration(SUMMARY_WINDOW_SECONDS)}; this notice settles "
            "when Telegram recovers.",
        ],
        tags=normalize_notification_tags(["telegram", component, "error"]),
        dedup_key=notice_dedup_key(component, record.state),
    )
    outcome = upsert_notification(
        notification,
        plus_one_note=f"degraded again: {record.detail}",
        plus_one_timestamp=notification.timestamp,
    )
    if getattr(outcome, "action", None) == "skipped_dismissed":
        # The newest row for this key is a settled earlier episode: this
        # transition still gets its own notice.
        append_notification(notification)
        return notification.id
    row_id = getattr(outcome, "id", None)
    return row_id if isinstance(row_id, str) and row_id else notification.id


def _settle_notice(notice_id: str, note: str) -> None:
    """Record why a degraded notice ended, then dismiss it."""
    from sase.notifications.store import append_notification_plus_one, mark_dismissed

    append_notification_plus_one(
        note=note,
        sender=NOTICE_SENDER,
        timestamp=datetime.now(UTC).isoformat(),
        notification_id=notice_id,
    )
    mark_dismissed(notice_id)


__all__ = [
    "CREDENTIAL_UNAVAILABLE",
    "DEGRADED_STATES",
    "NETWORK_DEGRADED",
    "NOTICE_SENDER",
    "OUTBOUND",
    "RECEIVER",
    "STATE_DIR",
    "SUMMARY_WINDOW_SECONDS",
    "DegradedRecord",
    "DegradedTracker",
    "classify_failure",
    "degraded_notice_component",
    "describe_failure",
    "format_duration",
    "notice_dedup_key",
    "skip_outbound_delivery",
]
