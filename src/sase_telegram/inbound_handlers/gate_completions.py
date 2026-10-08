"""Gate-answer completion delivery."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from sase_telegram import telegram_client
from sase.procs.models import TERMINAL_PROC_STATUSES
from sase.procs.store import get_proc
from sase_telegram.inbound import GATE_COMPLETION_PENDING_DIR
from sase_telegram.inbound_handlers.common import _load_json_file, _shorten_home

#: Finite wait before a missing proc row or a terminal proc without durable
#: output is reported. A still-running visible proc waits indefinitely.
#: Tests inject a frozen clock; production passes ``None`` for ``time.time()``.
MISSING_PROC_GRACE_SECONDS = 300.0

if TYPE_CHECKING:
    from sase_telegram.gate_flow import GateView

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


def _send_ready_gate_completions(now: float | None = None) -> int:
    sent_count = 0
    clock = time.time() if now is None else float(now)
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
                pending_path,
                record,
                bundle_path,
                chat_id,
                proc_id,
                created_at,
                now=clock,
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
    *,
    now: float | None = None,
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
        # Newest submission-relevant durable error first; proc status is
        # only a fallback diagnostic.
        error = _latest_gate_execution_error(bundle_path, since=created_at)
        if error is not None and _is_stale_error(error):
            if not _deliver_stale_recovery(bundle_path, chat_id, record):
                return False
            pending_path.unlink(missing_ok=True)
            return True
        if error is not None:
            if not _deliver_recorded_error_recovery(
                bundle_path, chat_id, record, error
            ):
                return False
            pending_path.unlink(missing_ok=True)
            return True
        clock = time.time() if now is None else float(now)
        proc = _gate_answer_proc_status(proc_id)
        try:
            terminal = proc is not None and proc.status in _TERMINAL
        except Exception:
            terminal = False
        if proc is not None and not terminal:
            # Still-running visible proc keeps waiting indefinitely.
            return False
        # Missing proc row or terminal proc without durable output: wait
        # out the finite grace interval, then report once with the review
        # left usable and the draft retained.
        try:
            elapsed = clock - float(created_at)
        except (TypeError, ValueError):
            elapsed = 0.0
        if elapsed < MISSING_PROC_GRACE_SECONDS:
            return False
        if not _deliver_missing_output_recovery(
            bundle_path, chat_id, record, proc_id, proc
        ):
            return False
        pending_path.unlink(missing_ok=True)
        return True
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
    # exist before a recorded coder-launch failure. Only a recorded failure
    # of the selected coder launch adds the coder-start claim.
    error = _latest_gate_execution_error(bundle_path, since=created_at)
    proc = _gate_answer_proc_status(proc_id)
    launch_failed = _is_launch_failure(error, proc, response, bundle_path=bundle_path)
    text = launch_failed_text(base) if launch_failed else base
    # Repeat polls produce one logical edit; freeze text and treat
    # already-identical as success, retrying only unfinished work. A later
    # launch failure updates the same card once with immutable values.
    if (
        isinstance(record.get("receipt_text"), str)
        and record.get("receipt_text") != text
        and not launch_failed
    ):
        text = str(record["receipt_text"])
    else:
        record["receipt_text"] = text
        if (
            launch_failed
            and isinstance(record.get("receipt_edited_text"), str)
            and record["receipt_edited_text"] != text
        ):
            # One follow-up edit/reply for the failure; accepted values stay.
            record["receipt_edit_done"] = False
            record["receipt_reply_sent"] = False
        _write_completion_record(pending_path, record)
    launch_pending = _launch_observation_pending(
        bundle_path, response, launch_failed=launch_failed
    )
    message_id = _review_message_id(bundle_path, record)
    if message_id is not None and not isinstance(record.get("review_message_id"), int):
        # Persist the review card id in the completion record as well as
        # progress, so receipt retries and restarts never edit a reply.
        try:
            record["review_message_id"] = int(message_id)
        except (TypeError, ValueError):
            pass
        _write_completion_record(pending_path, record)
    if message_id is None:
        if record.get("receipt_reply_sent") is not True:
            try:
                telegram_client.send_message(chat_id, text)
            except Exception:
                log.warning(
                    "Failed to send decision receipt for proc %s",
                    proc_id,
                    exc_info=True,
                )
                return False
            record["receipt_reply_sent"] = True
            _write_completion_record(pending_path, record)
        _settle_accepted_state(bundle_path, record)
        if launch_pending and not _launch_observation_expired(created_at):
            return False
        pending_path.unlink(missing_ok=True)
        return True
    if record.get("receipt_edit_done") is not True:
        try:
            telegram_client.edit_message_text(
                chat_id, message_id, text, reply_markup=None
            )
        except Exception as exc:
            if "not modified" in str(exc).lower() or "identical" in str(exc).lower():
                record["receipt_edit_done"] = True
                record["receipt_edited_text"] = text
                _write_completion_record(pending_path, record)
            else:
                log.warning(
                    "Failed to edit decision receipt for proc %s",
                    proc_id,
                    exc_info=True,
                )
                return False
        else:
            record["receipt_edit_done"] = True
            record["receipt_edited_text"] = text
            _write_completion_record(pending_path, record)
    # The completion reply carries the full summary sentence, sent once.
    if record.get("receipt_reply_sent") is not True:
        try:
            telegram_client.send_message(chat_id, text)
        except Exception:
            log.warning(
                "Failed to send decision completion for proc %s",
                proc_id,
                exc_info=True,
            )
            return False
        record["receipt_reply_sent"] = True
        _write_completion_record(pending_path, record)
    _settle_accepted_state(bundle_path, record)
    if launch_pending and not _launch_observation_expired(created_at):
        # Card settled; keep observing launch side effects without
        # duplicating the edit or the reply.
        return False
    pending_path.unlink(missing_ok=True)
    return True


#: Bound for launch-side-effect observation. Past this age the job stops
#: waiting and settles without inventing a launch failure.
LAUNCH_OBSERVATION_BOUND_SECONDS = 3600.0


def _write_completion_record(pending_path: Path, record: dict[str, Any]) -> None:
    try:
        pending_path.write_text(__import__("json").dumps(record, indent=2))
    except OSError:
        pass


def _settle_accepted_state(bundle_path: Path, record: dict[str, Any]) -> None:
    """Clear accepted pending action/feedback state after a successful edit.

    The completion job itself is retained separately when launch side
    effects are still unfinished, so a later sweep cannot second-settle.
    """
    _clear_decision_progress(bundle_path)
    prefix = str(record.get("prefix") or "")
    if not prefix:
        return
    try:
        from sase_telegram import pending_actions as _pending

        _pending.remove(prefix)
    except Exception:
        pass
    try:
        from sase_telegram.inbound import clear_awaiting_feedback_by_prefix

        clear_awaiting_feedback_by_prefix(prefix)
    except Exception:
        pass
    _clear_disable_retry(prefix)


def _launch_observation_pending(
    bundle_path: Path,
    response: dict[str, Any] | None,
    *,
    launch_failed: bool = False,
) -> bool:
    """Return whether launch side effects still need observation.

    Feature-detected: without host journal readers there is no pending
    observation to retain. Never invents a launch failure.
    """
    if launch_failed or not _selected_launches_coder(response):
        return False
    try:
        import importlib as _importlib

        journal = _importlib.import_module("sase.notification_gates.journal")
    except Exception:
        return False
    reader = getattr(journal, "current_post_response_failure", None)
    completed_marker = getattr(journal, "read_journal_records", None)
    if reader is None or completed_marker is None:
        return False
    try:
        records = completed_marker(bundle_path)
    except Exception:
        return False
    if not isinstance(records, tuple | list):
        return False
    pending_stage = False
    for entry in records:
        if not isinstance(entry, dict):
            continue
        if entry.get("stage") not in ("side_effects", "follow_up"):
            continue
        event = entry.get("event")
        if event == "stage_started":
            pending_stage = True
        elif event in ("stage_completed", "attempt_failed"):
            pending_stage = False
    return pending_stage


def _launch_observation_expired(created_at: float) -> bool:
    try:
        return (time.time() - float(created_at)) >= LAUNCH_OBSERVATION_BOUND_SECONDS
    except (TypeError, ValueError):
        return False


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


#: Post-response stages whose recorded failure proves the selected coder
#: launch failed. Read from structured ``stage`` identities, never from
#: message words.
_LAUNCH_FAILURE_STAGES = frozenset({"side_effects", "follow_up", "coder", "launch"})


def _selected_launches_coder(response: dict[str, Any] | None) -> bool:
    """Return whether the accepted options include a coder launch."""
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
        # Epic launch and tale approve both start implementation work.
        return "approve" in selected or any(
            s not in ("reject", "feedback", "commit") for s in selected
        )
    except Exception:
        return False


def _host_launch_failure(bundle_path: Path, response: dict[str, Any] | None) -> bool:
    """Feature-detected host journal/metadata check for a coder-start failure."""
    try:
        import importlib as _importlib

        journal = _importlib.import_module("sase.notification_gates.journal")
    except Exception:
        journal = None  # type: ignore[assignment]
    if journal is not None:
        for stage in ("side_effects", "follow_up"):
            reader = getattr(journal, "current_post_response_failure", None)
            if reader is None:
                continue
            try:
                failure = reader(bundle_path, response, stage=stage)
            except Exception:
                continue
            if failure is not None:
                return True
    # Gate-turn metadata tolerates coder follow-up errors as
    # ``gate_followup_error`` instead of raising a journal failure.
    try:
        meta = _load_json_file(bundle_path / "meta.json")
        if isinstance(meta, dict) and str(meta.get("gate_followup_error") or ""):
            return True
    except Exception:
        pass
    try:
        if isinstance(response, dict):
            meta = response.get("meta") or response.get("metadata")
            if isinstance(meta, dict) and str(meta.get("gate_followup_error") or ""):
                return True
    except Exception:
        pass
    return False


def _is_launch_failure(
    error: dict[str, Any] | None,
    proc: Any | None,
    response: dict[str, Any] | None = None,
    *,
    bundle_path: Path | None = None,
) -> bool:
    """Return whether recorded evidence proves the coder could not start.

    Only a recorded failure of the selected coder launch counts. Reject,
    feedback, commit-only, unrelated execution failures, and a running proc
    never acquire the claim. Structured ``stage`` identities and host
    journal/metadata readers decide; message words never do.
    """
    if not _selected_launches_coder(response):
        return False
    try:
        if proc is not None and proc.status not in TERMINAL_PROC_STATUSES:
            return False
    except Exception:
        pass
    if error is not None:
        stage = error.get("stage")
        if isinstance(stage, str) and stage in _LAUNCH_FAILURE_STAGES:
            return True
    if bundle_path is not None:
        try:
            if _host_launch_failure(bundle_path, response):
                return True
        except Exception:
            pass
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


def _recorded_error_message(error: dict[str, Any]) -> str:
    """Return the structured recorded message for a pre-response rejection."""
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    code = error.get("code")
    if isinstance(code, str) and code.strip():
        return code.strip()
    return "unknown error"


def _clear_disable_retry(prefix: str) -> None:
    """Clear a pending keyboard-removal retry so it cannot erase recovery."""
    if not prefix:
        return
    try:
        from sase_telegram.inbound_handlers.keyboard_cleanup import (
            clear_keyboard_cleanup_pending,
        )

        clear_keyboard_cleanup_pending(prefix)
    except Exception:
        pass


def _restore_refresh_for_stale(
    bundle_path: Path, chat_id: str, record: dict[str, Any]
) -> bool:
    """Restore the Refresh control on the original card. Return True on success."""
    from sase_telegram import callback_data as _cb
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    message_id = _review_message_id(bundle_path, record)
    if message_id is None:
        return False
    request = _load_json_file(bundle_path / "request.json")
    revision = 1
    if isinstance(request, dict):
        try:
            revision = int(request.get("review_revision", 1))
        except (TypeError, ValueError):
            revision = 1
    prefix = str(record.get("prefix") or "")
    if not prefix:
        return False
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
        return False
    _clear_disable_retry(prefix)
    return True


def _restore_usable_controls(
    bundle_path: Path, chat_id: str, record: dict[str, Any]
) -> bool:
    """Restore the full usable keyboard with the draft retained."""
    prefix = str(record.get("prefix") or "")
    if not prefix:
        return False
    message_id = _review_message_id(bundle_path, record)
    if message_id is None:
        return False
    try:
        view = _minimal_view_for_receipt(bundle_path)
        if view is None:
            return False
        from sase_telegram.formatting import render_gate_keyboard
        from sase_telegram.gate_flow import load_progress as _load_progress

        progress = _load_progress(view)
        markup = render_gate_keyboard(prefix, view, progress)
    except Exception:
        log.warning("Failed to render usable decision controls", exc_info=True)
        return False
    try:
        telegram_client.edit_message_reply_markup(
            chat_id, message_id, reply_markup=markup
        )
    except Exception:
        log.warning("Failed to restore usable decision controls", exc_info=True)
        return False
    _clear_disable_retry(prefix)
    return True


def _deliver_stale_recovery(
    bundle_path: Path, chat_id: str, record: dict[str, Any]
) -> bool:
    """Send the exact stale text and restore Refresh; keep draft and action."""
    from sase_telegram.plan_decisions import STALE_TEXT

    try:
        telegram_client.send_message(chat_id, STALE_TEXT)
    except Exception:
        log.warning("Failed to send stale recovery report", exc_info=True)
        return False
    # Keyboard restoration is part of delivery; a failed edit keeps the
    # durable completion retry instead of claiming recovery.
    return _restore_refresh_for_stale(bundle_path, chat_id, record)


def _deliver_recorded_error_recovery(
    bundle_path: Path,
    chat_id: str,
    record: dict[str, Any],
    error: dict[str, Any],
) -> bool:
    """Report a recorded pre-response message and leave the review retryable."""
    try:
        telegram_client.send_message(chat_id, _recorded_error_message(error))
    except Exception:
        log.warning("Failed to send recorded error recovery", exc_info=True)
        return False
    return _restore_usable_controls(bundle_path, chat_id, record)


def _deliver_missing_output_recovery(
    bundle_path: Path,
    chat_id: str,
    record: dict[str, Any],
    proc_id: str,
    proc: Any | None,
) -> bool:
    """Report missing output once and restore usable controls with draft kept."""
    try:
        status = getattr(proc, "status", "?") if proc is not None else "missing"
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
    # Restore usable controls; the draft and pending action stay so the
    # reviewer can retry. A failed restore keeps the durable retry.
    # The progress file is deliberately retained here.
    if not _restore_usable_controls(bundle_path, chat_id, record):
        return False
    return True


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
