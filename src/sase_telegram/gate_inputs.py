"""Pure decisions for the Telegram declared-input step flow.

Wraps the shared :mod:`sase.notification_gates.input_collection` helpers with
the per-step state a chat transport needs -- which field is next, how a
typed reply or button tap changes the collected values, and the compact
callback tokens that address one step. No Telegram API calls, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from sase.notification_gates.input_collection import (
    coerce_field_text,
    collected_input_fields,
    option_inputs_from_values,
)
from sase.notification_gates.models import GateInputField, GateOption

from sase_telegram.gate_flow import GateProgress, GateView, option_for_id

_INPUT_TOKEN_RE = re.compile(r"^i(\d+)(k|d|c|v\d+)$")


@dataclass(frozen=True)
class GateInputStep:
    """One declared input field awaiting an answer."""

    field: GateInputField
    index: int  # 0-based position in the collected field list
    total: int

    @property
    def position(self) -> int:
        """1-based position for display, e.g. "Input 2/3"."""
        return self.index + 1


def _resolved_options(
    view: GateView, option_ids: Sequence[str]
) -> tuple[GateOption, ...]:
    resolved = (option_for_id(view, option_id) for option_id in option_ids)
    return tuple(option for option in resolved if option is not None)


def pending_fields(
    view: GateView, option_ids: Sequence[str]
) -> tuple[GateInputField, ...]:
    """Return the declared input fields for one gate selection, deduped."""
    return collected_input_fields(_resolved_options(view, option_ids))


def unsupported_fields(
    fields: Sequence[GateInputField],
) -> tuple[GateInputField, ...]:
    """Return the fields Telegram cannot collect (``secret`` inputs)."""
    return tuple(field for field in fields if field.secret)


def begin_input(
    progress: GateProgress,
    option_ids: Sequence[str],
    *,
    feedback_requested: bool,
) -> GateProgress:
    """Open the input block for a selection the reviewer already committed to."""
    return replace(
        progress,
        input_option_ids=tuple(option_ids),
        input_field_index=0,
        input_values={},
        input_feedback_requested=feedback_requested,
    )


def clear_input(progress: GateProgress) -> GateProgress:
    """Close the input block, leaving the gate's own selection untouched."""
    return replace(
        progress,
        input_option_ids=(),
        input_field_index=None,
        input_values=None,
        input_feedback_requested=False,
    )


def current_step(view: GateView, progress: GateProgress) -> GateInputStep | None:
    """Return the field awaiting an answer, or ``None`` when collection is done."""
    if progress.input_field_index is None:
        return None
    fields = pending_fields(view, progress.input_option_ids)
    if not (0 <= progress.input_field_index < len(fields)):
        return None
    return GateInputStep(
        field=fields[progress.input_field_index],
        index=progress.input_field_index,
        total=len(fields),
    )


def advance(progress: GateProgress) -> GateProgress:
    """Move past the current field to the next one."""
    if progress.input_field_index is None:
        raise ValueError("no input step is active")
    return replace(progress, input_field_index=progress.input_field_index + 1)


def apply_text_answer(
    values: Mapping[str, Any], field: GateInputField, text: str
) -> dict[str, Any]:
    """Return ``values`` with one typed text reply converted and recorded.

    Raises:
        XPromptValidationError: If ``text`` cannot be converted to the
            field's declared type.
    """
    converted = coerce_field_text(field, text)
    return {**values, field.id: converted}


def apply_choice(
    values: Mapping[str, Any], field: GateInputField, value: str
) -> tuple[dict[str, Any], bool]:
    """Apply one enum keyboard choice. Returns ``(values, selected_now)``.

    A scalar enum sets the value and always returns ``True``. A
    ``repeatable`` enum toggles membership of a list kept in declared choice
    order and returns whether the tapped choice ended up selected.
    """
    if not field.repeatable:
        return {**values, field.id: value}, True
    current = set(values.get(field.id, []))
    if value in current:
        current.discard(value)
        selected_now = False
    else:
        current.add(value)
        selected_now = True
    ordered = [choice.value for choice in field.choices if choice.value in current]
    return {**values, field.id: ordered}, selected_now


def skip_step(values: Mapping[str, Any], field: GateInputField) -> dict[str, Any]:
    """Return ``values`` with ``field``'s id absent."""
    result = dict(values)
    result.pop(field.id, None)
    return result


def submitted_option_inputs(
    view: GateView, progress: GateProgress
) -> dict[str, dict[str, Any]]:
    """Distribute collected values to each selected option's declared ids."""
    options = _resolved_options(view, progress.input_option_ids)
    return option_inputs_from_values(options, progress.input_values or {})


def encode_input_token(index: int, verb: str) -> str:
    """Encode one input-step callback token: ``i<field_index><verb>``."""
    return f"i{index}{verb}"


def decode_input_token(token: str) -> tuple[int, str] | None:
    """Decode an ``i<field_index><verb>`` token, or ``None`` if malformed."""
    match = _INPUT_TOKEN_RE.fullmatch(token)
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


__all__ = [
    "GateInputStep",
    "advance",
    "apply_choice",
    "apply_text_answer",
    "begin_input",
    "clear_input",
    "current_step",
    "decode_input_token",
    "encode_input_token",
    "pending_fields",
    "skip_step",
    "submitted_option_inputs",
    "unsupported_fields",
]
