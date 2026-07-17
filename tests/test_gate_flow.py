"""Tests for resilient Telegram notification-gate progress state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sase.notification_gates.models import GateChoice

from sase_telegram.gate_flow import GateProgress, GateView, load_progress, progress_path


def _choice(
    choice_id: str,
    *,
    extras: list[dict[str, object]] | None = None,
) -> GateChoice:
    return GateChoice.from_mapping(
        {
            "id": choice_id,
            "label": choice_id.replace("_", " ").title(),
            "command": {"argv": [f"commands/{choice_id}"]},
            "extras": extras or [],
        },
        0,
    )


def _view(tmp_path: Path, *choices: GateChoice, kind: str = "epic_plan") -> GateView:
    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir()
    return GateView(
        bundle_path=bundle_path,
        request_id="telegram-progress-test",
        kind=kind,
        choices=choices,
    )


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param(None, id="missing"),
        pytest.param("{not-json", id="invalid-json"),
        pytest.param("[]", id="wrong-shape"),
    ],
)
def test_unknown_default_recovers_without_selecting_unadvertised_choice(
    tmp_path: Path,
    contents: str | None,
) -> None:
    view = _view(tmp_path, _choice("epic"))
    if contents is not None:
        progress_path(view).write_text(contents, encoding="utf-8")

    loaded = load_progress(
        view,
        default_choice_id="approve",
        active_message_id=42,
        chat_id="chat-1",
    )

    assert loaded == GateProgress(active_message_id=42, chat_id="chat-1")


def test_stale_choice_and_add_ons_recover_without_losing_message_metadata(
    tmp_path: Path,
) -> None:
    view = _view(tmp_path, _choice("epic"))
    progress_path(view).write_text(
        json.dumps(
            {
                "choice_id": "approve",
                "selected_extra_ids": ["commit_plan", "run_coder"],
                "active_message_id": 73,
                "chat_id": "saved-chat",
            }
        ),
        encoding="utf-8",
    )

    loaded = load_progress(
        view,
        default_choice_id="approve",
        active_message_id=99,
        chat_id="fallback-chat",
    )

    assert loaded == GateProgress(
        active_message_id=73,
        chat_id="saved-chat",
    )


def test_valid_saved_choice_is_retained_while_stale_add_ons_are_filtered(
    tmp_path: Path,
) -> None:
    approve = _choice(
        "approve",
        extras=[
            {
                "id": "commit_plan",
                "label": "Commit plan",
                "command": {"argv": ["commands/commit_plan"]},
                "default_selected": True,
            },
            {
                "id": "run_coder",
                "label": "Run coder",
                "command": {"argv": ["commands/run_coder"]},
                "default_selected": True,
            },
        ],
    )
    view = _view(tmp_path, approve, kind="plan")
    progress_path(view).write_text(
        json.dumps(
            {
                "choice_id": "approve",
                "selected_extra_ids": ["removed_extra", "run_coder"],
                "active_message_id": 84,
                "chat_id": "saved-chat",
            }
        ),
        encoding="utf-8",
    )

    loaded = load_progress(view, default_choice_id="missing-choice")

    assert loaded == GateProgress(
        choice_id="approve",
        selected_extra_ids=("run_coder",),
        active_message_id=84,
        chat_id="saved-chat",
    )


def test_stale_choice_uses_valid_default_with_its_default_add_ons(
    tmp_path: Path,
) -> None:
    approve = _choice(
        "approve",
        extras=[
            {
                "id": "commit_plan",
                "label": "Commit plan",
                "command": {"argv": ["commands/commit_plan"]},
                "default_selected": True,
            },
            {
                "id": "run_coder",
                "label": "Run coder",
                "command": {"argv": ["commands/run_coder"]},
                "default_selected": True,
            },
        ],
    )
    view = _view(tmp_path, approve, kind="plan")
    progress_path(view).write_text(
        json.dumps(
            {
                "choice_id": "removed-choice",
                "selected_extra_ids": ["removed-extra"],
                "active_message_id": 95,
                "chat_id": "saved-chat",
            }
        ),
        encoding="utf-8",
    )

    loaded = load_progress(view, default_choice_id="approve")

    assert loaded == GateProgress(
        choice_id="approve",
        selected_extra_ids=("commit_plan", "run_coder"),
        active_message_id=95,
        chat_id="saved-chat",
    )
