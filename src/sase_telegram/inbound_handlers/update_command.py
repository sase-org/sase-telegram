"""/update worker start and completion delivery."""

from __future__ import annotations

from dataclasses import dataclass
import time
from pathlib import Path
from typing import Any
from sase_telegram import credentials, telegram_client
from sase_telegram.inbound_handlers.common import (
    _load_json_file,
    _atomic_write_json,
    _shorten_home,
)

import logging

log = logging.getLogger(__name__)


_UPDATE_COMPLETION_PENDING_DIR = (
    Path.home() / ".sase" / "telegram" / "update_completions"
)


@dataclass(frozen=True)
class _ChatInstallUnavailableResult:
    status: str = "chat_install_unavailable"
    message: str = (
        "Update not started: installed sase package does not provide "
        "chat update worker support."
    )


def start_chat_install_worker() -> Any:
    try:
        from sase.integrations.chat_install import (
            start_chat_install_worker as worker,
        )
    except ImportError as exc:
        missing_name = getattr(exc, "name", None)
        if missing_name in {
            "sase.integrations",
            "sase.integrations.chat_install",
        } or "start_chat_install_worker" in str(exc):
            return _ChatInstallUnavailableResult()
        raise

    return worker()

    # Unknown commands (e.g. /start) are silently ignored


def _handle_update_command() -> None:
    """Start the detached SASE update worker and acknowledge in Telegram."""
    chat_id = credentials.get_chat_id()
    result = start_chat_install_worker()
    if result.status == "launched":
        _persist_update_completion_pending(result, chat_id)
    telegram_client.send_message(chat_id, _format_update_ack(result))


def _format_update_ack(result: Any) -> str:
    if result.status == "already_running":
        return "Update already running."
    if result.status == "chat_install_unavailable":
        return _ChatInstallUnavailableResult.message
    if result.status == "launched":
        return result.message
    return result.message


def _persist_update_completion_pending(result: Any, chat_id: str) -> None:
    job_id = getattr(result, "job_id", None)
    status_path = getattr(result, "status_path", None)
    if not job_id or status_path is None:
        return

    record = {
        "job_id": str(job_id),
        "chat_id": str(chat_id),
        "status_path": str(status_path),
        "log_path": str(getattr(result, "log_path", "") or ""),
        "created_at": time.time(),
    }
    pending_path = _UPDATE_COMPLETION_PENDING_DIR / f"{job_id}.json"
    try:
        _UPDATE_COMPLETION_PENDING_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(pending_path, record)
    except OSError:
        log.warning(
            "Failed to persist Telegram update completion context", exc_info=True
        )


def _send_ready_update_completions() -> int:
    sent_count = 0
    try:
        pending_paths = sorted(_UPDATE_COMPLETION_PENDING_DIR.glob("*.json"))
    except OSError:
        log.warning("Failed to scan Telegram update completion context", exc_info=True)
        return sent_count

    for pending_path in pending_paths:
        pending = _load_json_file(pending_path)
        if not isinstance(pending, dict):
            pending_path.unlink(missing_ok=True)
            continue

        status_path_raw = pending.get("status_path")
        chat_id = pending.get("chat_id")
        if not isinstance(status_path_raw, str) or not isinstance(chat_id, str):
            pending_path.unlink(missing_ok=True)
            continue

        status_path = Path(status_path_raw).expanduser()
        if not status_path.exists():
            continue

        completion = _load_json_file(status_path)
        if not isinstance(completion, dict):
            continue

        text = _format_update_completion(completion, pending)
        try:
            telegram_client.send_message(chat_id, text)
        except Exception:
            log.warning(
                "Failed to send Telegram update completion for %s",
                pending.get("job_id") or pending_path.name,
                exc_info=True,
            )
            continue
        sent_count += 1
        pending_path.unlink(missing_ok=True)
    return sent_count


def _format_update_completion(
    completion: dict[str, Any], pending: dict[str, Any]
) -> str:
    log_path = completion.get("log_path") or pending.get("log_path") or ""
    log_text = _shorten_home(str(log_path)) if log_path else "(unknown)"
    message = completion.get("message")
    if isinstance(message, str) and message:
        return f"{message.rstrip('.')}; log: {log_text}"

    exit_code = completion.get("exit_code")

    if completion.get("status") == "success" and exit_code == 0:
        return f"Update completed successfully; log: {log_text}"

    if isinstance(exit_code, int):
        return f"Update failed with exit code {exit_code}; log: {log_text}"

    return f"Update failed; log: {log_text}"
