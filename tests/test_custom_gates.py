"""Telegram gate formatting, progress, and executor integration tests."""

from __future__ import annotations

import argparse
from datetime import datetime
from inspect import signature
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sase.agent.launch_request import create_launch_approval_request
from sase.bead.flag_fields import FlagFields
from sase.bead.flag_gate import create_flag_triage_gate
from sase.bead.model import SnoozeRecord
from sase.bead.snooze_gate import create_bead_snooze_gate
from sase.bead.stale_cleanup_gate import create_bead_stale_cleanup_gate
from sase.bead.task_gate import create_task_triage_gate
from sase.notification_gates.models import GateError
from sase.notification_gates.registry import (
    adapter_for_kind,
    registered_gate_kinds,
)
from sase.notification_gates.service import create_gate
from sase.notifications.models import Notification
from sase.notifications.store import load_notifications
from sase.plan_gate import create_plan_approval_gate
from sase.plugins.required_gate import create_plugins_required_gate
from sase.sdd.plan_validate import validate_plan as validate_sase_plan
from sase_telegram import inbound, outbound, pending_actions
from sase_telegram.formatting import format_notification
from sase_telegram.gate_flow import GateProgress
from sase_telegram.scripts.sase_tg_inbound import (
    _handle_callback,
    _handle_gate_callback,
    _handle_text_message,
)
from sase_telegram.scripts.sase_tg_outbound import _run_outbound

VALID_TALE_PLAN = """---
tier: tale
title: Telegram plan approval
goal: Verify Telegram option selection
---
# Plan

Implement the requested change.
"""

VALID_EPIC_PLAN = """---
tier: epic
title: Telegram epic approval
goal: Verify Telegram renders command-backed epic gates
phases:
  - id: implementation
    title: Implement the requested change
    depends_on: []
---
# Plan

Implement the requested change.
"""


def _epic_plan_for_installed_sase() -> str:
    if "mode" not in signature(validate_sase_plan).parameters:
        return VALID_EPIC_PLAN
    return VALID_EPIC_PLAN.replace(
        "    depends_on: []\n",
        "    depends_on: []\n    size: small\n",
    )


@pytest.fixture()
def gate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from sase.notification_gates import paths
    from sase.notifications import pending_actions as core_pending
    from sase.notifications import store

    monkeypatch.setattr(paths, "INTERACTION_REQUESTS_DIR", tmp_path / "requests")
    monkeypatch.setattr(store, "NOTIFICATIONS_DIR", str(tmp_path / "notifications"))
    monkeypatch.setattr(
        store,
        "NOTIFICATIONS_FILE",
        str(tmp_path / "notifications" / "notifications.jsonl"),
    )
    monkeypatch.setattr(core_pending, "PENDING_ACTIONS_PATH", tmp_path / "core.json")
    monkeypatch.setattr(
        core_pending,
        "LEGACY_TELEGRAM_PENDING_ACTIONS_PATH",
        tmp_path / "telegram.json",
    )
    monkeypatch.setattr(
        pending_actions, "PENDING_ACTIONS_PATH", tmp_path / "telegram.json"
    )
    monkeypatch.setattr(inbound, "AWAITING_FEEDBACK_PATH", tmp_path / "awaiting.json")
    store._LOAD_CACHE.clear()
    return tmp_path


def _command_script(result: str) -> str:
    return (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        f"print(json.dumps({{'status': {result!r}}}))\n"
    )


def _custom_spec(
    *, request_id: str = "telegram-custom", feedback: str = "optional"
) -> dict[str, object]:
    option_records = (
        ("proceed", "Proceed safely", "✅", True, feedback, "proceeded"),
        ("audit", "Write audit record", "📝", True, "optional", "audited"),
        ("verify", "Verify health", "🩺", False, "optional", "healthy"),
        ("cancel", "Cancel", "❌", True, "disabled", "cancelled"),
    )
    return {
        "schema_version": 3,
        "request_id": request_id,
        "kind": "custom",
        "producer": {"agent": "telegram-test"},
        "payload": {"operation": "restart"},
        "presentation": {
            "title": "Restart the guarded service?",
            "icon": "🛡️",
            "sender": "safety-agent",
            "notes": ["Restart the guarded service?"],
            "files": ["preview.md"],
        },
        "query": "(proceed AND audit AND verify) OR cancel",
        "primary_branch": ["proceed", "audit", "verify"],
        "options": [
            {
                "id": option_id,
                "label": label,
                "icon": icon,
                "default_selected": default_selected,
                "feedback": feedback_mode,
                "command": {"argv": [f"commands/{option_id}"]},
                "input_schema": {"type": "object"},
                "result_schema": {"type": "object"},
            }
            for (
                option_id,
                label,
                icon,
                default_selected,
                feedback_mode,
                _result,
            ) in option_records
        ],
        "groups": [
            {
                "options": ["proceed", "audit", "verify"],
                "label": "Proceed safely",
                "icon": "✅",
            }
        ],
        "resources": [
            {
                "path": f"commands/{option_id}",
                "role": "command",
                "content": _command_script(result),
            }
            for option_id, _label, _icon, _selected, _feedback, result in option_records
        ]
        + [
            {
                "path": "preview.md",
                "role": "preview",
                "content": "# Guarded restart\n",
            }
        ],
        "auto": False,
    }


def _hitl_spec() -> dict[str, object]:
    return {
        "schema_version": 3,
        "request_id": "telegram-hitl",
        "kind": "hitl",
        "producer": {"agent": "telegram-test"},
        "payload": {"step_name": "review", "output": {"ok": True}},
        "presentation": {"notes": ["Review workflow output"]},
        "query": "accept",
        "primary_branch": ["accept"],
        "options": [
            {
                "id": "accept",
                "label": "Accept",
                "icon": "✅",
                "command": {"argv": ["commands/accept"]},
                "input_schema": {"type": "object"},
                "result_schema": {"type": "object"},
            }
        ],
        "resources": [
            {
                "path": "commands/accept",
                "role": "command",
                "content": _command_script("accepted"),
            }
        ],
        "auto": False,
    }


