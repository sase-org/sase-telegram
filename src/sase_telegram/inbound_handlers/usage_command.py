"""Telegram /usage command and refresh follow-up."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from sase_telegram import credentials, telegram_client
from sase_telegram.agent_format import html_escape
from sase_telegram.callback_data import encode
from sase_telegram.inbound_handlers.common import (
    _answer_callback,
    _atomic_write_json,
    _callback_chat_id,
    _callback_origin_message_id,
    _load_json_file,
    _message_chat_id,
    _send_html_chunks,
)
from sase_telegram.usage_format import build_usage_chunks
from datetime import UTC

log = logging.getLogger(__name__)

_USAGE_REFRESH_PENDING_DIR = Path.home() / ".sase" / "telegram" / "usage_refreshes"

_USAGE_REFRESH_STALE_DELETE_SECONDS = 600.0


def _usage_facade() -> Any:
    try:
        from sase.integrations import usage_windows as facade
        from sase.integrations.usage_windows import (
            USAGE_WINDOWS_REFRESH_TIMEOUT_SECONDS as _USAGE_TIMEOUT,
            live_usage_refresh_operations as _live_ops,
            request_usage_windows_refresh as _request_refresh,
            resolve_usage_provider as _resolve_provider,
            usage_windows_report as _usage_report,
        )

        _ = (
            _USAGE_TIMEOUT,
            _live_ops,
            _request_refresh,
            _resolve_provider,
            _usage_report,
        )
    except ImportError as exc:
        missing = getattr(exc, "name", "") or str(exc)
        if "usage_windows" in str(missing) or "usage_windows" in str(exc):
            raise
        raise
    return facade


def _usage_timezone() -> Any:
    try:
        from sase.core.time import get_timezone

        return get_timezone()
    except Exception:
        from datetime import timezone

        return UTC


def _build_usage_keyboard(scope: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=encode("usage", scope, "refresh"),
                )
            ]
        ]
    )


def _build_refreshing_keyboard(scope: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏳ Refreshing…",
                    callback_data=encode("usage", scope, "busy"),
                )
            ]
        ]
    )


def _record_path(chat_id: str, message_id: int) -> Path:
    return _USAGE_REFRESH_PENDING_DIR / f"{chat_id}_{message_id}.json"


def _pending_record_exists(chat_id: str, message_id: int) -> bool:
    try:
        return _record_path(chat_id, message_id).exists()
    except OSError:
        return False


def _scope_providers(scope: str) -> tuple[str, ...]:
    if scope == "all":
        return ()
    return (scope,)


def _render_report_chunks(
    facade: Any,
    scope: str,
    *,
    now: float,
    status_line: str | None = None,
) -> tuple[list[str], Any]:
    report = facade.usage_windows_report(_scope_providers(scope), now=now)
    tzinfo = _usage_timezone()
    if not getattr(report, "collection_enabled", True):
        text = (
            "📊 Usage tracking is off.\n"
            "Enable it with <code>llm_provider.usage_metrics.enabled: true</code>."
        )
        return [text], report
    providers = tuple(getattr(report, "providers", ()) or ())
    if not providers and not getattr(report, "configured_providers", ()):
        return ["📊 No configured providers report usage windows."], report
    chunks = build_usage_chunks(
        report, now=now, tzinfo=tzinfo, status_line=status_line, scope=scope
    )
    return chunks, report


def _handle_usage_command(args: str = "", message: Any | None = None) -> None:
    """Handle /usage and /usage <provider>."""
    chat_id = _message_chat_id(message) or credentials.get_chat_id()
    try:
        facade = _usage_facade()
    except ImportError:
        telegram_client.send_message(
            chat_id, "Installed sase doesn't support /usage yet — run /update."
        )
        return
    raw = args.strip().split(None, 1)[0] if args.strip() else ""
    scope = "all"
    if raw:
        try:
            resolved = facade.resolve_usage_provider(raw)
        except Exception:
            log.warning("Failed to resolve usage provider %r", raw, exc_info=True)
            resolved = None
        if resolved is None:
            try:
                configured = tuple(
                    getattr(
                        facade.usage_windows_report(now=time.time()),
                        "configured_providers",
                        (),
                    )
                )
            except Exception:
                configured = ()
            choices = ", ".join(configured) if configured else "none"
            telegram_client.send_message(
                chat_id,
                f"🤔 Unknown provider “{html_escape(raw)}”. Try: {html_escape(choices)}",
                parse_mode="HTML",
            )
            return
        scope = resolved
    try:
        chunks, _report = _render_report_chunks(facade, scope, now=time.time())
    except (OSError, RuntimeError, AttributeError, ImportError, ValueError) as exc:
        try:
            from sase.llm_provider.usage.errors import ProviderUsageStateError

            state_error = isinstance(exc, ProviderUsageStateError)
        except Exception:
            state_error = False
        if state_error or isinstance(exc, OSError):
            telegram_client.send_message(
                chat_id,
                f"⚠️ Couldn't read usage data: {html_escape(str(exc))}",
                parse_mode="HTML",
                reply_markup=_build_usage_keyboard(scope),
            )
            return
        log.exception("Failed to build /usage view")
        telegram_client.send_message(chat_id, "Failed to build /usage view.")
        return
    except Exception:
        log.exception("Failed to build /usage view")
        telegram_client.send_message(chat_id, "Failed to build /usage view.")
        return
    _send_html_chunks(chat_id, chunks, reply_markup=_build_usage_keyboard(scope))


def _handle_usage_callback(callback_query: Any, scope: str, choice: str) -> None:
    """Handle usage refresh, busy, and all-view callbacks."""
    if choice == "busy":
        _answer_callback(callback_query, "Still refreshing…")
        return
    if choice == "all":
        scope = "all"
        choice = "refresh-view"
    if choice not in {"refresh", "refresh-view"}:
        _answer_callback(callback_query, "Invalid usage action")
        return
    if choice == "refresh-view":
        try:
            facade = _usage_facade()
        except ImportError:
            _answer_callback(
                callback_query, "Installed sase doesn't support /usage yet"
            )
            return
        chat_id = _callback_chat_id(callback_query, None)
        if chat_id is None:
            _answer_callback(callback_query, "Could not resolve Telegram chat")
            return
        try:
            chunks, _report = _render_report_chunks(facade, scope, now=time.time())
        except Exception:
            log.exception("Failed to build /usage view")
            _answer_callback(callback_query, "Failed to build /usage view")
            return
        message_id = _callback_origin_message_id(callback_query, None)
        if message_id is not None and len(chunks) == 1:
            try:
                telegram_client.edit_message_text(
                    chat_id,
                    message_id,
                    chunks[0],
                    reply_markup=_build_usage_keyboard(scope),
                    parse_mode="HTML",
                )
            except Exception:
                log.warning("Failed to edit /usage message", exc_info=True)
                _send_html_chunks(
                    chat_id, chunks, reply_markup=_build_usage_keyboard(scope)
                )
        else:
            _send_html_chunks(
                chat_id, chunks, reply_markup=_build_usage_keyboard(scope)
            )
        _answer_callback(callback_query, "Refreshed")
        return
    try:
        facade = _usage_facade()
    except ImportError:
        _answer_callback(callback_query, "Installed sase doesn't support /usage yet")
        return
    chat_id = _callback_chat_id(callback_query, None)
    message_id = _callback_origin_message_id(callback_query, None)
    if chat_id is None or message_id is None:
        _answer_callback(callback_query, "Could not resolve Telegram chat")
        return
    if _pending_record_exists(chat_id, message_id):
        _answer_callback(callback_query, "Already refreshing…")
        return
    try:
        refresh = facade.request_usage_windows_refresh(_scope_providers(scope))
    except Exception:
        log.exception("Failed to start usage refresh")
        _answer_callback(callback_query, "Failed to start usage refresh")
        return
    toast = str(getattr(refresh, "summary", "") or "Refreshing usage")[:200]
    _answer_callback(callback_query, toast or "Refreshing usage")
    operation_ids = tuple(getattr(refresh, "operation_ids", ()) or ())
    started_providers = tuple(getattr(refresh, "providers", ()) or ())
    if not operation_ids:
        try:
            chunks, _report = _render_report_chunks(
                facade, scope, now=time.time(), status_line=toast
            )
        except Exception:
            log.exception("Failed to build /usage view")
            return
        keyboard = _build_usage_keyboard(scope)
        if len(chunks) == 1:
            try:
                telegram_client.edit_message_text(
                    chat_id,
                    message_id,
                    chunks[0],
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            except Exception:
                log.warning("Failed to edit /usage message", exc_info=True)
        else:
            _send_html_chunks(chat_id, chunks, reply_markup=keyboard)
        return
    providers_label = ", ".join(started_providers) if started_providers else scope
    try:
        chunks, _report = _render_report_chunks(
            facade,
            scope,
            now=time.time(),
            status_line=f"⏳ Refreshing {providers_label}…",
        )
    except Exception:
        log.exception("Failed to build /usage view")
        return
    try:
        if len(chunks) == 1:
            telegram_client.edit_message_text(
                chat_id,
                message_id,
                chunks[0],
                reply_markup=_build_refreshing_keyboard(scope),
                parse_mode="HTML",
            )
        else:
            _send_html_chunks(
                chat_id, chunks, reply_markup=_build_refreshing_keyboard(scope)
            )
            return
    except Exception:
        log.warning("Failed to edit /usage refreshing state", exc_info=True)
        return
    submitted_at = time.time()
    try:
        timeout = float(getattr(facade, "USAGE_WINDOWS_REFRESH_TIMEOUT_SECONDS", 75.0))
    except Exception:
        timeout = 75.0
    record = {
        "version": 1,
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "scope": str(scope),
        "operation_ids": list(operation_ids),
        "providers": list(started_providers),
        "submitted_at": float(submitted_at),
        "deadline_at": float(submitted_at) + float(timeout),
    }
    try:
        _USAGE_REFRESH_PENDING_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(_record_path(chat_id, message_id), record)
    except OSError:
        log.warning("Failed to persist usage refresh record", exc_info=True)


def _finish_ready_usage_refreshes() -> int:
    """Deliver completed usage refreshes by editing the original message."""
    sent_count = 0
    try:
        pending_paths = sorted(_USAGE_REFRESH_PENDING_DIR.glob("*.json"))
    except OSError:
        log.warning("Failed to scan usage refresh records", exc_info=True)
        return sent_count
    try:
        facade = _usage_facade()
    except ImportError:
        return sent_count
    now = time.time()
    for pending_path in pending_paths:
        record = _load_json_file(pending_path)
        if not isinstance(record, dict):
            log.warning("Deleting malformed usage refresh record: %s", pending_path)
            try:
                pending_path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        try:
            version = record.get("version")
            chat_id = record.get("chat_id")
            message_id = record.get("message_id")
            scope = record.get("scope")
            operation_ids = record.get("operation_ids")
            providers = record.get("providers")
            submitted_at = record.get("submitted_at")
            deadline_at = record.get("deadline_at")
            valid = (
                version == 1
                and isinstance(chat_id, str)
                and isinstance(message_id, int)
                and isinstance(scope, str)
                and isinstance(operation_ids, list)
                and all(isinstance(item, str) for item in operation_ids)
                and isinstance(providers, list)
                and isinstance(submitted_at, (int, float))
                and isinstance(deadline_at, (int, float))
            )
        except Exception:
            valid = False
        if not valid:
            log.warning("Deleting malformed usage refresh record: %s", pending_path)
            try:
                pending_path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        assert isinstance(record, dict)
        deadline = float(record["deadline_at"])
        if now > deadline + _USAGE_REFRESH_STALE_DELETE_SECONDS:
            log.warning("Deleting expired usage refresh record: %s", pending_path)
            try:
                pending_path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        try:
            live = facade.live_usage_refresh_operations(tuple(record["operation_ids"]))
            live = frozenset(str(item) for item in live)
        except Exception:
            log.warning("Failed to poll usage refresh liveness", exc_info=True)
            continue
        if live and now < deadline:
            continue
        elapsed = max(int(now - float(record["submitted_at"])), 0)
        if live:
            still = ", ".join(sorted(str(p) for p in record["providers"]))
            status_line = f"⚠️ Refresh still running for {still} after {elapsed}s — showing latest data"
        else:
            status_line = "✅ Refreshed"
        try:
            chunks, _report = _render_report_chunks(
                facade, str(record["scope"]), now=now, status_line=status_line
            )
        except Exception:
            log.exception("Failed to build refreshed /usage view")
            continue
        keyboard = _build_usage_keyboard(str(record["scope"]))
        try:
            if len(chunks) == 1:
                telegram_client.edit_message_text(
                    str(record["chat_id"]),
                    int(record["message_id"]),
                    chunks[0],
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            else:
                _send_html_chunks(str(record["chat_id"]), chunks, reply_markup=keyboard)
        except Exception:
            log.warning(
                "Failed to deliver usage refresh for %s",
                pending_path.name,
                exc_info=True,
            )
            continue
        try:
            pending_path.unlink(missing_ok=True)
        except OSError:
            pass
        sent_count += 1
    return sent_count
