"""Pure logic for inbound Telegram message handling.

Decodes callback queries and text messages, builds response dicts,
and manages offset/feedback state. No Telegram API calls.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Sequence
from typing import Any


_LAUNCH_XPROMPT_AT_WORKFLOWS = ("gh", "git", "hg", "jj", "p4", "cd")
_LAUNCH_XPROMPT_AT_REF_RE = re.compile(
    rf"(?P<context>^|(?<=[\s([{{\"']))"
    rf"#(?P<workflow>{'|'.join(_LAUNCH_XPROMPT_AT_WORKFLOWS)})"
    r"(?P<marker>!!|\?\?)?"
    r"@(?P<ref>[A-Za-z0-9][A-Za-z0-9_.~/-]*)"
    r"(?=$|[\s)\]},.!?;:\"'])",
    re.IGNORECASE,
)


def _code_span_ranges(text: str) -> list[tuple[int, int]]:
    """Return Markdown inline/fenced code ranges in *text*."""
    ranges: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        if text.startswith("```", i):
            start = i
            close = text.find("```", i + 3)
            if close == -1:
                ranges.append((start, len(text)))
                break
            ranges.append((start, close + 3))
            i = close + 3
            continue

        if text[i] == "`":
            start = i
            close = text.find("`", i + 1)
            if close == -1:
                i += 1
                continue
            ranges.append((start, close + 1))
            i = close + 1
            continue

        i += 1
    return ranges


def _is_inside_ranges(index: int, ranges: Sequence[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in ranges)


def normalize_launch_xprompt_at_refs(text: str) -> str:
    """Normalize Telegram ``#workflow@ref`` launch shorthand to ``#workflow:ref``.

    The rewrite is intentionally scoped to known workspace/VCS workflows and
    skips Markdown code spans, which Telegram message entities reconstruct
    before launch handling.
    """
    if "@" not in text or "#" not in text:
        return text

    code_ranges = _code_span_ranges(text)

    def replace(match: re.Match[str]) -> str:
        if _is_inside_ranges(match.start(), code_ranges):
            return match.group(0)
        marker = match.group("marker") or ""
        return f"#{match.group('workflow')}{marker}:{match.group('ref')}"

    return _LAUNCH_XPROMPT_AT_REF_RE.sub(replace, text)


def reconstruct_code_markers(text: str, entities: Sequence[Any] | None) -> str:
    """Re-insert backtick markers around ``code`` and ``pre`` entities.

    Telegram strips backticks and delivers them as MessageEntity objects.
    This function reconstructs the original markdown so downstream handlers
    (e.g. xprompt expansion) can honour backtick-protected text.
    """
    if not entities:
        return text

    # Process in reverse offset order so earlier positions stay valid.
    for entity in sorted(entities, key=lambda e: e.offset, reverse=True):
        start = entity.offset
        end = start + entity.length
        content = text[start:end]

        if entity.type == "code":
            text = text[:start] + f"`{content}`" + text[end:]
        elif entity.type == "pre":
            lang = getattr(entity, "language", None) or ""
            text = text[:start] + f"```{lang}\n{content}\n```" + text[end:]

    return text


UPDATE_OFFSET_PATH = Path.home() / ".sase" / "telegram" / "update_offset.txt"
AWAITING_FEEDBACK_PATH = Path.home() / ".sase" / "telegram" / "awaiting_feedback.json"
IMAGES_DIR = Path.home() / ".sase" / "telegram" / "images"


@dataclass
class ResponseAction:
    """A response to write based on a user's Telegram interaction."""

    action_type: str  # "plan", "hitl", "launch", "question"
    notif_id_prefix: str  # 8-char prefix
    response_path: Path  # Where to write response JSON
    response_data: dict[str, Any]  # JSON content
    answer_text: str | None  # Text for answer_callback_query popup


# ---------------------------------------------------------------------------
# Offset persistence
# ---------------------------------------------------------------------------


def get_last_offset() -> int | None:
    """Load the last processed Telegram update offset."""
    if not UPDATE_OFFSET_PATH.exists():
        return None
    try:
        return int(UPDATE_OFFSET_PATH.read_text().strip())
    except (ValueError, OSError):
        return None


def save_offset(offset: int) -> None:
    """Persist the Telegram update offset."""
    UPDATE_OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_OFFSET_PATH.write_text(str(offset))


# ---------------------------------------------------------------------------
# Awaiting-feedback state (two-step flow), keyed by Telegram message_id
# ---------------------------------------------------------------------------