def _stale_cleanup_bead() -> dict[str, object]:
    return {
        "project": "sase",
        "bead_id": "sase-task.1",
        "title": "Follow up on cache invalidation",
        "created_at": "2026-08-01T09:14:02-04:00",
        "plus_one_count": 0,
        "size": "small",
    }


def _missing_plugin_entry() -> dict[str, str]:
    return {
        "requirement": "sase-github",
        "name": "sase-github",
        "kind": "missing",
        "install_command": "sase plugin install sase-github",
        "message": (
            "required plugin `sase-github` is not installed; "
            "run `sase plugin install sase-github`"
        ),
    }


def _notification(result: object, *, action: str, sender: str) -> Notification:
    bundle_path = Path(result.bundle_path)
    request = json.loads((bundle_path / "request.json").read_text(encoding="utf-8"))
    action_data = dict(request.get("presentation", {}).get("action_data", {}))
    action_data.update(
        {
            "request_id": str(request["request_id"]),
            "request_kind": str(request["kind"]),
            "bundle_path": str(bundle_path),
            "request_path": str(bundle_path / "request.json"),
            "response_path": str(bundle_path / "response.json"),
            "response_dir": str(bundle_path),
            "artifacts_dir": str(bundle_path),
        }
    )
    return Notification(
        id=str(result.notification_id),
        timestamp="2026-07-17T00:00:00+00:00",
        sender=sender,
        icon="🛡️" if action == "CustomGate" else None,
        notes=["N" * 700],
        files=[str(bundle_path / "preview.md")]
        if (bundle_path / "preview.md").exists()
        else [],
        action=action,
        action_data=action_data,
    )


def _pending(notification: Notification) -> dict[str, object]:
    return {
        "notification_id": notification.id,
        "action": notification.action,
        "action_data": notification.action_data,
        "message_id": 42,
        "chat_id": "chat-1",
    }


def _stored_notification(notification_id: str | None) -> Notification:
    assert notification_id is not None
    return next(
        notification
        for notification in load_notifications(include_dismissed=True)
        if notification.id == notification_id
    )


def _epic_notification(gate_home: Path, request_id: str) -> Notification:
    plan_file = gate_home / f"{request_id}.md"
    plan_file.write_text(_epic_plan_for_installed_sase(), encoding="utf-8")
    result = create_plan_approval_gate(plan_file, request_id)
    notification = _notification(result, action="EpicApproval", sender="epic")
    notification.files = [str(Path(result.bundle_path) / "plan.md")]
    notification.dismissed = True
    return notification


def _callback(data: str, callback_id: str = "callback") -> SimpleNamespace:
    return SimpleNamespace(
        id=callback_id,
        data=data,
        message=SimpleNamespace(message_id=42, chat_id="chat-1"),
    )


def _button_data(keyboard: object) -> list[str]:
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


def test_custom_gate_renders_expanded_group_with_compact_callbacks_and_fallback(
    gate_home: Path,
) -> None:
    result = create_gate(_custom_spec())
    notification = _notification(result, action="CustomGate", sender="safety-agent")

    text, keyboard, attachments = format_notification(notification)

    assert text.startswith("🛡️ *Custom Request*")
    assert "**>" in text
    assert keyboard is not None
    assert [[button.text for button in row] for row in keyboard.inline_keyboard] == [
        ["☑️ ✅ Proceed safely"],
        ["☑️ 📝 Write audit record"],
        ["⬜ 🩺 Verify health"],
        ["✅ Proceed safely", "💬 Proceed safely with feedback"],
        ["❌ Cancel"],
    ]
    prefix = notification.id[:8]
    assert _button_data(keyboard) == [
        f"gate:{prefix}:x0",
        f"gate:{prefix}:x1",
        f"gate:{prefix}:x2",
        f"gate:{prefix}:s0",
        f"gate:{prefix}:f0",
        f"gate:{prefix}:c1",
    ]
    assert attachments == notification.files

    notification.action_data["request_id"] = "missing"
    notification.action_data["bundle_path"] = str(gate_home / "missing")
    _, fallback_keyboard, _ = format_notification(notification)
    assert fallback_keyboard is None


def test_task_triage_outbound_renders_tracks_attaches_and_launches(
    gate_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = create_task_triage_gate(
        request_id="telegram-task-launch",
        bead_id="sase-test",
        project="sase",
        title="Investigate the registry",
        description="Keep every Telegram gate driven by the adapter registry.",
        notes="Created by the Telegram regression test.",
    )
    notification = _stored_notification(gate.notification_id)
    last_sent_file = gate_home / "last-sent-task-triage"
    last_sent_file.write_text("0", encoding="utf-8")
    monkeypatch.setattr(outbound, "LAST_SENT_FILE", last_sent_file)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_outbound.get_unsent_notifications",
            return_value=[notification],
        ),
        patch(
            "sase_telegram.scripts.sase_tg_outbound.get_chat_id",
            return_value="chat-1",
        ),
        patch(
            "sase_telegram.scripts.sase_tg_outbound.rate_limit.check_rate_limit",
            return_value=True,
        ),
        patch("sase_telegram.scripts.sase_tg_outbound.rate_limit.record_send"),
        patch(
            "sase_telegram.scripts.sase_tg_outbound.send_message",
            return_value=SimpleNamespace(message_id=42),
        ) as send_message,
        patch("sase_telegram.scripts.sase_tg_outbound.md_to_pdf", return_value=None),
        patch("sase_telegram.scripts.sase_tg_outbound.send_document") as send_document,
        patch("sase_telegram.scripts.sase_tg_outbound._register_shared_transport"),
    ):
        assert _run_outbound(argparse.Namespace(dry_run=False)) == 0

    text = send_message.call_args.args[1]
    keyboard = send_message.call_args.kwargs["reply_markup"]
    assert text.startswith("✦ *Task Triage*")
    assert "Keep every Telegram gate driven" in text
    assert [[button.text for button in row] for row in keyboard.inline_keyboard] == [
        ["🚀 Launch"],
        ["💬 Launch with feedback"],
        ["✕ Close", "◈ Snooze"],
        ["💬 Snooze with feedback"],
    ]
    prefix = notification.id[:8]
    assert _button_data(keyboard) == [
        f"gate:{prefix}:c0",
        f"gate:{prefix}:f0",
        f"gate:{prefix}:c1",
        f"gate:{prefix}:c2",
        f"gate:{prefix}:f2",
    ]
    assert notification.files == [str(gate.preview_path)]
    send_document.assert_called_once_with("chat-1", str(gate.preview_path))
    pending = pending_actions.get(prefix)
    assert pending is not None
    assert pending["action"] == "TaskTriage"

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch(
            "sase.bead.task_gate.launch_task_triage",
            return_value=SimpleNamespace(proc_id="task-123"),
        ) as launch_task,
    ):
        _handle_callback(_callback(f"gate:{prefix}:c0"), {prefix: pending})

    launch_task.assert_called_once()
    response = json.loads(gate.response_path.read_text(encoding="utf-8"))
    assert response["selected_option_ids"] == ["launch"]
    assert response["option_results"][0]["result"] == {"action": "launch"}
    assert response["task_launch_task_id"] == "task-123"


