"""Gate-answer completion delivery."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from sase_telegram import telegram_client
from sase.procs.models import TERMINAL_PROC_STATUSES
from sase.procs.store import get_proc
from sase_telegram.inbound import GATE_COMPLETION_PENDING_DIR
from sase_telegram.inbound_handlers.common import _load_json_file, _shorten_home

if TYPE_CHECKING:
    from sase_telegram.gate_flow import GateView

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
        # One decision-plan receipt path for Telegram and external
        # settlement. Persist the edit before removing pending/progress.
        if _is_decision_bundle(bundle_path):
            if _settle_decision_receipt(
                pending_path, record, bundle_path, chat_id, proc_id, created_at
            ):
                sent_count += 1
            continue
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


def _is_decision_bundle(bundle_path: Path) -> bool:
    request = _load_json_file(bundle_path / "request.json")
    if not isinstance(request, dict):
        return False
    payload = request.get("payload")
    return isinstance(payload, dict) and isinstance(payload.get("decisions"), list)


def _settle_decision_receipt(
    pending_path: Path,
    record: dict[str, Any],
    bundle_path: Path,
    chat_id: str,
    proc_id: str,
    created_at: float,
) -> bool:
    """Edit one review card into its answered receipt. Return True when done."""
    from sase_telegram.decision_receipt import (
        authoritative_values as _auth_values,
        decider_surface,
        format_when,
        launch_failed_text,
        receipt_text,
    )
    from sase_telegram.gate_flow import GateView

    from sase.procs.models import TERMINAL_PROC_STATUSES as _TERMINAL

    response = _load_json_file(bundle_path / "response.json")
    # Fast acceptance before response.json/stamped plan: disable controls
    # and retain the receipt job until the accepted vector is readable.
    if not isinstance(response, dict):
        error = _latest_gate_execution_error(bundle_path, since=created_at)
        if error is not None and _is_stale_error(error):
            _restore_refresh_for_stale(bundle_path, chat_id, record)
            pending_path.unlink(missing_ok=True)
            return True
        proc = _gate_answer_proc_status(proc_id)
        try:
            terminal = proc is not None and proc.status in _TERMINAL
        except Exception:
            terminal = False
        if terminal:
            # Terminal proc with no durable response/error: actionable
            # report instead of spinning forever.
            try:
                status = getattr(proc, "status", "?") if proc is not None else "?"
                telegram_client.send_message(
                    chat_id,
                    f"❌ Gate {record.get('kind', '?')}/{record.get('request_id', '?')} "
                    f"finished without a recorded answer (proc {proc_id} {status}); "
                    "retry from the review card.",
                )
            except Exception:
                log.warning(
                    "Failed to send missing-response report for proc %s",
                    proc_id,
                    exc_info=True,
                )
                return False
            _clear_decision_progress(bundle_path)
            pending_path.unlink(missing_ok=True)
            return True
        # Still running or not yet visible: keep pending, never claim failure.
        return False
    view = _minimal_view_for_receipt(bundle_path)
    if view is None:
        return False
    values = _auth_values(view)
    if values is None:
        return False
    # One truthful receipt path: durable decider/surface/time plus the
    # true approval verdict; reject/feedback render their own headers.
    decider, surface = decider_surface(response)
    when = format_when(response)
    base = receipt_text(
        view, values, decider=decider, surface=surface, when=when, response=response
    )
    # Acceptance and implementation status stay distinct: a response can
    # exist before a successor-launch failure. Inspect post-response
    # failures as well as responses. Only recorded launch-failure evidence
    # adds the coder-start claim; running procs never do.
    error = _latest_gate_execution_error(bundle_path, since=created_at)
    proc = _gate_answer_proc_status(proc_id)
    launch_failed = _is_launch_failure(error, proc, response)
    text = launch_failed_text(base) if launch_failed else base
    # Repeat polls produce one logical edit; freeze text and treat
    # already-identical as success, retrying only unfinished work.
    if (
        isinstance(record.get("receipt_text"), str)
        and record.get("receipt_text") != text
        and not launch_failed
    ):
        text = str(record["receipt_text"])
    else:
        record["receipt_text"] = text
        try:
            pending_path.write_text(__import__("json").dumps(record, indent=2))
        except OSError:
            pass
    message_id = _review_message_id(bundle_path, record)
    if message_id is None:
        try:
            telegram_client.send_message(chat_id, text)
        except Exception:
            log.warning(
                "Failed to send decision receipt for proc %s", proc_id, exc_info=True
            )
            return False
        _clear_decision_progress(bundle_path)
        pending_path.unlink(missing_ok=True)
        return True
    try:
        telegram_client.edit_message_text(chat_id, message_id, text, reply_markup=None)
    except Exception as exc:
        if "not modified" in str(exc).lower() or "identical" in str(exc).lower():
            _clear_decision_progress(bundle_path)
            pending_path.unlink(missing_ok=True)
            return True
        log.warning(
            "Failed to edit decision receipt for proc %s", proc_id, exc_info=True
        )
        return False
    # The completion reply carries the full summary sentence.
    try:
        telegram_client.send_message(chat_id, text)
    except Exception:
        log.warning(
            "Failed to send decision completion for proc %s", proc_id, exc_info=True
        )
    _clear_decision_progress(bundle_path)
    pending_path.unlink(missing_ok=True)
    return True


def _minimal_view_for_receipt(bundle_path: Path) -> GateView | None:
    from sase_telegram.gate_flow import GateView as _View

    request = _load_json_file(bundle_path / "request.json")
    if not isinstance(request, dict):
        return None
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    raw_decisions = payload.get("decisions") if isinstance(payload, dict) else None
    decisions = tuple(raw_decisions) if isinstance(raw_decisions, list) else ()
    revision = request.get("review_revision", 1)
    try:
        revision = int(revision)
    except (TypeError, ValueError):
        revision = 1
    return _View(
        bundle_path=bundle_path,
        request_id=str(request.get("request_id") or bundle_path.name),
        kind=str(request.get("kind") or "plan"),
        options=(),
        groups=(),
        branches=(),
        decisions=tuple(decisions),
        review_revision=revision,
    )


def _verdict_for_response(view: GateView, response: dict[str, Any]) -> str:
    selected = response.get("selected_option_ids")
    ids = [str(item) for item in selected] if isinstance(selected, list) else []
    if "reject" in ids:
        return "Rejected"
    if "feedback" in ids:
        return "Feedback"
    return "Epic" if view.kind == "epic_plan" else "Tale"


def _is_stale_error(error: dict[str, Any]) -> bool:
    message = str(error.get("message") or "")
    code = str(error.get("code") or "")
    return "stale_review" in code or "stale_review" in message


def _is_launch_failure(
    error: dict[str, Any] | None,
    proc: Any | None,
    response: dict[str, Any] | None = None,
) -> bool:
    """Return whether recorded evidence proves the coder could not start.

    Only real launch-failure evidence counts. Reject, feedback,
    commit-only, and generic execution failures never acquire the claim,
    and a still-running proc is never a launch failure.
    """
    try:
        selected: list[str] = []
        if isinstance(response, dict) and isinstance(
            response.get("selected_option_ids"), list
        ):
            selected = [str(s) for s in response["selected_option_ids"]]
        if any(s in ("reject", "feedback") for s in selected):
            return False
        # Commit-only tales never launch a coder.
        if "commit" in selected and "approve" not in selected:
            return False
    except Exception:
        pass
    if error is not None:
        message = str(error.get("message") or "").lower()
        if "coder" in message and ("could not start" in message or "launch" in message):
            return True
        if str(error.get("code") or "") == "successor_launch_failed":
            return True
    # Proc status alone never proves a launch failure: a running proc keeps
    # its job pending, and generic terminal failures are reported without
    # the coder-start claim.
    return False


def _review_message_id(bundle_path: Path, record: dict[str, Any]) -> int | None:
    """Return the review card id, never a feedback reply id."""
    progress_file = bundle_path / "telegram_gate_progress.json"
    progress = _load_json_file(progress_file)
    if isinstance(progress, dict):
        # source first, then the active review id.
        for key in ("source_message_id", "active_message_id"):
            raw = progress.get(key)
            if raw is None:
                continue
            if isinstance(raw, int):
                return raw
            try:
                parsed = int(raw)
                return parsed
            except (TypeError, ValueError):
                continue
    # Saved review/action message ids from the pending record and the
    # shared pending-action store; never the feedback reply.
    for key in ("message_id", "review_message_id", "action_message_id"):
        raw = record.get(key)
        if raw is None:
            continue
        if isinstance(raw, int):
            return raw
        try:
            parsed = int(raw)
            return parsed
        except (TypeError, ValueError):
            continue
    try:
        from sase_telegram import pending_actions as _pending

        prefix = str(record.get("prefix") or "")
        if prefix:
            action = _pending.get(prefix)
            if isinstance(action, dict):
                for key in ("message_id", "review_message_id"):
                    raw = action.get(key)
                    if isinstance(raw, int):
                        return raw
                    try:
                        if raw is not None:
                            return int(raw)
                    except (TypeError, ValueError):
                        continue
    except Exception:
        pass
    return None


def _restore_refresh_for_stale(
    bundle_path: Path, chat_id: str, record: dict[str, Any]
) -> None:
    from sase_telegram import callback_data as _cb
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    message_id = _review_message_id(bundle_path, record)
    if message_id is None:
        return
    request = _load_json_file(bundle_path / "request.json")
    revision = 1
    if isinstance(request, dict):
        try:
            revision = int(request.get("review_revision", 1))
        except (TypeError, ValueError):
            revision = 1
    prefix = str(record.get("prefix") or "")
    if not prefix:
        return
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "↻ Refresh review",
                    callback_data=_cb.encode("gate", prefix, f"dRr{revision}"),
                )
            ]
        ]
    )
    try:
        telegram_client.edit_message_reply_markup(
            chat_id, message_id, reply_markup=markup
        )
    except Exception:
        log.warning("Failed to restore decision refresh controls", exc_info=True)


def _clear_decision_progress(bundle_path: Path) -> None:
    try:
        (bundle_path / "telegram_gate_progress.json").unlink(missing_ok=True)
    except OSError:
        pass


def _gate_answer_proc_status(proc_id: str) -> Any | None:
    try:
        return get_proc(proc_id)
    except Exception:
        log.warning("Failed to read Telegram gate answer proc status", exc_info=True)
        return None
