"""Telegram gate formatting, progress, and executor integration tests."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sase.agent.launch_request import create_launch_approval_request
from sase.notification_gates.service import create_gate
from sase.notifications.models import Notification
from sase.plan_gate import create_plan_approval_gate
from sase_telegram import inbound, outbound, pending_actions
from sase_telegram.formatting import format_notification
from sase_telegram.scripts.sase_tg_inbound import (
    _handle_callback,
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
        "schema_version": 2,
        "request_id": request_id,
        "kind": "custom",
        "producer": {"agent": "telegram-test"},
        "payload": {"operation": "restart"},
        "presentation": {
            "icon": "🛡️",
            "sender": "safety-agent",
            "notes": ["Restart the guarded service?"],
            "files": ["preview.md"],
        },
        "query": "(proceed AND audit AND verify) OR cancel",
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
        "schema_version": 2,
        "request_id": "telegram-hitl",
        "kind": "hitl",
        "producer": {"agent": "telegram-test"},
        "payload": {"step_name": "review", "output": {"ok": True}},
        "presentation": {"notes": ["Review workflow output"]},
        "query": "accept",
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


def _notification(result: object, *, action: str, sender: str) -> Notification:
    bundle_path = Path(result.bundle_path)
    request = json.loads((bundle_path / "request.json").read_text(encoding="utf-8"))
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
        action_data={
            "request_id": str(request["request_id"]),
            "request_kind": str(request["kind"]),
            "bundle_path": str(bundle_path),
            "request_path": str(bundle_path / "request.json"),
            "response_path": str(bundle_path / "response.json"),
            "response_dir": str(bundle_path),
            "artifacts_dir": str(bundle_path),
        },
    )


def _pending(notification: Notification) -> dict[str, object]:
    return {
        "notification_id": notification.id,
        "action": notification.action,
        "action_data": notification.action_data,
        "message_id": 42,
        "chat_id": "chat-1",
    }


def _epic_notification(gate_home: Path, request_id: str) -> Notification:
    plan_file = gate_home / f"{request_id}.md"
    plan_file.write_text(VALID_EPIC_PLAN, encoding="utf-8")
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
    assert [row[0].text for row in keyboard.inline_keyboard] == [
        "☑️ ✅ Proceed safely",
        "☑️ 📝 Write audit record",
        "⬜ 🩺 Verify health",
        "✅ Proceed safely",
        "❌ Cancel",
    ]
    prefix = notification.id[:8]
    assert _button_data(keyboard) == [
        f"gate:{prefix}:x0",
        f"gate:{prefix}:x1",
        f"gate:{prefix}:x2",
        f"gate:{prefix}:s0",
        f"gate:{prefix}:c1",
    ]
    assert attachments == notification.files

    notification.action_data["request_id"] = "missing"
    notification.action_data["bundle_path"] = str(gate_home / "missing")
    _, fallback_keyboard, _ = format_notification(notification)
    assert fallback_keyboard is None


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
            "prompt": "%n(telegram-launch, reviewer)\nReview this change",
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
        ["✅ Approve", "❌ Reject", "💬 Send Feedback"]
    ]
    prefix = notification.id[:8]
    assert _button_data(keyboard) == [
        f"gate:{prefix}:c0",
        f"gate:{prefix}:c1",
        f"gate:{prefix}:c2",
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
    ):
        _handle_callback(_callback(f"gate:{prefix}:c1"), {prefix: action})

    response = json.loads(result.response_path.read_text(encoding="utf-8"))
    assert response["selected_option_ids"] == ["reject"]
    assert response["source"] == "telegram"


def test_epic_approval_uses_singleton_branch_row(gate_home: Path) -> None:
    notification = _epic_notification(gate_home, "telegram-epic-formatting")
    bundle = Path(notification.action_data["bundle_path"])

    text, keyboard, attachments = format_notification(notification)

    assert "Epic Review" in text
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
        patch("sase_telegram.scripts.sase_tg_outbound.send_document"),
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
    assert float(last_sent_file.read_text(encoding="utf-8")) == pytest.approx(
        datetime.fromisoformat(notification.timestamp).timestamp()
    )


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
        ["☑️ ✅ Approve"],
        ["☑️ 💾 Commit plan file to the plans sidecar"],
        ["✅ Approve"],
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
