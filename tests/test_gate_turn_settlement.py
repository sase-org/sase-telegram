"""Telegram submits gate answers through the shared supervised proc.

Regression coverage for the gap R6 of sase's ``gate-fork-cli`` phase found:
``inbound.resolve_gate_response`` called the shared executor directly with no
awareness of gate turns at all, so a turn-backed gate answered from Telegram was
answered (``response.json`` written) but its session member stayed pending
forever and its recorded follow-up never launched.

sase-zr.4 fixed this at the root, not by teaching Telegram more about gate
turns: Telegram no longer executes or settles gates itself at all. It
submits ``sase gate answer --id ... --kind ... --no-detach --json`` as a
supervised background proc, exactly the request ``sase gate answer
--detach`` already submits for a gate-turn-backed gate (see
``test_gate_cli_answer_detach.py`` in sase). Settlement is the reinvoked
CLI process's own, already-tested responsibility, identical regardless of
which surface submitted the answer -- so Telegram's own tests only need to
confirm the submission is built correctly, not re-verify settlement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from sase.axe.run_agent_helpers_artifacts import update_meta_field
from sase.notification_gates.models import GateError, GateSpec
from sase.notification_gates.service import create_gate
from sase_telegram import inbound

from .gate_turn_compat import (
    TURN_SPEC_KEY,
    GateTurnSpec,
    make_gate_turn_member,
    mark_turn_row_managed,
)
from .test_custom_gates import gate_home

__all__ = ["gate_home"]

_ECHO_COMMAND = (
    "#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps({'status': 'ok'}))\n"
)


def _spec(request_id: str, *, turn: bool) -> dict[str, Any] | GateSpec:
    spec: dict[str, Any] = {
        "schema_version": 3,
        "request_id": request_id,
        "kind": "custom",
        "producer": {"agent": "test"},
        "payload": {},
        "presentation": {"icon": "🧪", "title": "T", "notes": ["n"]},
        "query": "cleanup",
        "primary_branch": ["cleanup"],
        "options": [
            {
                "id": "cleanup",
                "label": "Clean up",
                "command": {"argv": ["commands/cleanup"]},
            }
        ],
        "resources": [
            {"path": "commands/cleanup", "role": "command", "content": _ECHO_COMMAND}
        ],
    }
    if turn:
        spec[TURN_SPEC_KEY] = {}
        # This test establishes the gate-turn row itself with
        # ``_make_gate_turn_member``: mark the spec the way the production
        # transaction does so the turn-row guard accepts the setup.
        return mark_turn_row_managed(GateSpec.from_mapping(spec))
    return spec


def _make_gate_turn_member(request_id: str, bundle_path: Path) -> str:
    turn = GateTurnSpec.from_mapping(
        {"pending_status": "GATE", "settled_status": "GATED"},
        branches=(("cleanup",),),
    )
    artifacts_dir = make_gate_turn_member(
        "proj",
        {"name": "lane--0", "agent_session": "lane", "model": "gpt-5"},
        lane="lane",
        suffix="--gate",
        prev_artifacts_timestamp="20260812120000",
        workspace_num=None,
        gate_id=request_id,
        gate_kind="custom",
        label="T",
        reason="wait for reviewer",
        creator_agent="lane--0",
        timeout_seconds=86400.0,
        request_fingerprint=None,
        turn_spec=turn,
    )
    update_meta_field(artifacts_dir, "gate_bundle_path", str(bundle_path))
    return artifacts_dir


def _mock_submit(monkeypatch: pytest.MonkeyPatch, proc_id: str = "proc-1") -> MagicMock:
    mock = MagicMock(return_value=MagicMock(proc_id=proc_id))
    monkeypatch.setattr("sase.procs.service.submit_proc_request", mock)
    return mock


def _mock_sase_cli(monkeypatch: pytest.MonkeyPatch) -> str:
    path = "/venv/bin/sase"
    monkeypatch.setattr(
        "sase_telegram.inbound.resolve_console_script", lambda _name: path
    )
    return path


def _action(request_id: str, bundle_path: Path) -> dict[str, Any]:
    return {
        "action": "CustomGate",
        "action_data": {
            "request_id": request_id,
            "request_kind": "custom",
            "bundle_path": str(bundle_path),
        },
        "chat_id": "chat-1",
        "message_id": 7,
    }


def test_telegram_submits_a_turn_backed_gate(
    gate_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram submits the shared proc; it never touches the gate turn."""
    del gate_home
    sase_cli = _mock_sase_cli(monkeypatch)
    mock = _mock_submit(monkeypatch)
    gate = create_gate(_spec("tg-turn-1", turn=True))
    _make_gate_turn_member("tg-turn-1", gate.bundle_path)

    response = inbound.ResponseAction(
        action_type="gate",
        notif_id_prefix="tgturn1",
        response_path=gate.bundle_path / "response.json",
        response_data={},
        answer_text=None,
        selected_option_ids=("cleanup",),
    )

    message = inbound.resolve_gate_response(
        response, _action("tg-turn-1", gate.bundle_path)
    )

    assert message == "Gate answer submitted (cleanup)"
    mock.assert_called_once()
    submitted = mock.call_args.args[0]
    assert submitted.argv == [
        sase_cli,
        "gate",
        "answer",
        "--id",
        "tg-turn-1",
        "--kind",
        "custom",
        "--no-detach",
        "--json",
    ]
    assert submitted.operation_payload == {
        "option_ids": ["cleanup"],
        "source": "telegram",
    }
    # Execution (and settlement) has not happened yet -- it happens inside
    # the submitted proc, not this call.
    assert not gate.response_path.exists()


def test_telegram_submits_an_ordinary_gate_identically(
    gate_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-turn gate submits through the exact same path -- no branching."""
    del gate_home
    sase_cli = _mock_sase_cli(monkeypatch)
    mock = _mock_submit(monkeypatch)
    gate = create_gate(_spec("tg-plain-1", turn=False))

    response = inbound.ResponseAction(
        action_type="gate",
        notif_id_prefix="tgplain1",
        response_path=gate.bundle_path / "response.json",
        response_data={},
        answer_text=None,
        selected_option_ids=("cleanup",),
    )

    message = inbound.resolve_gate_response(
        response, _action("tg-plain-1", gate.bundle_path)
    )

    assert message == "Gate answer submitted (cleanup)"
    mock.assert_called_once()
    submitted = mock.call_args.args[0]
    assert submitted.argv == [
        sase_cli,
        "gate",
        "answer",
        "--id",
        "tg-plain-1",
        "--kind",
        "custom",
        "--no-detach",
        "--json",
    ]
    assert submitted.operation_payload["source"] == "telegram"
    assert not gate.response_path.exists()


def test_telegram_rejects_a_gate_already_answered(
    gate_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale tap on an already-answered gate never spawns a proc."""
    del gate_home
    mock = _mock_submit(monkeypatch)
    gate = create_gate(_spec("tg-answered-1", turn=False))
    gate.response_path.write_text("{}", encoding="utf-8")

    response = inbound.ResponseAction(
        action_type="gate",
        notif_id_prefix="tganswered1",
        response_path=gate.response_path,
        response_data={},
        answer_text=None,
        selected_option_ids=("cleanup",),
    )

    with pytest.raises(GateError, match="gate is already answered") as excinfo:
        inbound.resolve_gate_response(
            response, _action("tg-answered-1", gate.bundle_path)
        )
    assert excinfo.value.code == "already_answered"
    mock.assert_not_called()
