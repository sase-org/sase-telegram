"""Tests for Telegram sequential question flow decisions."""

from __future__ import annotations

import json
from pathlib import Path

from sase_telegram import question_flow


def _request() -> dict:
    return {
        "session_id": "s1",
        "questions": [
            {
                "question": "Which databases?",
                "options": [{"label": "PostgreSQL"}, {"label": "SQLite"}],
                "multiSelect": True,
            },
            {
                "question": "Use migrations?",
                "options": [{"label": "Yes"}, {"label": "No"}],
            },
            {"question": "Any notes?", "options": []},
        ],
    }


def test_single_select_completes_single_question() -> None:
    request = {
        "session_id": "s1",
        "questions": [
            {
                "question": "Which approach?",
                "options": [{"label": "A"}, {"label": "B"}],
            }
        ],
    }
    progress = question_flow.new_progress(request, active_message_id=42, chat_id="c")

    decision = question_flow.apply_question_choice(request, progress, "1")

    assert decision.kind == "complete"
    assert decision.response_data == {
        "answers": [
            {
                "question": "Which approach?",
                "selected": ["B"],
                "custom_feedback": None,
            }
        ],
        "global_note": "Answered via Telegram",
    }


def test_multi_select_toggles_then_advances_and_completes() -> None:
    request = _request()
    progress = question_flow.new_progress(request, active_message_id=10, chat_id="c")

    first = question_flow.apply_question_choice(request, progress, "0")
    assert first.kind == "toggle"
    assert first.selected == ["PostgreSQL"]

    second = question_flow.apply_question_choice(request, first.progress, "1")
    assert second.kind == "toggle"
    assert second.selected == ["PostgreSQL", "SQLite"]

    third = question_flow.apply_question_choice(request, second.progress, "0")
    assert third.kind == "toggle"
    assert third.selected == ["SQLite"]

    advanced = question_flow.apply_question_choice(request, third.progress, "submit")
    assert advanced.kind == "advance"
    assert advanced.next_index == 1
    assert advanced.progress.answers == [
        {
            "question": "Which databases?",
            "selected": ["SQLite"],
            "custom_feedback": None,
        }
    ]
    assert advanced.progress.pending_selection == []

    progress = question_flow.with_active_message(
        advanced.progress, active_message_id=11, chat_id="c"
    )
    next_decision = question_flow.apply_question_choice(request, progress, "0")
    assert next_decision.kind == "advance"

    progress = question_flow.with_active_message(
        next_decision.progress, active_message_id=12, chat_id="c"
    )
    complete = question_flow.apply_question_custom_text(
        request, progress, "Use pgvector"
    )
    assert complete.kind == "complete"
    assert [answer["selected"] for answer in complete.response_data["answers"]] == [
        ["SQLite"],
        ["Yes"],
        ["Other"],
    ]
    assert complete.response_data["answers"][2]["custom_feedback"] == "Use pgvector"


def test_save_load_clear_progress_roundtrip(tmp_path: Path) -> None:
    request = _request()
    progress = question_flow.QuestionProgress(
        session_id="s1",
        total=3,
        current_index=1,
        answers=[{"question": "Q", "selected": ["A"], "custom_feedback": None}],
        pending_selection=["B"],
        active_message_id=99,
        chat_id="chat",
    )

    question_flow.save_progress(tmp_path, progress)
    loaded = question_flow.load_progress(tmp_path, request)

    assert loaded == progress
    assert question_flow.progress_path(tmp_path).exists()
    question_flow.clear_progress(tmp_path)
    assert not question_flow.progress_path(tmp_path).exists()


def test_load_progress_initializes_from_request(tmp_path: Path) -> None:
    request = _request()
    loaded = question_flow.load_progress(
        tmp_path,
        request,
        active_message_id=7,
        chat_id="chat",
    )

    assert loaded.session_id == "s1"
    assert loaded.total == 3
    assert loaded.current_index == 0
    assert loaded.active_message_id == 7
    assert loaded.chat_id == "chat"


def test_load_question_request(tmp_path: Path) -> None:
    request = _request()
    (tmp_path / "question_request.json").write_text(json.dumps(request))

    assert question_flow.load_question_request(tmp_path) == request
