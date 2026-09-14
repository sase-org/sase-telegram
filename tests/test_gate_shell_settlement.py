"""Telegram submits gate answers through the shared supervised proc.

Regression coverage for the gap R6 of sase's ``gate-fork-cli`` phase found:
``inbound.resolve_gate_response`` called the shared executor directly with no
awareness of gate shells at all, so a shell gate answered from Telegram was
answered (``response.json`` written) but its family member stayed pending
forever and its recorded follow-up never launched.

sase-zr.4 fixed this at the root, not by teaching Telegram more about gate
shells: Telegram no longer executes or settles gates itself at all. It
submits ``sase gate answer --id ... --kind ... --no-detach --json`` as a
supervised background proc, exactly the request ``sase gate answer
--detach`` already submits for a gate-shell-backed gate (see
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
from sase.gate_shell.member import create_gate_shell_member
from sase.notification_gates.model_shell import GateShellSpec
from sase.notification_gates.service import create_gate
from sase.notification_gates.models import GateError
from sase_telegram import inbound

from .test_custom_gates import gate_home

__all__ = ["gate_home"]

_ECHO_COMMAND = (
    "#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps({'status': 'ok'}))\n"
)


def _spec(request_id: str, *, shell: bool) -> dict[str, Any]:
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
    if shell:
        spec["shell"] = {}
    return spec


def _make_gate_shell_member(request_id: str, bundle_path: Path) -> str:
    shell = GateShellSpec.from_mapping(
        {"pending_status": "GATE", "settled_status": "GATED"},
        branches=(("cleanup",),),
    )
    artifacts_dir = create_gate_shell_member(
        "proj",
        {"name": "lane--0", "agent_family": "lane", "model": "gpt-5"},
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
        shell=shell,
    )
    update_meta_field(artifacts_dir, "gate_bundle_path", str(bundle_path))
    return artifacts_dir


def _mock_submit(monkeypatch: pytest.MonkeyPatch, proc_id: str = "proc-1") -> MagicMock:
    mock = MagicMock(return_value=MagicMock(proc_id=proc_id))
    monkeypatch.setattr("sase.procs.service.submit_proc_request", mock)
    return mock


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


def test_telegram_submits_a_shell_backed_gate(
    gate_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram submits the shared proc; it never touches the gate shell."""
    del gate_home
    mock = _mock_submit(monkeypatch)
    gate = create_gate(_spec("tg-shell-1", shell=True))
    _make_gate_shell_member("tg-shell-1", gate.bundle_path)

    response = inbound.ResponseAction(
        action_type="gate",
        notif_id_prefix="tgshell1",
        response_path=gate.bundle_path / "response.json",
        response_data={},
        answer_text=None,
        selected_option_ids=("cleanup",),
    )

    message = inbound.resolve_gate_response(
        response, _action("tg-shell-1", gate.bundle_path)
    )

    assert message == "Gate answer submitted (cleanup)"
    mock.assert_called_once()
    submitted = mock.call_args.args[0]
    assert submitted.argv == [
        "sase",
        "gate",
        "answer",
        "--id",
        "tg-shell-1",
        "--kind",
        "custom",
        "--no-detach",
        "--json",
    ]
    assert submitted.operation_payload == {"option_ids": ["cleanup"]}
    # Execution (and settlement) has not happened yet -- it happens inside
    # the submitted proc, not this call.
    assert not gate.response_path.exists()


def test_telegram_submits_an_ordinary_gate_identically(
    gate_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-shell gate submits through the exact same path -- no branching."""
    del gate_home
    mock = _mock_submit(monkeypatch)
    gate = create_gate(_spec("tg-plain-1", shell=False))

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
        "sase",
        "gate",
        "answer",
        "--id",
        "tg-plain-1",
        "--kind",
        "custom",
        "--no-detach",
        "--json",
    ]
    assert not gate.response_path.exists()


def test_telegram_rejects_a_gate_already_answered(
    gate_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale tap on an already-answered gate never spawns a proc."""
    del gate_home
    mock = _mock_submit(monkeypatch)
    gate = create_gate(_spec("tg-answered-1", shell=False))
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
