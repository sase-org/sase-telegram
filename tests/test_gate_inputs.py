"""Tests for the pure declared-input step-flow decision layer."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sase.notification_gates.models import GateError, GateOption
from sase.xprompt.models import XPromptValidationError

from sase_telegram.gate_flow import GateProgress, GateView
from sase_telegram.gate_inputs import (
    advance,
    apply_choice,
    apply_text_answer,
    begin_input,
    clear_input,
    current_step,
    decode_input_token,
    encode_input_token,
    pending_fields,
    skip_step,
    submitted_option_inputs,
    unsupported_fields,
)


def _option(
    option_id: str, *, inputs: tuple[dict[str, object], ...] = ()
) -> GateOption:
    return GateOption.from_mapping(
        {
            "id": option_id,
            "label": option_id.replace("_", " ").title(),
            "command": {"argv": [f"commands/{option_id}"]},
            "inputs": list(inputs),
        },
        0,
    )


def _view(tmp_path: Path, *, options: tuple[GateOption, ...]) -> GateView:
    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir(exist_ok=True)
    branch = tuple(option.id for option in options)
    return GateView(
        bundle_path=bundle_path,
        request_id="gate-inputs-test",
        kind="custom",
        options=options,
        groups=(),
        branches=(branch,),
    )


_LINE_FIELD = {"id": "line", "label": "Line", "type": "line", "required": True}
_NOTE_FIELD = {
    "id": "note",
    "label": "Note",
    "type": "text",
    "required": False,
    "placeholder": "e.g. context",
}
_TAGS_FIELD = {
    "id": "tags",
    "label": "Tags",
    "type": "line",
    "required": False,
    "repeatable": True,
}
_COUNT_FIELD = {"id": "count", "label": "Count", "type": "int", "required": True}
_COLOR_FIELD = {
    "id": "color",
    "label": "Color",
    "type": "enum",
    "required": True,
    "choices": ["red", "green", "blue"],
}
_COLORS_FIELD = {
    "id": "colors",
    "label": "Colors",
    "type": "enum",
    "required": False,
    "repeatable": True,
    "choices": ["red", "green", "blue"],
}
_SECRET_FIELD = {
    "id": "token",
    "label": "Token",
    "type": "word",
    "required": True,
    "secret": True,
}


def test_pending_fields_dedupes_and_orders_across_options(tmp_path: Path) -> None:
    proceed = _option("proceed", inputs=(_LINE_FIELD, _NOTE_FIELD))
    audit = _option("audit", inputs=(_NOTE_FIELD, _COUNT_FIELD))
    view = _view(tmp_path, options=(proceed, audit))

    fields = pending_fields(view, ("proceed", "audit"))

    assert [field.id for field in fields] == ["line", "note", "count"]


def test_pending_fields_raises_on_conflicting_redeclaration(tmp_path: Path) -> None:
    proceed = _option("proceed", inputs=(_COUNT_FIELD,))
    conflicting_count = {**_COUNT_FIELD, "type": "line"}
    audit = _option("audit", inputs=(conflicting_count,))
    view = _view(tmp_path, options=(proceed, audit))

    with pytest.raises(GateError, match="declared differently"):
        pending_fields(view, ("proceed", "audit"))


def test_unsupported_fields_picks_out_secret(tmp_path: Path) -> None:
    option = _option("proceed", inputs=(_LINE_FIELD, _SECRET_FIELD))
    view = _view(tmp_path, options=(option,))

    fields = pending_fields(view, ("proceed",))

    assert [field.id for field in unsupported_fields(fields)] == ["token"]


@pytest.mark.parametrize(
    ("index", "verb"),
    [
        (0, "k"),
        (1, "d"),
        (2, "c"),
        (3, "v0"),
        (12, "v7"),
    ],
)
def test_input_token_round_trip(index: int, verb: str) -> None:
    token = encode_input_token(index, verb)
    assert decode_input_token(token) == (index, verb)


@pytest.mark.parametrize(
    "token",
    ["", "i", "ik", "i-1k", "i1", "i1x", "gate:i1k", "1k"],
)
def test_decode_input_token_returns_none_for_malformed(token: str) -> None:
    assert decode_input_token(token) is None


def test_apply_text_answer_converts_each_scalar_type() -> None:
    values: dict[str, object] = {}
    values = apply_text_answer(values, _field(_LINE_FIELD), "hello")
    assert values["line"] == "hello"
    values = apply_text_answer(values, _field(_COUNT_FIELD), "5")
    assert values["count"] == 5

    bool_field = _field({"id": "ok", "label": "OK", "type": "bool"})
    values = apply_text_answer(values, bool_field, "yes")
    assert values["ok"] is True

    float_field = _field({"id": "ratio", "label": "Ratio", "type": "float"})
    values = apply_text_answer(values, float_field, "1.5")
    assert values["ratio"] == 1.5


def test_apply_text_answer_raises_on_bad_int() -> None:
    with pytest.raises(XPromptValidationError):
        apply_text_answer({}, _field(_COUNT_FIELD), "not-a-number")


def test_apply_text_answer_splits_repeatable_field_dropping_blank_lines() -> None:
    values = apply_text_answer({}, _field(_TAGS_FIELD), "alpha\n\nbeta\n")
    assert values["tags"] == ["alpha", "beta"]


def test_apply_choice_sets_scalar_enum_value() -> None:
    values, selected_now = apply_choice({}, _field(_COLOR_FIELD), "green")
    assert values == {"color": "green"}
    assert selected_now is True


def test_apply_choice_toggles_repeatable_enum_off_in_declared_order() -> None:
    field = _field(_COLORS_FIELD)
    values, _ = apply_choice({}, field, "blue")
    values, _ = apply_choice(values, field, "red")
    assert values["colors"] == ["red", "blue"]

    values, selected_now = apply_choice(values, field, "red")
    assert values["colors"] == ["blue"]
    assert selected_now is False


def test_skip_step_leaves_id_absent() -> None:
    values = {"line": "hello", "note": "context"}
    result = skip_step(values, _field(_NOTE_FIELD))
    assert result == {"line": "hello"}


def test_submitted_option_inputs_distributes_declared_ids_only(
    tmp_path: Path,
) -> None:
    proceed = _option("proceed", inputs=(_LINE_FIELD,))
    verify = _option("verify")
    view = _view(tmp_path, options=(proceed, verify))
    progress = begin_input(
        GateProgress(), ("proceed", "verify"), feedback_requested=False
    )
    progress = replace(progress, input_values={"line": "hello"})

    result = submitted_option_inputs(view, progress)

    assert result == {"proceed": {"line": "hello"}, "verify": {}}


def test_begin_current_advance_walk_declared_fields(tmp_path: Path) -> None:
    option = _option("proceed", inputs=(_LINE_FIELD, _COUNT_FIELD))
    view = _view(tmp_path, options=(option,))
    progress = begin_input(GateProgress(), ("proceed",), feedback_requested=False)

    first = current_step(view, progress)
    assert first is not None
    assert first.field.id == "line"
    assert first.position == 1
    assert first.total == 2

    progress = advance(progress)
    second = current_step(view, progress)
    assert second is not None
    assert second.field.id == "count"
    assert second.position == 2

    progress = advance(progress)
    assert current_step(view, progress) is None


def test_clear_input_resets_only_input_fields() -> None:
    progress = replace(
        GateProgress(), selected_option_ids=("proceed",), expanded_branch_index=0
    )
    progress = begin_input(progress, ("proceed",), feedback_requested=True)

    cleared = clear_input(progress)

    assert cleared.input_option_ids == ()
    assert cleared.input_field_index is None
    assert cleared.input_values is None
    assert cleared.input_feedback_requested is False
    assert cleared.selected_option_ids == ("proceed",)
    assert cleared.expanded_branch_index == 0


def _field(raw: dict[str, object]):
    return GateOption.from_mapping(
        {
            "id": "holder",
            "label": "Holder",
            "command": {"argv": ["commands/holder"]},
            "inputs": [raw],
        },
        0,
    ).inputs[0]