def test_task_triage_close_uses_required_feedback_flow(gate_home: Path) -> None:
    gate = create_task_triage_gate(
        request_id="telegram-task-close",
        bead_id="sase-close",
        project="sase",
        title="Close this task",
    )
    notification = _stored_notification(gate.notification_id)
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ) as answer,
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch("sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message"),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.credentials.get_chat_id",
            return_value="chat-1",
        ),
        patch("sase.bead.task_gate.close_task_triage") as close_task,
    ):
        _handle_callback(_callback(f"gate:{prefix}:c1", "close"), {prefix: action})
        answer.assert_any_call("close", "Send the required feedback as a text message")
        assert not gate.response_path.exists()

        message = SimpleNamespace(
            text="The task is no longer needed.",
            entities=[],
            message_id=78,
            chat_id="chat-1",
            reply_to_message=SimpleNamespace(message_id=42),
        )
        _handle_text_message(message, {})

    close_task.assert_called_once()
    response = json.loads(gate.response_path.read_text(encoding="utf-8"))
    assert response["selected_option_ids"] == ["close"]
    assert response["feedback"] == "The task is no longer needed."


def test_task_triage_launch_collects_optional_feedback(gate_home: Path) -> None:
    gate = create_task_triage_gate(
        request_id="telegram-task-optional",
        bead_id="sase-optional",
        project="sase",
        title="Launch with context",
    )
    notification = _stored_notification(gate.notification_id)
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ) as answer,
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch("sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message"),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.credentials.get_chat_id",
            return_value="chat-1",
        ),
        patch(
            "sase.bead.task_gate.launch_task_triage",
            return_value=SimpleNamespace(task_id="task-456"),
        ) as launch_task,
    ):
        _handle_callback(_callback(f"gate:{prefix}:f0", "feedback"), {prefix: action})
        answer.assert_any_call(
            "feedback", "Send the required feedback as a text message"
        )
        assert not gate.response_path.exists()

        message = SimpleNamespace(
            text="Start from the failing regression test.",
            entities=[],
            message_id=79,
            chat_id="chat-1",
            reply_to_message=SimpleNamespace(message_id=42),
        )
        _handle_text_message(message, {})

    launch_task.assert_called_once()
    response = json.loads(gate.response_path.read_text(encoding="utf-8"))
    assert response["selected_option_ids"] == ["launch"]
    assert response["feedback"] == "Start from the failing regression test."


def test_task_triage_launch_tap_still_resolves_without_feedback(
    gate_home: Path,
) -> None:
    gate = create_task_triage_gate(
        request_id="telegram-task-plain",
        bead_id="sase-plain",
        project="sase",
        title="Launch without context",
    )
    notification = _stored_notification(gate.notification_id)
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch(
            "sase.bead.task_gate.launch_task_triage",
            return_value=SimpleNamespace(task_id="task-789"),
        ),
    ):
        _handle_callback(_callback(f"gate:{prefix}:c0"), {prefix: action})

    response = json.loads(gate.response_path.read_text(encoding="utf-8"))
    assert response["selected_option_ids"] == ["launch"]
    assert not response.get("feedback")


