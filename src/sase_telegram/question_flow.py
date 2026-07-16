"""Progress state and pure decisions for Telegram question flows."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

PROGRESS_FILENAME = "question_progress.json"
RESPONSE_FILENAME = "question_response.json"
REQUEST_FILENAME = "question_request.json"
NEUTRAL_RESPONSE_FILENAME = "response.json"
NEUTRAL_REQUEST_FILENAME = "request.json"
GLOBAL_NOTE = "Answered via Telegram"
CUSTOM_SELECTED_LABEL = "Other"


@dataclass(frozen=True)
class QuestionProgress:
    """Telegram-private progress for one user-question session."""

    session_id: str | None
    total: int
    current_index: int = 0
    answers: list[dict[str, Any]] | None = None
    pending_selection: list[str] | None = None
    active_message_id: int | None = None
    chat_id: str | None = None

    def normalized(self) -> QuestionProgress:
        """Return a progress value with list fields and bounds normalized."""
        total = max(0, self.total)
        current_index = min(max(0, self.current_index), total)
        answers = list(self.answers or [])
        pending_selection = list(self.pending_selection or [])
        return replace(
            self,
            total=total,
            current_index=current_index,
            answers=answers,
            pending_selection=pending_selection,
        )


@dataclass(frozen=True)
class ToggleQuestion:
    kind: Literal["toggle"]
    progress: QuestionProgress
    selected: list[str]
    answer_text: str


@dataclass(frozen=True)
class AwaitCustom:
    kind: Literal["await_custom"]
    progress: QuestionProgress


@dataclass(frozen=True)
class AdvanceQuestion:
    kind: Literal["advance"]
    progress: QuestionProgress
    answered_index: int
    answer: dict[str, Any]
    next_index: int
    answer_text: str


@dataclass(frozen=True)
class CompleteQuestions:
    kind: Literal["complete"]
    progress: QuestionProgress
    answered_index: int
    answer: dict[str, Any]
    response_data: dict[str, Any]
    answer_text: str


QuestionDecision = ToggleQuestion | AwaitCustom | AdvanceQuestion | CompleteQuestions


def load_question_request(response_dir: str | Path) -> dict[str, Any]:
    """Load a neutral question payload first, then the legacy request."""
    root = Path(response_dir)
    neutral_path = root / NEUTRAL_REQUEST_FILENAME
    request_path = neutral_path if neutral_path.is_file() else root / REQUEST_FILENAME
    data = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    if request_path == neutral_path:
        if data.get("kind") != "question" or not isinstance(data.get("payload"), dict):
            return {}
        return data["payload"]
    return data


def progress_path(response_dir: str | Path) -> Path:
    """Return the Telegram progress path for a response directory."""
    return Path(response_dir) / PROGRESS_FILENAME


def response_path(response_dir: str | Path) -> Path:
    """Return the final question response path for a response directory."""
    root = Path(response_dir)
    filename = (
        NEUTRAL_RESPONSE_FILENAME
        if (root / NEUTRAL_REQUEST_FILENAME).is_file()
        else RESPONSE_FILENAME
    )
    return root / filename


def _questions(request: dict[str, Any]) -> list[dict[str, Any]]:
    raw = request.get("questions", [])
    if not isinstance(raw, list):
        return []
    return [q for q in raw if isinstance(q, dict)]


def question_count(request: dict[str, Any]) -> int:
    """Return the number of questions in a request."""
    return len(_questions(request))


def question_at(request: dict[str, Any], index: int) -> dict[str, Any]:
    """Return one question dict, or an empty dict when out of range."""
    questions = _questions(request)
    if 0 <= index < len(questions):
        return questions[index]
    return {}


def current_question(
    request: dict[str, Any], progress: QuestionProgress
) -> dict[str, Any]:
    """Return the current question for progress."""
    return question_at(request, progress.current_index)


def is_multi_select(question: dict[str, Any]) -> bool:
    """Return whether a question allows multiple option selections."""
    return bool(question.get("multiSelect", False))


def option_label(question: dict[str, Any], index: int) -> str:
    """Return the label for an option index, matching legacy fallback text."""
    options = question.get("options", [])
    if not isinstance(options, list):
        options = []
    if 0 <= index < len(options) and isinstance(options[index], dict):
        label = options[index].get("label")
        if isinstance(label, str) and label:
            return label
    return f"Option {index + 1}"


def option_count(question: dict[str, Any]) -> int:
    """Return the number of option dicts on a question."""
    options = question.get("options", [])
    return len(options) if isinstance(options, list) else 0


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def new_progress(
    request: dict[str, Any],
    *,
    active_message_id: int | None = None,
    chat_id: str | None = None,
) -> QuestionProgress:
    """Create initial progress for a request."""
    session_id = request.get("session_id")
    return QuestionProgress(
        session_id=session_id if isinstance(session_id, str) else None,
        total=question_count(request),
        current_index=0,
        answers=[],
        pending_selection=[],
        active_message_id=active_message_id,
        chat_id=chat_id,
    )


def load_progress(
    response_dir: str | Path,
    request: dict[str, Any],
    *,
    active_message_id: int | None = None,
    chat_id: str | None = None,
) -> QuestionProgress:
    """Load progress, initializing it when the session has no progress file."""
    path = progress_path(response_dir)
    if not path.exists():
        return new_progress(
            request,
            active_message_id=active_message_id,
            chat_id=chat_id,
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return new_progress(
            request,
            active_message_id=active_message_id,
            chat_id=chat_id,
        )

    if not isinstance(raw, dict):
        return new_progress(
            request,
            active_message_id=active_message_id,
            chat_id=chat_id,
        )

    progress = QuestionProgress(
        session_id=raw.get("session_id")
        if isinstance(raw.get("session_id"), str)
        else None,
        total=question_count(request),
        current_index=_optional_int(raw.get("current_index")) or 0,
        answers=raw.get("answers") if isinstance(raw.get("answers"), list) else [],
        pending_selection=(
            raw.get("pending_selection")
            if isinstance(raw.get("pending_selection"), list)
            else []
        ),
        active_message_id=_optional_int(raw.get("active_message_id"))
        or active_message_id,
        chat_id=str(raw["chat_id"]) if raw.get("chat_id") is not None else chat_id,
    ).normalized()

    if progress.active_message_id is None and active_message_id is not None:
        progress = replace(progress, active_message_id=active_message_id)
    if progress.chat_id is None and chat_id is not None:
        progress = replace(progress, chat_id=chat_id)
    return progress


def save_progress(response_dir: str | Path, progress: QuestionProgress) -> None:
    """Persist progress next to the question request."""
    data = {
        "session_id": progress.session_id,
        "total": progress.total,
        "current_index": progress.current_index,
        "answers": progress.answers or [],
        "pending_selection": progress.pending_selection or [],
        "active_message_id": progress.active_message_id,
        "chat_id": progress.chat_id,
    }
    path = progress_path(response_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clear_progress(response_dir: str | Path) -> None:
    """Remove Telegram progress for a completed or externally-handled session."""
    progress_path(response_dir).unlink(missing_ok=True)


def with_active_message(
    progress: QuestionProgress,
    *,
    active_message_id: int | None,
    chat_id: str | None,
) -> QuestionProgress:
    """Return progress updated to the current live Telegram message."""
    return replace(
        progress,
        active_message_id=active_message_id,
        chat_id=chat_id,
    ).normalized()


def is_stale_tap(progress: QuestionProgress, message_id: int | None) -> bool:
    """Return True when a callback came from an older question message."""
    return (
        progress.active_message_id is not None
        and message_id is not None
        and message_id != progress.active_message_id
    )


def _answer(
    question: dict[str, Any],
    *,
    selected: list[str],
    custom_feedback: str | None,
) -> dict[str, Any]:
    return {
        "question": str(question.get("question", "")),
        "selected": selected,
        "custom_feedback": custom_feedback,
    }


def _advance_or_complete(
    request: dict[str, Any],
    progress: QuestionProgress,
    answer: dict[str, Any],
    *,
    answer_text: str,
) -> AdvanceQuestion | CompleteQuestions:
    current_index = progress.current_index
    answers = [*(progress.answers or []), answer]
    next_index = current_index + 1
    next_progress = replace(
        progress,
        current_index=next_index,
        answers=answers,
        pending_selection=[],
    ).normalized()

    if next_index >= question_count(request):
        return CompleteQuestions(
            kind="complete",
            progress=next_progress,
            answered_index=current_index,
            answer=answer,
            response_data={"answers": answers, "global_note": GLOBAL_NOTE},
            answer_text=answer_text,
        )

    return AdvanceQuestion(
        kind="advance",
        progress=next_progress,
        answered_index=current_index,
        answer=answer,
        next_index=next_index,
        answer_text=answer_text,
    )


def apply_question_choice(
    request: dict[str, Any],
    progress: QuestionProgress,
    choice: str,
) -> QuestionDecision:
    """Apply an inline-button choice to progress and return the next decision."""
    progress = progress.normalized()
    question = current_question(request, progress)

    if choice == "custom":
        return AwaitCustom(kind="await_custom", progress=progress)

    multi_select = is_multi_select(question)

    if choice == "submit":
        if not multi_select:
            raise ValueError("submit is only valid for multi-select questions")
        selected = list(progress.pending_selection or [])
        if not selected:
            raise ValueError("at least one option must be selected")
        answer = _answer(question, selected=selected, custom_feedback=None)
        return _advance_or_complete(
            request,
            progress,
            answer,
            answer_text="Answer recorded",
        )

    try:
        option_index = int(choice)
    except ValueError as exc:
        raise ValueError(f"unknown question choice: {choice}") from exc

    label = option_label(question, option_index)
    if multi_select:
        selected = list(progress.pending_selection or [])
        if label in selected:
            selected.remove(label)
            answer_text = f"Removed: {label}"
        else:
            selected.append(label)
            answer_text = f"Selected: {label}"
        next_progress = replace(progress, pending_selection=selected).normalized()
        return ToggleQuestion(
            kind="toggle",
            progress=next_progress,
            selected=selected,
            answer_text=answer_text,
        )

    answer = _answer(question, selected=[label], custom_feedback=None)
    return _advance_or_complete(
        request,
        progress,
        answer,
        answer_text=f"Selected: {label}",
    )


def apply_question_custom_text(
    request: dict[str, Any],
    progress: QuestionProgress,
    text: str,
) -> AdvanceQuestion | CompleteQuestions:
    """Record a free-text custom answer for the current question."""
    progress = progress.normalized()
    question = current_question(request, progress)
    answer = _answer(
        question,
        selected=[CUSTOM_SELECTED_LABEL],
        custom_feedback=text,
    )
    return _advance_or_complete(
        request,
        progress,
        answer,
        answer_text="Answer recorded",
    )
