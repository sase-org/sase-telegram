"""Renamed sase gate-turn APIs with legacy gate-shell fallback.

sase renamed gate-shell to gate-turn (runtime cutover), but telegram's
``sase>=`` floor predates the rename. Resolve the new module and names
first and fall back to the legacy ones, so the suite passes against both
the installed sase release and sase master. Test modules import the
gate-turn vocabulary from here and never touch ``sase.gate_shell`` or
``sase.notification_gates.model_shell`` directly.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

try:
    from sase.gate_turn.member import create_gate_turn_member
    from sase.notification_gates.model_turn import GateTurnSpec

    TURN_SPEC_KEY = "turn"
    TURN_ROW_MANAGED_FIELD = "turn_row_managed"
    MEMBER_SPEC_KWARG = "turn"
except ImportError:
    from sase.gate_shell.member import (
        create_gate_shell_member as create_gate_turn_member,
    )
    from sase.notification_gates.model_shell import (
        GateShellSpec as GateTurnSpec,
    )

    TURN_SPEC_KEY = "shell"
    TURN_ROW_MANAGED_FIELD = "shell_row_managed"
    MEMBER_SPEC_KWARG = "shell"

__all__ = [
    "MEMBER_SPEC_KWARG",
    "TURN_ROW_MANAGED_FIELD",
    "TURN_SPEC_KEY",
    "GateTurnSpec",
    "create_gate_turn_member",
    "gate_turn_creation_of",
    "make_gate_turn_member",
    "mark_turn_row_managed",
]


def mark_turn_row_managed(spec: Any) -> Any:
    """Mark a ``GateSpec`` as owning its own gate-turn member row.

    Only the gate-turn transaction (after it registers the member row) or
    tests that establish their own rows set this; the production guard
    refuses rowless turn-block custom gates without it.
    """
    return replace(spec, **{TURN_ROW_MANAGED_FIELD: True})


def make_gate_turn_member(*args: Any, turn_spec: Any, **kwargs: Any) -> str:
    """Create the gate-turn member row, spelling the spec kwarg per sase vintage."""
    return create_gate_turn_member(*args, **kwargs, **{MEMBER_SPEC_KWARG: turn_spec})


def gate_turn_creation_of(result: Any) -> Any:
    """Read the gate-turn creation off a launch-request result, either vintage."""
    if hasattr(result, "gate_turn_creation"):
        return result.gate_turn_creation
    return result.gate_shell_creation