_LEGACY_AWAITING_KEY = "_legacy"


def _load_awaiting_map() -> dict[str, Any]:
    """Read the awaiting-feedback map, normalizing the legacy single-entry shape.

    The on-disk file used to be ``{"prefix": "...", "action_info": {...}}``.
    Such files are surfaced as a single ``_LEGACY_AWAITING_KEY`` entry so callers
    can still look up the only pending flow when no explicit key is known.
    """
    if not AWAITING_FEEDBACK_PATH.exists():
        return {}
    try:
        data = json.loads(AWAITING_FEEDBACK_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "prefix" in data and "action_info" in data:
        return {
            _LEGACY_AWAITING_KEY: {
                "prefix": data["prefix"],
                "action_info": data["action_info"],
            }
        }
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _save_awaiting_map(data: dict[str, Any]) -> None:
    AWAITING_FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    AWAITING_FEEDBACK_PATH.write_text(json.dumps(data, indent=2))


def save_awaiting_feedback(key: str, prefix: str, action_info: dict[str, Any]) -> None:
    """Record an awaiting-feedback entry under ``key``.

    ``key`` is typically the originating Telegram ``message_id`` (the message
    hosting the feedback button). Existing entries under other keys are kept,
    so concurrent two-step flows do not overwrite each other.
    """
    data = _load_awaiting_map()
    data[str(key)] = {"prefix": prefix, "action_info": action_info}
    _save_awaiting_map(data)


def load_awaiting_feedback(key: str | None = None) -> dict[str, Any] | None:
    """Return one awaiting-feedback entry.

    If ``key`` is provided, return the entry under that key, or ``None``.
    If ``key`` is ``None``, return the only entry when exactly one exists,
    or ``None`` otherwise (legacy / single-flow fallback).
    """
    data = _load_awaiting_map()
    if not data:
        return None
    if key is not None:
        return data.get(str(key))
    if len(data) == 1:
        return next(iter(data.values()))
    return None


def load_all_awaiting_feedback() -> dict[str, Any]:
    """Return the full awaiting-feedback map (key -> entry)."""
    return _load_awaiting_map()


def clear_awaiting_feedback(key: str | None = None) -> None:
    """Clear awaiting-feedback entries.

    With ``key``, drop only that entry. With ``key=None``, clear everything
    (used by tests and as a hard-reset path).
    """
    if key is None:
        AWAITING_FEEDBACK_PATH.unlink(missing_ok=True)
        return
    data = _load_awaiting_map()
    skey = str(key)
    if skey not in data:
        return
    del data[skey]
    if data:
        _save_awaiting_map(data)
    else:
        AWAITING_FEEDBACK_PATH.unlink(missing_ok=True)


def clear_awaiting_feedback_by_prefix(prefix: str) -> str | None:
    """Drop the awaiting-feedback entry whose ``prefix`` matches.

    Returns the key that was cleared, or ``None`` if no entry matched.
    Used by externally-handled cleanup, where the originating key may not
    be known but the action prefix is.
    """
    data = _load_awaiting_map()
    for key, entry in data.items():
        if entry.get("prefix") == prefix:
            clear_awaiting_feedback(key)
            return key
    return None


# ---------------------------------------------------------------------------
# Callback processing
# ---------------------------------------------------------------------------


def _get_question_info(response_dir: str, idx: int) -> tuple[str, str]:
    """Return (question_text, option_label) from question_request.json."""
    request_file = Path(response_dir) / "question_request.json"
    request_data = json.loads(request_file.read_text())
    questions = request_data.get("questions", [])
    question_text = questions[0].get("question", "") if questions else ""
    options = questions[0].get("options", []) if questions else []
    if idx < len(options):
        label = options[idx].get("label", f"Option {idx + 1}")
    else:
        label = f"Option {idx + 1}"
    return question_text, label


def _get_question_text(response_dir: str) -> str:
    """Return the first question's text from question_request.json."""
    request_file = Path(response_dir) / "question_request.json"
    try:
        request_data = json.loads(request_file.read_text())
        questions = request_data.get("questions", [])
        return questions[0].get("question", "") if questions else ""
    except (OSError, json.JSONDecodeError):
        return ""


def process_callback(
    callback_data_str: str, pending: dict[str, Any]
) -> ResponseAction | None:
    """Decode a callback and build a ResponseAction for immediate responses.

    Returns None for two-step callbacks (feedback/custom) and unknown actions.
    """
    from sase_telegram.callback_data import decode

    cb = decode(callback_data_str)
    action = pending.get(cb.notif_id_prefix)
    if action is None:
        return None

    action_data = action["action_data"]

    if cb.action_type == "plan":
        response_dir = action_data["response_dir"]
        response_path = Path(response_dir) / "plan_response.json"
        if cb.choice == "approve":
            return ResponseAction(
                action_type="plan",
                notif_id_prefix=cb.notif_id_prefix,
                response_path=response_path,
                response_data={"action": "approve"},
                answer_text="Plan approved",
            )
        elif cb.choice == "reject":
            return ResponseAction(
                action_type="plan",
                notif_id_prefix=cb.notif_id_prefix,
                response_path=response_path,
                response_data={"action": "reject"},
                answer_text="Plan rejected",
            )
        elif cb.choice == "epic":
            return ResponseAction(
                action_type="plan",
                notif_id_prefix=cb.notif_id_prefix,
                response_path=response_path,
                response_data={"action": "epic"},
                answer_text="Epic created",
            )
        elif cb.choice == "run":
            return ResponseAction(
                action_type="plan",
                notif_id_prefix=cb.notif_id_prefix,
                response_path=response_path,
                response_data={
                    "action": "approve",
                    "commit_plan": False,
                    "run_coder": True,
                },
                answer_text="Running coder (no commit)",
            )

    elif cb.action_type == "hitl":
        artifacts_dir = action_data["artifacts_dir"]
        response_path = Path(artifacts_dir) / "hitl_response.json"
        if cb.choice == "accept":
            return ResponseAction(
                action_type="hitl",
                notif_id_prefix=cb.notif_id_prefix,
                response_path=response_path,
                response_data={"action": "accept", "approved": True},
                answer_text="Accepted",
            )
        elif cb.choice == "reject":
            return ResponseAction(
                action_type="hitl",
                notif_id_prefix=cb.notif_id_prefix,
                response_path=response_path,
                response_data={"action": "reject", "approved": False},
                answer_text="Rejected",
            )
        # "feedback" handled by twostep

    elif cb.action_type == "launch":
        response_dir = action_data["response_dir"]
        response_path = Path(response_dir) / "launch_response.json"
        if cb.choice == "approve":
            return ResponseAction(
                action_type="launch",
                notif_id_prefix=cb.notif_id_prefix,
                response_path=response_path,
                response_data={"action": "approve"},
                answer_text="Launch approved",
            )
        elif cb.choice == "reject":
            return ResponseAction(
                action_type="launch",
                notif_id_prefix=cb.notif_id_prefix,
                response_path=response_path,
                response_data={"action": "reject"},
                answer_text="Launch rejected",
            )
        # "feedback" handled by twostep

    elif cb.action_type == "question":
        # Telegram question sessions are progress-aware and handled by
        # scripts.sase_tg_inbound so multi-question requests cannot be
        # resolved from a single callback.
        return None

    return None


def process_callback_twostep(
    callback_data_str: str, pending: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """Check if a callback initiates a two-step feedback flow.

    Returns (notif_id_prefix, action_info) for feedback/custom callbacks,
    or None for regular one-shot callbacks.
    """
    from sase_telegram.callback_data import decode

    cb = decode(callback_data_str)
    action = pending.get(cb.notif_id_prefix)
    if action is None:
        return None

    action_data = action["action_data"]

    if cb.action_type == "plan" and cb.choice == "feedback":
        return (
            cb.notif_id_prefix,
            {
                "action_type": "plan",
                "response_dir": action_data["response_dir"],
            },
        )

    if cb.action_type == "hitl" and cb.choice == "feedback":
        return (
            cb.notif_id_prefix,
            {
                "action_type": "hitl",
                "artifacts_dir": action_data["artifacts_dir"],
            },
        )

    if cb.action_type == "launch" and cb.choice == "feedback":
        return (
            cb.notif_id_prefix,
            {
                "action_type": "launch",
                "response_dir": action_data["response_dir"],
            },
        )

    if cb.action_type == "question" and cb.choice == "custom":
        return None

    return None


# ---------------------------------------------------------------------------
# Text message processing (two-step completion)
# ---------------------------------------------------------------------------


def process_text_message(text: str, key: str | None = None) -> ResponseAction | None:
    """Complete a two-step feedback flow using the user's text message.

    ``key`` selects the matching awaiting-feedback entry (typically the
    Telegram ``message_id`` the user replied to). When omitted, falls back to
    the unique pending entry — preserves single-flow behavior and legacy
    state files.
    """
    awaiting = load_awaiting_feedback(key)
    if not awaiting:
        return None

    prefix: str = awaiting["prefix"]
    info: dict[str, Any] = awaiting["action_info"]

    if info["action_type"] == "plan":
        return ResponseAction(
            action_type="plan",
            notif_id_prefix=prefix,
            response_path=Path(info["response_dir"]) / "plan_response.json",
            response_data={
                "action": "reject",
                "feedback": text,
            },
            answer_text=None,
        )

    if info["action_type"] == "hitl":
        return ResponseAction(
            action_type="hitl",
            notif_id_prefix=prefix,
            response_path=Path(info["artifacts_dir"]) / "hitl_response.json",
            response_data={
                "action": "feedback",
                "approved": False,
                "feedback": text,
            },
            answer_text=None,
        )

    if info["action_type"] == "launch":
        return ResponseAction(
            action_type="launch",
            notif_id_prefix=prefix,
            response_path=Path(info["response_dir"]) / "launch_response.json",
            response_data={
                "action": "feedback",
                "feedback": text,
            },
            answer_text=None,
        )

    if info["action_type"] == "question":
        return None

    return None


def resolve_launch_response(
    response: ResponseAction,
    action: dict[str, Any] | None,
) -> str:
    """Resolve a LaunchApproval response through the host shared executor."""
    from sase.launch_approval_actions import (
        LaunchApprovalActionContext,
        LaunchApprovalActionError,
        execute_launch_approval_response,
    )

    if action is None:
        raise LaunchApprovalActionError(
            "not_found",
            response.notif_id_prefix,
            "pending launch action is missing",
        )
    action_data = action.get("action_data")
    if not isinstance(action_data, dict):
        raise LaunchApprovalActionError(
            "invalid_request",
            "action_data",
            "launch action data is missing",
        )

    choice = response.response_data.get("action")
    feedback = response.response_data.get("feedback")
    result = execute_launch_approval_response(
        LaunchApprovalActionContext(
            id=str(action.get("notification_id") or response.notif_id_prefix),
            host_files=tuple(str(path) for path in action.get("files", [])),
            host_action_data={
                str(key): str(value) for key, value in action_data.items()
            },
        ),
        str(choice),
        feedback=str(feedback) if feedback is not None else None,
    )
    return result.message


# ---------------------------------------------------------------------------
# Confirmation text for two-step completions
# ---------------------------------------------------------------------------


def confirmation_text(response: ResponseAction) -> str:
    """Return a human-readable confirmation string for a two-step response."""
    if response.action_type == "plan":
        return "\u2705 Feedback received \u2014 plan will be revised"
    if response.action_type == "hitl":
        return "\u2705 Feedback received"
    if response.action_type == "launch":
        return "\u2705 Feedback received \u2014 launch rejected"
    if response.action_type == "question":
        return "\u2705 Answer received"
    return "\u2705 Response received"


# ---------------------------------------------------------------------------
# Photo / image helpers
# ---------------------------------------------------------------------------


def make_image_filename(file_id: str) -> str:
    """Generate a unique filename for a downloaded Telegram photo.

    Format: ``{UTC_timestamp}_{file_id_prefix}.jpg``
    """
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{file_id[:12]}.jpg"


_ACTIONABLE_ACTIONS = {"PlanApproval", "HITL", "LaunchApproval", "UserQuestion"}


def find_externally_handled(
    pending: dict[str, Any],
) -> list[tuple[str, int, str]]:
    """Find pending actions whose notifications were handled externally (e.g. TUI).

    Returns list of (notif_id_prefix, message_id, chat_id) for actions that
    should have their Telegram buttons removed.
    """
    handled: list[tuple[str, int, str]] = []
    for prefix, entry in pending.items():
        action = entry.get("action")
        if action not in _ACTIONABLE_ACTIONS:
            continue
        action_data = entry.get("action_data", {})

        if action == "PlanApproval":
            response_dir = Path(action_data.get("response_dir", ""))
            if (
                (response_dir / "plan_response.json").exists()
                or (response_dir / "plan_approved.marker").exists()
                or (
                    response_dir != Path("")
                    and not (response_dir / "plan_request.json").exists()
                )
            ):
                handled.append((prefix, entry["message_id"], entry["chat_id"]))

        elif action == "HITL":
            artifacts_dir = Path(action_data.get("artifacts_dir", ""))
            if (artifacts_dir / "hitl_response.json").exists():
                handled.append((prefix, entry["message_id"], entry["chat_id"]))

        elif action == "LaunchApproval":
            response_dir = Path(action_data.get("response_dir", ""))
            if (response_dir / "launch_response.json").exists() or (
                response_dir != Path("")
                and not (response_dir / "launch_request.json").exists()
            ):
                handled.append((prefix, entry["message_id"], entry["chat_id"]))

        elif action == "UserQuestion":
            response_dir = Path(action_data.get("response_dir", ""))
            if (response_dir / "question_response.json").exists():
                handled.append((prefix, entry["message_id"], entry["chat_id"]))

    return handled


_TELEGRAM_TRANSPORTS = ("telegram", "telegram_legacy")
_RESOLVED_SHARED_STATES = {"already_handled", "stale"}


def _telegram_transport_record(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Return the Telegram transport record (chat_id + message_id) for *entry*."""
    transports = entry.get("transports")
    if not isinstance(transports, list):
        return None
    for item in transports:
        if (
            not isinstance(item, dict)
            or item.get("transport") not in _TELEGRAM_TRANSPORTS
        ):
            continue
        record = item.get("record")
        if not isinstance(record, dict):
            continue
        if record.get("chat_id") is not None and record.get("message_id") is not None:
            return record
    return None


def _shared_action_resolved(entry: dict[str, Any], now: float) -> bool:
    """Return True when a shared pending-action entry is no longer actionable."""
    if entry.get("state") in _RESOLVED_SHARED_STATES:
        return True
    deadline = entry.get("stale_deadline_unix")
    return isinstance(deadline, (int, float)) and deadline <= now


def find_shared_handled_transports(
    store: dict[str, Any], *, now: float
) -> list[tuple[str, int, str]]:
    """Find Telegram messages whose shared pending action is already resolved.

    Reads the shared host pending-action store (with legacy Telegram records
    merged in) and returns ``(prefix, message_id, chat_id)`` for entries that
    were handled, went stale, or expired and still carry a Telegram transport
    record. The inbound chop removes those inline keyboards.
    """
    results: list[tuple[str, int, str]] = []
    actions = store.get("actions")
    if not isinstance(actions, dict):
        return results
    for prefix, entry in actions.items():
        if not isinstance(entry, dict):
            continue
        record = _telegram_transport_record(entry)
        if record is None or not _shared_action_resolved(entry, now):
            continue
        try:
            message_id = int(record["message_id"])
        except (TypeError, ValueError):
            continue
        results.append((str(prefix), message_id, str(record["chat_id"])))
    return results


def _normalized_caption(caption: str | None) -> str | None:
    if not caption:
        return None
    normalized = normalize_launch_xprompt_at_refs(caption)
    return normalized if normalized.strip() else None


def build_image_prompt(image_paths: Sequence[Path], caption: str | None) -> str:
    """Build an agent prompt that references one or more downloaded images."""
    paths = list(image_paths)
    if not paths:
        raise ValueError("at least one image path is required")

    normalized_caption = _normalized_caption(caption)
    if len(paths) == 1:
        return build_photo_prompt(paths[0], normalized_caption)

    image_list = "\n".join(f"{idx}. {path}" for idx, path in enumerate(paths, 1))
    if normalized_caption:
        return (
            f"The user sent {len(paths)} images via Telegram with the following "
            f"caption:\n\n"
            f"{normalized_caption}\n\n"
            f"The images have been saved to:\n{image_list}\n"
            f"Please read the image files and respond to the user's request."
        )
    return (
        f"The user sent {len(paths)} images via Telegram.\n\n"
        f"The images have been saved to:\n{image_list}\n"
        f"Please read the image files and describe what you see."
    )


def build_photo_prompt(image_path: Path, caption: str | None) -> str:
    """Build an agent prompt that references one downloaded image."""
    normalized_caption = _normalized_caption(caption)
    if normalized_caption:
        return (
            f"The user sent an image via Telegram with the following caption:\n\n"
            f"{normalized_caption}\n\n"
            f"The image has been saved to: {image_path}\n"
            f"Please read the image file and respond to the user's request."
        )
    return (
        f"The user sent an image via Telegram.\n\n"
        f"The image has been saved to: {image_path}\n"
        f"Please read the image file and describe what you see."
    )
