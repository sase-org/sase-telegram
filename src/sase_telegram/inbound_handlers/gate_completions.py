"""Gate-answer completion delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from sase_telegram import telegram_client
from sase.procs.models import TERMINAL_PROC_STATUSES
from sase.procs.store import get_proc
from sase_telegram.inbound import GATE_COMPLETION_PENDING_DIR
from sase_telegram.inbound_handlers.common import _load_json_file, _shorten_home

import logging

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gate answer completion delivery
#
# `inbound.resolve_gate_response` only submits a gate answer to the shared
# supervised proc and returns; it does not know the outcome. This scans the
# durable records it persisted (`inbound.GATE_COMPLETION_PENDING_DIR`) and
# delivers the real result -- success, a recorded execution error, or the
# proc itself exiting without either -- as a follow-up message once it is
# known, mirroring `update_command._send_ready_update_completions`.
# ---------------------------------------------------------------------------


def _latest_gate_execution_error(
    bundle_path: Path, *, since: float
) -> dict[str, Any] | None:
    """Return the newest ``errors/`` record written at/after *since*, if any."""
    try:
        candidates = sorted((bundle_path / "errors").glob("*.json"))
    except OSError:
        return None
    latest: dict[str, Any] | None = None
    latest_created_at = -1.0
    for candidate in candidates:
        payload = _load_json_file(candidate)
        if not isinstance(payload, dict):
            continue
        created_at = payload.get("created_at_unix")
        if not isinstance(created_at, (int, float)) or created_at < since:
            continue
        if created_at >= latest_created_at:
            latest = payload
            latest_created_at = created_at
    return latest


def _format_gate_response_success(
    record: dict[str, Any], response: dict[str, Any]
) -> str:
    selected = response.get("selected_option_ids")
    options = (
        ", ".join(str(item) for item in selected)
        if isinstance(selected, list) and selected
        else "?"
    )
    return f"✅ Gate {record.get('kind', '?')}/{record.get('request_id', '?')} answered with {options}"


def _format_gate_execution_error(record: dict[str, Any], error: dict[str, Any]) -> str:
    message = error.get("message")
    message = message if isinstance(message, str) and message else "unknown error"
    return f"❌ Gate {record.get('kind', '?')}/{record.get('request_id', '?')} failed: {message}"


def _format_gate_proc_failure(record: dict[str, Any], proc: Any) -> str:
    log_text = _shorten_home(str(proc.log_path)) if proc.log_path else "(unknown)"
    return (
        f"❌ Gate {record.get('kind', '?')}/{record.get('request_id', '?')} failed: "
        f"background proc exited ({proc.status}); log: {log_text}"
    )


def _send_ready_gate_completions() -> int:
    sent_count = 0
    try:
        pending_paths = sorted(GATE_COMPLETION_PENDING_DIR.glob("*.json"))
    except OSError:
        log.warning("Failed to scan Telegram gate completion context", exc_info=True)
        return sent_count

    for pending_path in pending_paths:
        record = _load_json_file(pending_path)
        if not isinstance(record, dict):
            pending_path.unlink(missing_ok=True)
            continue

        chat_id = record.get("chat_id")
        bundle_path_raw = record.get("bundle_path")
        proc_id = record.get("proc_id")
        created_at = record.get("created_at")
        if not (
            isinstance(chat_id, str)
            and isinstance(bundle_path_raw, str)
            and isinstance(proc_id, str)
            and isinstance(created_at, (int, float))
        ):
            pending_path.unlink(missing_ok=True)
            continue

        bundle_path = Path(bundle_path_raw)
        response = _load_json_file(bundle_path / "response.json")
        text: str | None = None
        if isinstance(response, dict):
            text = _format_gate_response_success(record, response)
        else:
            error = _latest_gate_execution_error(bundle_path, since=created_at)
            if error is not None:
                text = _format_gate_execution_error(record, error)
            else:
                proc = _gate_answer_proc_status(proc_id)
                if (
                    proc is not None
                    and proc.status in TERMINAL_PROC_STATUSES
                    and proc.status != "success"
                ):
                    text = _format_gate_proc_failure(record, proc)

        if text is None:
            # Still running, the proc row is not visible yet, or it reported
            # success but `response.json`/an error record has not landed on
            # disk yet -- wait for a later tick rather than guessing.
            continue

        try:
            telegram_client.send_message(chat_id, text)
        except Exception:
            log.warning(
                "Failed to send Telegram gate completion for proc %s",
                proc_id,
                exc_info=True,
            )
            continue
        sent_count += 1
        pending_path.unlink(missing_ok=True)
    return sent_count


def _gate_answer_proc_status(proc_id: str) -> Any | None:
    try:
        return get_proc(proc_id)
    except Exception:
        log.warning("Failed to read Telegram gate answer proc status", exc_info=True)
        return None
