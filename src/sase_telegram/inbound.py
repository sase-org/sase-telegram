"""Pure logic for inbound Telegram message handling.

Decodes callback queries and text messages, builds response dicts,
and manages offset/feedback state. No Telegram API calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Sequence
from typing import Any


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

    action_type: str  # "plan", "hitl", "question"
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
# Awaiting-feedback state (two-step flow)
# ---------------------------------------------------------------------------


def save_awaiting_feedback(prefix: str, action_info: dict[str, Any]) -> None:
    """Save two-step feedback state to disk."""
    AWAITING_FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"prefix": prefix, "action_info": action_info}
    AWAITING_FEEDBACK_PATH.write_text(json.dumps(data, indent=2))


def load_awaiting_feedback() -> dict[str, Any] | None:
    """Load two-step feedback state, or None if not awaiting."""
    if not AWAITING_FEEDBACK_PATH.exists():
        return None
    try:
        return json.loads(AWAITING_FEEDBACK_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def clear_awaiting_feedback() -> None:
    """Clear two-step feedback state."""
    AWAITING_FEEDBACK_PATH.unlink(missing_ok=True)


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

    elif cb.action_type == "question":
        response_dir = action_data["response_dir"]
        response_path = Path(response_dir) / "question_response.json"
        if cb.choice != "custom":
            idx = int(cb.choice)
            question_text, label = _get_question_info(response_dir, idx)
            return ResponseAction(
                action_type="question",
                notif_id_prefix=cb.notif_id_prefix,
                response_path=response_path,
                response_data={
                    "answers": [
                        {
                            "question": question_text,
                            "selected": [label],
                            "custom_feedback": None,
                        }
                    ],
                    "global_note": "Answered via Telegram",
                },
                answer_text=f"Selected: {label}",
            )
        # "custom" handled by twostep

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

    if cb.action_type == "question" and cb.choice == "custom":
        response_dir = action_data["response_dir"]
        question_text = _get_question_text(response_dir)
        return (
            cb.notif_id_prefix,
            {
                "action_type": "question",
                "response_dir": response_dir,
                "question_text": question_text,
            },
        )

    return None


# ---------------------------------------------------------------------------
# Text message processing (two-step completion)
# ---------------------------------------------------------------------------


def process_text_message(text: str) -> ResponseAction | None:
    """Complete a two-step feedback flow using the user's text message.

    Returns a ResponseAction if there is an active awaiting-feedback state,
    or None if no feedback is pending.
    """
    awaiting = load_awaiting_feedback()
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

    if info["action_type"] == "question":
        return ResponseAction(
            action_type="question",
            notif_id_prefix=prefix,
            response_path=Path(info["response_dir"]) / "question_response.json",
            response_data={
                "answers": [
                    {
                        "question": info.get("question_text", ""),
                        "selected": [],
                        "custom_feedback": text,
                    }
                ],
                "global_note": "Answered via Telegram",
            },
            answer_text=None,
        )

    return None


# ---------------------------------------------------------------------------
# Photo / image helpers
# ---------------------------------------------------------------------------


def make_image_filename(file_id: str) -> str:
    """Generate a unique filename for a downloaded Telegram photo.

    Format: ``{UTC_timestamp}_{file_id_prefix}.jpg``
    """
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{file_id[:12]}.jpg"


def build_photo_prompt(image_path: Path, caption: str | None) -> str:
    """Build an agent prompt that references a downloaded image."""
    if caption:
        return (
            f"The user sent an image via Telegram with the following caption:\n\n"
            f"{caption}\n\n"
            f"The image has been saved to: {image_path}\n"
            f"Please read the image file and respond to the user's request."
        )
    return (
        f"The user sent an image via Telegram.\n\n"
        f"The image has been saved to: {image_path}\n"
        f"Please read the image file and describe what you see."
    )
