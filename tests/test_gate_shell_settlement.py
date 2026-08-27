"""A shell-backed gate answered from Telegram must settle its gate shell.

Regression coverage for the gap R6 of sase's ``gate-fork-cli`` phase found:
``inbound.resolve_gate_response`` called the shared executor directly with no
awareness of gate shells at all, so a shell gate answered from Telegram was
answered (``response.json`` written) but its family member stayed pending
forever and its recorded follow-up never launched -- exactly the bug already
fixed for the mobile bridge in ``sase``'s own
``_mobile_notification_actions.execute_mobile_gate_action``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sase.axe.run_agent_helpers_artifacts import update_meta_field
from sase.gate_shell.member import create_gate_shell_member
from sase.gate_shell.store import read_gate_shell_marker
from sase.notification_gates.model_shell import GateShellSpec
from sase.notification_gates.service import create_gate
from sase_telegram import inbound

from .test_custom_gates import gate_home

__all__ = ["gate_home"]

_ECHO_COMMAND = (
    "#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps({'status': 'ok'}))\n"
)


def _spec(request_id: str) -> dict[str, Any]:
    return {
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
        "shell": {},
    }


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


def test_telegram_answer_settles_a_gate_shell(gate_home: Path) -> None:
    del gate_home
    gate = create_gate(_spec("tg-shell-1"))
    artifacts_dir = _make_gate_shell_member("tg-shell-1", gate.bundle_path)

    action = {
        "action": "CustomGate",
        "action_data": {
            "request_id": "tg-shell-1",
            "request_kind": "custom",
            "bundle_path": str(gate.bundle_path),
        },
    }
    response = inbound.ResponseAction(
        action_type="gate",
        notif_id_prefix="tgshell1",
        response_path=gate.bundle_path / "response.json",
        response_data={},
        answer_text=None,
        selected_option_ids=("cleanup",),
    )

    message = inbound.resolve_gate_response(response, action)

    assert message == "Gate answered with cleanup"
    assert gate.response_path.is_file()
    record = read_gate_shell_marker("proj", artifacts_dir)
    assert record is not None
    assert record.gate_state == "answered"

    meta = json.loads((Path(artifacts_dir) / "agent_meta.json").read_text())
    assert meta["chat_path"]
    log_text = (Path(artifacts_dir) / "gate.log").read_text(encoding="utf-8")
    assert "$ commands/cleanup" in log_text


def test_telegram_answer_leaves_an_ordinary_gate_unaffected(gate_home: Path) -> None:
    """A non-shell gate keeps working exactly as before this fix."""
    del gate_home
    plain_spec = _spec("tg-plain-1")
    del plain_spec["shell"]
    gate = create_gate(plain_spec)

    action = {
        "action": "CustomGate",
        "action_data": {
            "request_id": "tg-plain-1",
            "request_kind": "custom",
            "bundle_path": str(gate.bundle_path),
        },
    }
    response = inbound.ResponseAction(
        action_type="gate",
        notif_id_prefix="tgplain1",
        response_path=gate.bundle_path / "response.json",
        response_data={},
        answer_text=None,
        selected_option_ids=("cleanup",),
    )

    message = inbound.resolve_gate_response(response, action)

    assert message == "Gate answered with cleanup"
    assert gate.response_path.is_file()
    assert not (gate.bundle_path / "gate.log").exists()
