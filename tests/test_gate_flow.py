"""Tests for resilient Telegram notification-gate progress state."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest
from sase.notification_gates.models import GateGroup, GateOption

from sase_telegram.gate_flow import (
    GateProgress,
    GateView,
    expand_branch,
    feedback_mode,
    initial_progress,
    load_progress,
    progress_path,
    save_progress,
    toggle_option,
)
from sase_telegram.formatting import render_gate_keyboard


def _option(
    option_id: str,
    *,
    default_selected: bool = True,
    feedback: str = "disabled",
    input_schema: dict[str, object] | None = None,
) -> GateOption:
    return GateOption.from_mapping(
        {
            "id": option_id,
            "label": option_id.replace("_", " ").title(),
            "command": {"argv": [f"commands/{option_id}"]},
            "default_selected": default_selected,
            "feedback": feedback,
            "input_schema": input_schema or {},
        },
        0,
    )


def _group(*option_ids: str, label: str = "Submit") -> GateGroup:
    return GateGroup.from_mapping(
        {"options": list(option_ids), "label": label, "icon": "✅"}, 0
    )


def _view(
    tmp_path: Path,
    *,
    options: tuple[GateOption, ...],
    branches: tuple[tuple[str, ...], ...],
    groups: tuple[GateGroup, ...] = (),
    kind: str = "custom",
) -> GateView:
    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir()
    return GateView(
        bundle_path=bundle_path,
        request_id="telegram-progress-test",
        kind=kind,
        options=options,
        groups=groups,
        branches=branches,
    )


def test_single_and_group_expands_with_query_order_defaults(tmp_path: Path) -> None:
    view = _view(
        tmp_path,
        options=(
            _option("approve"),
            _option("commit", default_selected=False),
            _option("reject"),
        ),
        branches=(("approve", "commit"), ("reject",)),
        groups=(_group("approve", "commit", label="Approve"),),
    )

    assert initial_progress(view, active_message_id=42, chat_id="chat-1") == (
        GateProgress(
            selected_option_ids=("approve",),
            expanded_branch_index=0,
            active_message_id=42,
            chat_id="chat-1",
        )
    )


def test_multiple_groups_start_collapsed_and_expansion_resets_defaults(
    tmp_path: Path,
) -> None:
    view = _view(
        tmp_path,
        options=tuple(_option(option_id) for option_id in ("a", "b", "c", "d")),
        branches=(("a", "b"), ("c", "d")),
        groups=(_group("a", "b"), _group("c", "d")),
    )

    progress = initial_progress(view)
    assert progress == GateProgress()
    assert expand_branch(view, progress, 1) == GateProgress(
        selected_option_ids=("c", "d"), expanded_branch_index=1
    )


def test_multiple_groups_render_collapsed_then_expand_only_the_activated_group(
    tmp_path: Path,
) -> None:
    view = _view(
        tmp_path,
        options=tuple(_option(option_id) for option_id in ("a", "b", "c", "d")),
        branches=(("a", "b"), ("c", "d")),
        groups=(
            _group("a", "b", label="First"),
            _group("c", "d", label="Second"),
        ),
    )

    collapsed = render_gate_keyboard("gate0001", view, initial_progress(view))
    assert [[button.text for button in row] for row in collapsed.inline_keyboard] == [
        ["✅ First"],
        ["✅ Second"],
    ]

    expanded = render_gate_keyboard(
        "gate0001", view, expand_branch(view, initial_progress(view), 1)
    )
    assert [[button.text for button in row] for row in expanded.inline_keyboard] == [
        ["✅ First"],
        ["☑️ • C"],
        ["☑️ • D"],
        ["✅ Second"],
    ]


def test_optional_feedback_branches_gain_a_feedback_button(tmp_path: Path) -> None:
    view = _view(
        tmp_path,
        options=(
            _option("quiet", feedback="disabled"),
            _option("ask", feedback="optional"),
            _option("explain", feedback="required"),
        ),
        branches=(("quiet",), ("ask",), ("explain",)),
    )

    keyboard = render_gate_keyboard("gate0001", view, initial_progress(view))

    assert [[button.text for button in row] for row in keyboard.inline_keyboard] == [
        ["• Quiet", "• Ask"],
        ["💬 Ask with feedback"],
        ["• Explain"],
    ]
    assert [
        [button.callback_data for button in row] for row in keyboard.inline_keyboard
    ] == [
        ["gate:gate0001:c0", "gate:gate0001:c1"],
        ["gate:gate0001:f1"],
        ["gate:gate0001:c2"],
    ]


def test_expanded_group_feedback_button_tracks_the_current_selection(
    tmp_path: Path,
) -> None:
    view = _view(
        tmp_path,
        options=(
            _option("approve", feedback="disabled"),
            _option("annotate", feedback="optional", default_selected=False),
        ),
        branches=(("approve", "annotate"),),
        groups=(_group("approve", "annotate", label="Approve"),),
    )

    progress = initial_progress(view)
    without_feedback = render_gate_keyboard("gate0001", view, progress)
    assert [
        [button.text for button in row] for row in without_feedback.inline_keyboard
    ] == [
        ["☑️ • Approve"],
        ["⬜ • Annotate"],
        ["✅ Approve"],
    ]

    progress, _enabled = toggle_option(view, progress, "x1")
    with_feedback = render_gate_keyboard("gate0001", view, progress)
    assert [
        [button.text for button in row] for row in with_feedback.inline_keyboard
    ] == [
        ["☑️ • Approve"],
        ["☑️ • Annotate"],
        ["✅ Approve", "💬 Approve with feedback"],
    ]


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param(None, id="missing"),
        pytest.param("{not-json", id="invalid-json"),
        pytest.param("[]", id="wrong-shape"),
    ],
)
def test_malformed_progress_recovers_to_single_group_defaults(
    tmp_path: Path,
    contents: str | None,
) -> None:
    view = _view(
        tmp_path,
        options=(_option("approve"), _option("commit")),
        branches=(("approve", "commit"),),
        groups=(_group("approve", "commit"),),
    )
    if contents is not None:
        progress_path(view).write_text(contents, encoding="utf-8")

    loaded = load_progress(view, active_message_id=42, chat_id="chat-1")

    assert loaded == GateProgress(
        selected_option_ids=("approve", "commit"),
        expanded_branch_index=0,
        active_message_id=42,
        chat_id="chat-1",
    )


def test_stale_selected_options_are_filtered_without_losing_metadata(
    tmp_path: Path,
) -> None:
    view = _view(
        tmp_path,
        options=(_option("approve"), _option("commit")),
        branches=(("approve", "commit"),),
        groups=(_group("approve", "commit"),),
    )
    progress_path(view).write_text(
        json.dumps(
            {
                "selected_option_ids": ["removed", "commit"],
                "expanded_branch_index": 0,
                "active_message_id": 73,
                "chat_id": "saved-chat",
            }
        ),
        encoding="utf-8",
    )

    loaded = load_progress(view, active_message_id=99, chat_id="fallback-chat")

    assert loaded == GateProgress(
        selected_option_ids=("commit",),
        expanded_branch_index=0,
        active_message_id=73,
        chat_id="saved-chat",
    )


def test_toggle_uses_global_option_token_and_preserves_query_order(
    tmp_path: Path,
) -> None:
    view = _view(
        tmp_path,
        options=(_option("approve"), _option("commit")),
        branches=(("approve", "commit"),),
        groups=(_group("approve", "commit"),),
    )
    progress = initial_progress(view)

    progress, enabled = toggle_option(view, progress, "x0")
    assert enabled is False
    assert progress.selected_option_ids == ("commit",)
    progress, enabled = toggle_option(view, progress, "x0")
    assert enabled is True
    assert progress.selected_option_ids == ("approve", "commit")


def test_selection_feedback_contract_uses_strongest_mode(tmp_path: Path) -> None:
    view = _view(
        tmp_path,
        options=(
            _option("approve", feedback="optional"),
            _option(
                "feedback",
                feedback="required",
                input_schema={
                    "type": "object",
                    "required": ["feedback"],
                    "properties": {"feedback": {"type": "string"}},
                },
            ),
        ),
        branches=(("approve", "feedback"),),
        groups=(_group("approve", "feedback"),),
    )

    assert feedback_mode(view, ("approve", "feedback")) == "required"


def test_input_block_round_trips_through_save_and_load(tmp_path: Path) -> None:
    view = _view(
        tmp_path,
        options=(_option("approve"), _option("commit")),
        branches=(("approve", "commit"),),
        groups=(_group("approve", "commit"),),
    )
    progress = replace(
        initial_progress(view, active_message_id=42, chat_id="chat-1"),
        input_option_ids=("approve",),
        input_field_index=1,
        input_values={"note": "hello"},
        input_feedback_requested=True,
    )

    save_progress(view, progress)
    loaded = load_progress(view, active_message_id=42, chat_id="chat-1")

    assert loaded.input_option_ids == ("approve",)
    assert loaded.input_field_index == 1
    assert loaded.input_values == {"note": "hello"}
    assert loaded.input_feedback_requested is True


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"input_field_index": -1}, id="bad-index"),
        pytest.param({"input_option_ids": "approve"}, id="non-list-option-ids"),
        pytest.param({"input_option_ids": ["missing"]}, id="undeclared-option-id"),
    ],
)
def test_malformed_input_block_resets_while_leaving_branch_selection_intact(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    view = _view(
        tmp_path,
        options=(_option("approve"), _option("commit")),
        branches=(("approve", "commit"),),
        groups=(_group("approve", "commit"),),
    )
    payload = {
        "selected_option_ids": ["approve", "commit"],
        "expanded_branch_index": 0,
        "active_message_id": 42,
        "chat_id": "chat-1",
        "input_option_ids": ["approve"],
        "input_field_index": 0,
        "input_values": {},
        "input_feedback_requested": False,
    }
    payload.update(overrides)
    progress_path(view).write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_progress(view, active_message_id=42, chat_id="chat-1")

    assert loaded.input_option_ids == ()
    assert loaded.input_field_index is None
    assert loaded.input_values is None
    assert loaded.input_feedback_requested is False
    assert loaded.selected_option_ids == ("approve", "commit")
    assert loaded.expanded_branch_index == 0