def test_optional_feedback_callback_submits_expanded_group_selection(
    gate_home: Path,
) -> None:
    result = create_gate(_custom_spec(request_id="telegram-optional-group"))
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch("sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message"),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.credentials.get_chat_id",
            return_value="chat-1",
        ),
    ):
        _handle_callback(_callback(f"gate:{prefix}:x2"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:f0", "feedback"), {prefix: action})

        message = SimpleNamespace(
            text="Audit the restart afterwards.",
            entities=[],
            message_id=80,
            chat_id="chat-1",
            reply_to_message=SimpleNamespace(message_id=42),
        )
        _handle_text_message(message, {})

    response = json.loads(
        (Path(result.bundle_path) / "response.json").read_text(encoding="utf-8")
    )
    assert response["selected_option_ids"] == ["proceed", "audit", "verify"]
    assert response["feedback"] == "Audit the restart afterwards."


def test_disabled_feedback_branch_has_no_feedback_button(gate_home: Path) -> None:
    result = create_gate(_custom_spec(request_id="telegram-disabled-feedback"))
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    _text, keyboard, _attachments = format_notification(notification)
    assert keyboard is not None
    assert f"gate:{prefix}:f1" not in _button_data(keyboard)

    with patch(
        "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
    ) as answer:
        _handle_callback(_callback(f"gate:{prefix}:f1", "nofeedback"), {prefix: action})

    answer.assert_any_call("nofeedback", "This option does not accept feedback")
    assert not (Path(result.bundle_path) / "response.json").exists()


def test_registry_declared_generic_forms_render_keyboards(gate_home: Path) -> None:
    custom = create_gate(_custom_spec(request_id="telegram-registry-custom"))
    task = create_task_triage_gate(
        request_id="telegram-registry-task",
        bead_id="sase-registry",
        project="sase",
        title="Exercise every generic form",
    )
    snooze = create_bead_snooze_gate(
        request_id="telegram-registry-snooze",
        bead_id="sase-registry-snooze",
        project="sase",
        title="Exercise the snooze generic form",
        snooze=SnoozeRecord(
            until="2026-08-09T09:00:00-04:00",
            snoozed_at="2026-08-06T09:00:00-04:00",
            snoozed_by="bryanbugyi34@gmail.com",
            reason="waiting on the upstream fix",
        ),
    )
    flag = create_flag_triage_gate(
        request_id="telegram-registry-flag",
        bead_id="sase-registry-flag",
        project="sase",
        title="Exercise the flag triage generic form",
        flag=FlagFields(
            key="prettier_enabled",
            kind="beta",
            remove_by_date="2026-08-01",
            remove_by_release="0.16.0",
        ),
        due_state="due",
        due_as_of="2026-08-09",
        release="0.16.0",
    )
    stale_cleanup = create_bead_stale_cleanup_gate(
        request_id="telegram-registry-stale-cleanup",
        beads=[_stale_cleanup_bead()],
        min_plus_ones=1,
        stale_after_days=7,
        stale_cleanup_min_beads=10,
        stale_as_of="2026-08-17T11:00:00-04:00",
        producer={"chop": "bead_stale_cleanup"},
    )
    plugins_required = create_plugins_required_gate(
        request_id="telegram-registry-plugins-required",
        project="sase",
        project_label="sase",
        missing=[_missing_plugin_entry()],
        producer={"chop": "plugins_required"},
    )
    notifications = {
        "custom": _notification(custom, action="CustomGate", sender="custom"),
        "task_triage": _stored_notification(task.notification_id),
        "bead_snooze": _stored_notification(snooze.notification_id),
        "flag_triage": _stored_notification(flag.notification_id),
        "bead_stale_cleanup": _stored_notification(stale_cleanup.notification_id),
        "plugins_required": _stored_notification(plugins_required.notification_id),
    }

    for kind in registered_gate_kinds():
        adapter = adapter_for_kind(kind)
        if not adapter.generic_form:
            continue
        _text, keyboard, _attachments = format_notification(notifications[kind])
        assert keyboard is not None, kind


def test_registry_drives_resolution_guard_and_inbound_kind_lookup(
    gate_home: Path,
) -> None:
    request_path = gate_home / "registry-request.json"
    request_path.write_text("{}", encoding="utf-8")
    bundle = SimpleNamespace(root=gate_home, request=request_path, legacy=False)
    response = inbound.ResponseAction(
        action_type="gate",
        notif_id_prefix="registry",
        response_path=gate_home / "response.json",
        response_data={},
        answer_text=None,
        selected_option_ids=("accept",),
        input_data={},
    )
    view = SimpleNamespace(
        bundle_path=gate_home,
        branches=(("accept",),),
        options=(),
    )
    callback = _callback("gate:registry:c0")

    with (
        patch(
            "sase.notification_gates.paths.resolve_action_bundle",
            return_value=bundle,
        ),
        patch(
            "sase.notification_gates.executor.execute_gate_selection",
            return_value=SimpleNamespace(already_completed=False),
        ),
    ):
        for kind in registered_gate_kinds():
            adapter = adapter_for_kind(kind)
            action = {"action": adapter.action, "action_data": {}}
            if adapter.branch_actionable:
                assert inbound.resolve_gate_response(response, action) == (
                    "Gate answered with accept"
                )
            else:
                with pytest.raises(GateError, match="unsupported gate action"):
                    inbound.resolve_gate_response(response, action)

    for kind in registered_gate_kinds():
        adapter = adapter_for_kind(kind)
        action = {
            "action": adapter.action,
            "action_data": {},
            "message_id": 42,
            "chat_id": "chat-1",
        }
        if not adapter.branch_actionable:
            with patch(
                "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
            ) as answer:
                _handle_gate_callback(callback, {"registry": action})
            answer.assert_called_once_with("callback", "This request has expired")
            continue

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound.load_gate_view",
                return_value=view,
            ) as load_view,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.load_gate_progress",
                return_value=GateProgress(),
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.feedback_mode",
                return_value="disabled",
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._execute_gate_callback_response"
            ) as execute_response,
        ):
            _handle_gate_callback(callback, {"registry": action})
        load_view.assert_called_once_with({}, expected_kind=adapter.kind)
        execute_response.assert_called_once()


@pytest.mark.parametrize(
    ("case", "toggle_tokens", "expected_option_ids"),
    [
        pytest.param("proceed-only", ("x1",), ["proceed"], id="one"),
        pytest.param("defaults", (), ["proceed", "audit"], id="defaults"),
        pytest.param("all", ("x2",), ["proceed", "audit", "verify"], id="all"),
    ],
)
def test_group_selection_matrix_executes_options_in_query_order(
    gate_home: Path,
    case: str,
    toggle_tokens: tuple[str, ...],
    expected_option_ids: list[str],
) -> None:
    result = create_gate(_custom_spec(request_id=f"telegram-options-{case}"))
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
    ):
        for token in toggle_tokens:
            _handle_callback(_callback(f"gate:{prefix}:{token}"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:s0"), {prefix: action})

    response = json.loads(
        Path(notification.action_data["response_path"]).read_text(encoding="utf-8")
    )
    assert response["selected_option_ids"] == expected_option_ids
    assert response["source"] == "telegram"
    assert [result["id"] for result in response["option_results"]] == (
        expected_option_ids
    )


def test_required_feedback_uses_generic_two_step_text_flow(gate_home: Path) -> None:
    result = create_gate(
        _custom_spec(request_id="telegram-feedback", feedback="required")
    )
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ) as answer,
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch("sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message"),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.credentials.get_chat_id",
            return_value="chat-1",
        ),
    ):
        _handle_callback(_callback(f"gate:{prefix}:s0", "submit"), {prefix: action})
        answer.assert_any_call("submit", "Send the required feedback as a text message")
        assert not Path(notification.action_data["response_path"]).exists()

        message = SimpleNamespace(
            text="Please schedule it after midnight.",
            entities=[],
            message_id=77,
            chat_id="chat-1",
            reply_to_message=SimpleNamespace(message_id=42),
        )
        _handle_text_message(message, {})

    response = json.loads(
        Path(notification.action_data["response_path"]).read_text(encoding="utf-8")
    )
    assert response["feedback"] == "Please schedule it after midnight."
    assert response["selected_option_ids"] == ["proceed", "audit"]


def test_hitl_uses_the_same_renderer_and_executor(gate_home: Path) -> None:
    result = create_gate(_hitl_spec())
    notification = _notification(result, action="HITL", sender="hitl")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    _, keyboard, _ = format_notification(notification)
    assert keyboard is not None
    assert _button_data(keyboard) == [f"gate:{prefix}:c0"]

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
    ):
        _handle_callback(_callback(f"gate:{prefix}:c0"), {prefix: action})

    bundle = Path(notification.action_data["bundle_path"])
    response = json.loads((bundle / "response.json").read_text(encoding="utf-8"))
    assert response["selected_option_ids"] == ["accept"]
    assert not (bundle / "hitl_response.json").exists()


def test_launch_approval_uses_the_same_singleton_renderer(gate_home: Path) -> None:
    result = create_launch_approval_request(
        {
            "schema_version": 1,
            "prompt": "%i(telegram-launch, reviewer)\nReview this change",
            "reason": "Verify the Telegram launch controls",
            "approval": "required",
            "max_slots": 1,
        },
        source_surface="telegram-test",
    )
    notification = _notification(
        SimpleNamespace(
            bundle_path=result.response_dir,
            notification_id=result.notification_id,
        ),
        action="LaunchApproval",
        sender="launch",
    )
    notification.files = [str(result.preview_path)]

    _, keyboard, attachments = format_notification(notification)

    assert keyboard is not None
    assert [[button.text for button in row] for row in keyboard.inline_keyboard] == [
        ["✅ Approve", "❌ Reject"],
        ["💬 Reject with feedback"],
    ]
    prefix = notification.id[:8]
    assert _button_data(keyboard) == [
        f"gate:{prefix}:c0",
        f"gate:{prefix}:c1",
        f"gate:{prefix}:f1",
    ]
    assert attachments == notification.files

    action = _pending(notification)
    pending_actions.add(prefix, action)
    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch("sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message"),
    ):
        _handle_callback(_callback(f"gate:{prefix}:c1"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:i0k"), {prefix: action})

    response = json.loads(result.response_path.read_text(encoding="utf-8"))
    assert response["selected_option_ids"] == ["reject"]
    assert response["source"] == "telegram"


def test_epic_approval_uses_singleton_branch_row(gate_home: Path) -> None:
    notification = _epic_notification(gate_home, "telegram-epic-formatting")
    bundle = Path(notification.action_data["bundle_path"])
    legacy_plan = gate_home / "legacy-epic.md"
    legacy_plan.write_text(VALID_EPIC_PLAN, encoding="utf-8")
    notification.files = [str(legacy_plan)]

    def validate_plan(content: str, tier: str, *, mode: str):
        assert "size:" not in content
        assert tier == "epic"
        assert mode == "launch"
        return SimpleNamespace(
            ok=True,
            plan=SimpleNamespace(phases=(SimpleNamespace(size="small"),)),
        )

    with patch(
        "sase_telegram.formatting.import_module",
        return_value=SimpleNamespace(validate_plan=validate_plan),
    ):
        text, keyboard, attachments = format_notification(notification)

    assert text.splitlines()[:2] == [
        "📋 *Epic Review · 1 phase*",
        "Phase sizes: 1 small",
    ]
    assert attachments == notification.files
    assert notification.dismissed is True
    assert keyboard is not None
    prefix = notification.id[:8]
    assert _button_data(keyboard) == [
        f"gate:{prefix}:c0",
        f"gate:{prefix}:c1",
        f"gate:{prefix}:c2",
    ]
    assert not (bundle / "telegram_gate_progress.json").exists()


def test_epic_validator_failure_preserves_gate_and_attachment(
    gate_home: Path,
) -> None:
    notification = _epic_notification(gate_home, "telegram-epic-degraded")

    with patch(
        "sase_telegram.formatting.import_module",
        side_effect=ImportError("validator unavailable"),
    ):
        text, keyboard, attachments = format_notification(notification)

    assert text.splitlines()[0] == "📋 *Epic Review · 1 phase*"
    assert "Phase sizes:" not in text
    assert "🧾 *Properties*" in text
    assert "*Plan*" in text
    assert attachments == notification.files
    assert keyboard is not None
    prefix = notification.id[:8]
    assert _button_data(keyboard) == [
        f"gate:{prefix}:c0",
        f"gate:{prefix}:c1",
        f"gate:{prefix}:c2",
    ]


def test_epic_approval_outbound_sends_generic_keyboard(
    gate_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notification = _epic_notification(gate_home, "telegram-epic-outbound")
    last_sent_file = gate_home / "last-sent"
    last_sent_file.write_text("0", encoding="utf-8")
    monkeypatch.setattr(outbound, "LAST_SENT_FILE", last_sent_file)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_outbound.get_unsent_notifications",
            return_value=[notification],
        ),
        patch(
            "sase_telegram.scripts.sase_tg_outbound.get_chat_id",
            return_value="chat-1",
        ),
        patch(
            "sase_telegram.scripts.sase_tg_outbound.rate_limit.check_rate_limit",
            return_value=True,
        ),
        patch("sase_telegram.scripts.sase_tg_outbound.rate_limit.record_send"),
        patch(
            "sase_telegram.scripts.sase_tg_outbound.send_message",
            return_value=SimpleNamespace(message_id=42),
        ) as send_message,
        patch("sase_telegram.scripts.sase_tg_outbound.md_to_pdf", return_value=None),
        patch("sase_telegram.scripts.sase_tg_outbound.send_document") as send_document,
        patch("sase_telegram.scripts.sase_tg_outbound._register_shared_transport"),
    ):
        assert _run_outbound(argparse.Namespace(dry_run=False)) == 0

    keyboard = send_message.call_args.kwargs["reply_markup"]
    prefix = notification.id[:8]
    assert _button_data(keyboard) == [
        f"gate:{prefix}:c0",
        f"gate:{prefix}:c1",
        f"gate:{prefix}:c2",
    ]
    cursor = json.loads(last_sent_file.read_text(encoding="utf-8"))
    assert cursor["version"] == 2
    assert datetime.fromisoformat(cursor["activity_at"]).timestamp() == pytest.approx(
        datetime.fromisoformat(notification.timestamp).timestamp()
    )
    assert cursor["id"] == notification.id
    send_document.assert_called_once_with("chat-1", notification.files[0])


@pytest.mark.parametrize(
    ("plan_content", "action", "proposal_name", "expected_filename"),
    [
        pytest.param(
            VALID_TALE_PLAN,
            "PlanApproval",
            "telegram.plan.review.tale.md",
            "telegram.plan.review.tale.pdf",
            id="tale",
        ),
        pytest.param(
            _epic_plan_for_installed_sase(),
            "EpicApproval",
            "telegram.plan.review.epic.md",
            "telegram.plan.review.epic.pdf",
            id="epic",
        ),
    ],
)
def test_plan_approval_outbound_names_rendered_pdf_after_durable_proposal(
    gate_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan_content: str,
    action: str,
    proposal_name: str,
    expected_filename: str,
) -> None:
    durable_dir = gate_home / "durable" / "nested"
    durable_dir.mkdir(parents=True)
    plan_file = durable_dir / proposal_name
    plan_file.write_text(plan_content, encoding="utf-8")
    result = create_plan_approval_gate(plan_file, f"telegram-{action.lower()}")
    notification = _notification(result, action=action, sender="plan")
    bundle_plan = Path(result.bundle_path) / "plan.md"
    notification.files = [str(bundle_plan)]
    last_sent_file = gate_home / f"last-sent-{action.lower()}"
    last_sent_file.write_text("0", encoding="utf-8")
    monkeypatch.setattr(outbound, "LAST_SENT_FILE", last_sent_file)
    rendered_pdf = bundle_plan.with_suffix(".pdf")
    rendered_bytes = b"%PDF-1.4\nrendered plan bytes\n"
    sent_documents: list[tuple[str, str, bytes, str | None]] = []

    def render_plan(source: str) -> str:
        assert source == str(bundle_plan)
        assert Path(source).read_text(encoding="utf-8") == plan_content
        rendered_pdf.write_bytes(rendered_bytes)
        return str(rendered_pdf)

    def capture_document(
        chat_id: str,
        file_path: str,
        *,
        filename: str | None = None,
    ) -> None:
        sent_documents.append(
            (chat_id, file_path, Path(file_path).read_bytes(), filename)
        )

    with (
        patch(
            "sase_telegram.scripts.sase_tg_outbound.get_unsent_notifications",
            return_value=[notification],
        ),
        patch(
            "sase_telegram.scripts.sase_tg_outbound.get_chat_id",
            return_value="chat-1",
        ),
        patch(
            "sase_telegram.scripts.sase_tg_outbound.rate_limit.check_rate_limit",
            return_value=True,
        ),
        patch("sase_telegram.scripts.sase_tg_outbound.rate_limit.record_send"),
        patch(
            "sase_telegram.scripts.sase_tg_outbound.send_message",
            return_value=SimpleNamespace(message_id=42),
        ),
        patch(
            "sase_telegram.scripts.sase_tg_outbound.md_to_pdf",
            side_effect=render_plan,
        ),
        patch(
            "sase_telegram.scripts.sase_tg_outbound.send_document",
            side_effect=capture_document,
        ),
        patch("sase_telegram.scripts.sase_tg_outbound._register_shared_transport"),
    ):
        assert _run_outbound(argparse.Namespace(dry_run=False)) == 0

    assert notification.action_data["original_plan_file"] == str(plan_file)
    assert sent_documents == [
        ("chat-1", str(rendered_pdf), rendered_bytes, expected_filename)
    ]
    assert bundle_plan.exists()
    assert not rendered_pdf.exists()


def test_tale_plan_pins_five_control_layout_and_submits_selected_options(
    gate_home: Path,
) -> None:
    plan_file = gate_home / "plan.md"
    plan_file.write_text(VALID_TALE_PLAN, encoding="utf-8")
    result = create_plan_approval_gate(plan_file, "telegram-plan")
    notification = _notification(result, action="PlanApproval", sender="plan")
    bundle = Path(notification.action_data["bundle_path"])
    notification.files = [str(bundle / "plan.md")]
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    _, keyboard, _ = format_notification(notification)
    assert keyboard is not None
    rows = keyboard.inline_keyboard
    assert [[button.text for button in row] for row in rows] == [
        ["☑️ 🚀 Launch coder agent"],
        ["☑️ 💾 Commit plan file to the plans sidecar"],
        ["✅ Tale"],
        ["❌ Reject", "💬 Send Feedback"],
    ]
    assert _button_data(keyboard) == [
        f"gate:{prefix}:x0",
        f"gate:{prefix}:x1",
        f"gate:{prefix}:s0",
        f"gate:{prefix}:c1",
        f"gate:{prefix}:c2",
    ]

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch("sase.plan_approval_actions.run_plan_side_effects"),
    ):
        _handle_callback(_callback(f"gate:{prefix}:x0"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:s0"), {prefix: action})

    response = json.loads((bundle / "response.json").read_text(encoding="utf-8"))
    assert response["selected_option_ids"] == ["commit"]


# ---------------------------------------------------------------------------
# Declared-input step flow (sase-h7.8)
# ---------------------------------------------------------------------------


def _input_option(
    option_id: str,
    *,
    inputs: tuple[dict[str, object], ...] = (),
    feedback: str = "disabled",
) -> dict[str, object]:
    return {
        "id": option_id,
        "label": option_id.replace("_", " ").title(),
        "icon": "✅",
        "feedback": feedback,
        "command": {"argv": [f"commands/{option_id}"]},
        "inputs": list(inputs),
        "result_schema": {"type": "object"},
    }


def _inputs_gate_spec(
    *,
    request_id: str,
    query: str = "proceed",
    primary_branch: tuple[str, ...] = ("proceed",),
    options: tuple[dict[str, object], ...] | None = None,
    groups: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    resolved_options = options or (_input_option("proceed"),)
    return {
        "schema_version": 3,
        "request_id": request_id,
        "kind": "custom",
        "producer": {"agent": "telegram-test"},
        "payload": {"operation": "restart"},
        "presentation": {
            "title": "Declared input request",
            "icon": "🛡️",
            "sender": "safety-agent",
            "notes": ["Provide the requested details."],
        },
        "query": query,
        "primary_branch": list(primary_branch),
        "options": list(resolved_options),
        "groups": list(groups),
        "resources": [
            {
                "path": f"commands/{option['id']}",
                "role": "command",
                "content": _command_script(f"{option['id']}-done"),
            }
            for option in resolved_options
        ],
        "auto": False,
    }


def _text_message(text: str, *, message_id: int = 100) -> SimpleNamespace:
    """A text reply with no reply_to_message, relying on the single-entry fallback."""
    return SimpleNamespace(
        text=text,
        entities=[],
        message_id=message_id,
        chat_id="chat-1",
        reply_to_message=None,
    )


_NOTE_FIELD = {"id": "note", "label": "Note", "type": "line", "required": True}
_COLOR_FIELD = {
    "id": "color",
    "label": "Color",
    "type": "enum",
    "required": False,
    "choices": ["red", "green", "blue"],
}
_CONFIRM_FIELD = {
    "id": "confirm",
    "label": "Confirm",
    "type": "bool",
    "required": False,
}


def test_declared_input_step_flow_collects_values_in_order(gate_home: Path) -> None:
    result = create_gate(
        _inputs_gate_spec(
            request_id="telegram-inputs-order",
            options=(
                _input_option(
                    "proceed", inputs=(_NOTE_FIELD, _COLOR_FIELD, _CONFIRM_FIELD)
                ),
            ),
        )
    )
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    sent: list[SimpleNamespace] = []

    def _fake_send_message(
        chat_id: str, text: str, **kwargs: object
    ) -> SimpleNamespace:
        message = SimpleNamespace(message_id=200 + len(sent), text=text, **kwargs)
        sent.append(message)
        return message

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message",
            side_effect=_fake_send_message,
        ),
    ):
        _handle_callback(_callback(f"gate:{prefix}:c0"), {prefix: action})
        assert sent[-1].text.startswith("📝 *Input 1/3*")

        _handle_text_message(_text_message("hello there"), {})
        assert sent[-1].text.startswith("📝 *Input 2/3*")
        keyboard = sent[-1].reply_markup
        button_texts = [
            button.text for row in keyboard.inline_keyboard for button in row
        ]
        assert button_texts[:3] == ["red", "green", "blue"]

        _handle_callback(_callback(f"gate:{prefix}:i1v1"), {prefix: action})
        assert sent[-1].text.startswith("📝 *Input 3/3*")

        _handle_text_message(_text_message("yes"), {})

    response = json.loads(
        (Path(result.bundle_path) / "response.json").read_text(encoding="utf-8")
    )
    assert response["selected_option_ids"] == ["proceed"]
    assert response["option_inputs"] == {
        "proceed": {"note": "hello there", "color": "green", "confirm": True}
    }


def test_and_group_members_receive_only_their_own_declared_inputs(
    gate_home: Path,
) -> None:
    audit_field = {
        "id": "note_a",
        "label": "Audit note",
        "type": "line",
        "required": True,
    }
    verify_field = {
        "id": "note_b",
        "label": "Verify note",
        "type": "line",
        "required": True,
    }
    result = create_gate(
        _inputs_gate_spec(
            request_id="telegram-inputs-and-group",
            query="(audit AND verify)",
            primary_branch=("audit", "verify"),
            options=(
                _input_option("audit", inputs=(audit_field,)),
                _input_option("verify", inputs=(verify_field,)),
            ),
            groups=(
                {"options": ["audit", "verify"], "label": "Proceed", "icon": "✅"},
            ),
        )
    )
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch("sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message"),
    ):
        _handle_callback(_callback(f"gate:{prefix}:c0"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:s0"), {prefix: action})
        _handle_text_message(_text_message("audit note"), {})
        _handle_text_message(_text_message("verify note"), {})

    response = json.loads(
        (Path(result.bundle_path) / "response.json").read_text(encoding="utf-8")
    )
    assert response["selected_option_ids"] == ["audit", "verify"]
    assert response["option_inputs"] == {
        "audit": {"note_a": "audit note"},
        "verify": {"note_b": "verify note"},
    }


def test_invalid_answer_reprompts_same_field_and_leaves_gate_pending(
    gate_home: Path,
) -> None:
    count_field = {
        "id": "count",
        "label": "Retry count",
        "type": "int",
        "required": True,
    }
    result = create_gate(
        _inputs_gate_spec(
            request_id="telegram-inputs-invalid",
            options=(_input_option("proceed", inputs=(count_field,)),),
        )
    )
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    sent: list[str] = []

    def _fake_send_message(
        chat_id: str, text: str, **kwargs: object
    ) -> SimpleNamespace:
        sent.append(text)
        return SimpleNamespace(message_id=300 + len(sent))

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message",
            side_effect=_fake_send_message,
        ),
    ):
        _handle_callback(_callback(f"gate:{prefix}:c0"), {prefix: action})
        _handle_text_message(_text_message("not-a-number"), {})

    assert not (Path(result.bundle_path) / "response.json").exists()
    assert any("expects int" in message for message in sent)
    assert sent[-1].startswith("📝 *Input 1/1*")


def test_skip_omits_optional_field_and_is_refused_on_required_field(
    gate_home: Path,
) -> None:
    result = create_gate(
        _inputs_gate_spec(
            request_id="telegram-inputs-skip",
            options=(
                _input_option(
                    "proceed", inputs=(_NOTE_FIELD, _COLOR_FIELD, _CONFIRM_FIELD)
                ),
            ),
        )
    )
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ) as answer,
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch("sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message"),
    ):
        _handle_callback(_callback(f"gate:{prefix}:c0"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:i0k"), {prefix: action})
        answer.assert_any_call("callback", "This input is required")

        _handle_text_message(_text_message("hello"), {})
        _handle_callback(_callback(f"gate:{prefix}:i1k"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:i2k"), {prefix: action})

    response = json.loads(
        (Path(result.bundle_path) / "response.json").read_text(encoding="utf-8")
    )
    assert response["option_inputs"] == {"proceed": {"note": "hello"}}


def test_repeatable_enum_accumulates_toggles_and_submits_on_done(
    gate_home: Path,
) -> None:
    colors_field = {
        "id": "colors",
        "label": "Colors",
        "type": "enum",
        "required": False,
        "repeatable": True,
        "choices": ["red", "green", "blue"],
    }
    result = create_gate(
        _inputs_gate_spec(
            request_id="telegram-inputs-repeatable",
            options=(_input_option("proceed", inputs=(colors_field,)),),
        )
    )
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch("sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message"),
    ):
        _handle_callback(_callback(f"gate:{prefix}:c0"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:i0v0"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:i0v2"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:i0d"), {prefix: action})

    response = json.loads(
        (Path(result.bundle_path) / "response.json").read_text(encoding="utf-8")
    )
    assert response["option_inputs"] == {"proceed": {"colors": ["red", "blue"]}}


def test_cancel_clears_input_block_and_leaves_gate_answerable(gate_home: Path) -> None:
    result = create_gate(
        _inputs_gate_spec(
            request_id="telegram-inputs-cancel",
            options=(
                _input_option(
                    "proceed", inputs=(_NOTE_FIELD, _COLOR_FIELD, _CONFIRM_FIELD)
                ),
            ),
        )
    )
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ) as answer,
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch("sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message"),
    ):
        _handle_callback(_callback(f"gate:{prefix}:c0"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:i0c"), {prefix: action})
        answer.assert_any_call(
            "callback", "Input cancelled — the gate is still answerable"
        )
        assert pending_actions.get(prefix) is not None
        assert not (Path(result.bundle_path) / "response.json").exists()

        # A re-tap of the original keyboard restarts collection from the first field.
        _handle_callback(_callback(f"gate:{prefix}:c0"), {prefix: action})
        _handle_text_message(_text_message("hello"), {})
        _handle_callback(_callback(f"gate:{prefix}:i1k"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:i2k"), {prefix: action})

    response = json.loads(
        (Path(result.bundle_path) / "response.json").read_text(encoding="utf-8")
    )
    assert response["option_inputs"] == {"proceed": {"note": "hello"}}


def test_secret_field_is_refused_with_pointed_message(gate_home: Path) -> None:
    token_field = {
        "id": "token",
        "label": "Token",
        "type": "word",
        "required": True,
        "secret": True,
    }
    result = create_gate(
        _inputs_gate_spec(
            request_id="telegram-inputs-secret",
            options=(_input_option("proceed", inputs=(token_field,)),),
        )
    )
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ) as answer,
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
    ):
        _handle_callback(_callback(f"gate:{prefix}:c0"), {prefix: action})

    answer.assert_any_call("callback", "Telegram cannot collect secret input: token")
    assert not (Path(result.bundle_path) / "response.json").exists()


def test_gate_without_declared_inputs_still_submits_immediately(
    gate_home: Path,
) -> None:
    result = create_gate(_inputs_gate_spec(request_id="telegram-inputs-none"))
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
    ):
        _handle_callback(_callback(f"gate:{prefix}:c0"), {prefix: action})

    response = json.loads(
        (Path(result.bundle_path) / "response.json").read_text(encoding="utf-8")
    )
    assert response["selected_option_ids"] == ["proceed"]
    assert response["option_inputs"] == {"proceed": {}}


def test_optional_feedback_with_inputs_collects_both_and_submits(
    gate_home: Path,
) -> None:
    result = create_gate(
        _inputs_gate_spec(
            request_id="telegram-inputs-feedback",
            options=(
                _input_option("proceed", inputs=(_NOTE_FIELD,), feedback="optional"),
            ),
        )
    )
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch("sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message"),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.credentials.get_chat_id",
            return_value="chat-1",
        ),
    ):
        _handle_callback(_callback(f"gate:{prefix}:f0", "feedback"), {prefix: action})
        _handle_text_message(_text_message("audit context"), {})
        _handle_text_message(_text_message("please double-check first"), {})

    response = json.loads(
        (Path(result.bundle_path) / "response.json").read_text(encoding="utf-8")
    )
    assert response["selected_option_ids"] == ["proceed"]
    assert response["feedback"] == "please double-check first"
    assert response["option_inputs"] == {
        "proceed": {"note": "audit context", "feedback": "please double-check first"}
    }
