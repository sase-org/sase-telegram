"""Telegram custom-gate formatting, state, and executor integration tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sase.notification_gates.service import create_gate
from sase.notifications.models import Notification
from sase.plan_gate import create_plan_approval_gate
from sase_telegram import inbound, pending_actions
from sase_telegram.formatting import format_notification
from sase_telegram.scripts.sase_tg_inbound import (
    _handle_callback,
    _handle_text_message,
)

VALID_TALE_PLAN = """---
tier: tale
title: Telegram plan approval
goal: Verify Telegram add-on selection
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
    return {
        "schema_version": 1,
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
        "choices": [
            {
                "id": "proceed",
                "label": "Proceed safely",
                "icon": "✅",
                "feedback": feedback,
                "command": {"argv": ["commands/proceed"]},
                "input_schema": {"type": "object"},
                "result_schema": {"type": "object"},
                "extras": [
                    {
                        "id": "audit",
                        "label": "Write audit record",
                        "icon": "📝",
                        "default_selected": True,
                        "command": {"argv": ["commands/audit"]},
                    },
                    {
                        "id": "verify",
                        "label": "Verify health",
                        "icon": "🩺",
                        "default_selected": False,
                        "command": {"argv": ["commands/verify"]},
                    },
                ],
            },
            {
                "id": "cancel",
                "label": "Cancel",
                "icon": "❌",
                "feedback": "disabled",
                "command": {"argv": ["commands/cancel"]},
                "input_schema": {"type": "object"},
                "result_schema": {"type": "object"},
            },
        ],
        "resources": [
            {
                "path": "commands/proceed",
                "role": "command",
                "content": _command_script("proceeded"),
            },
            {
                "path": "commands/audit",
                "role": "command",
                "content": _command_script("audited"),
            },
            {
                "path": "commands/verify",
                "role": "command",
                "content": _command_script("healthy"),
            },
            {
                "path": "commands/cancel",
                "role": "command",
                "content": _command_script("cancelled"),
            },
            {
                "path": "preview.md",
                "role": "preview",
                "content": "# Guarded restart\n",
            },
        ],
        "auto": False,
    }


def _hitl_spec() -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": "telegram-hitl",
        "kind": "hitl",
        "producer": {"agent": "telegram-test"},
        "payload": {"step_name": "review", "output": {"ok": True}},
        "presentation": {"notes": ["Review workflow output"]},
        "choices": [
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
    request_id = str(request["request_id"])
    kind = str(request["kind"])
    notification_id = str(result.notification_id)
    return Notification(
        id=notification_id,
        timestamp="2026-07-17T00:00:00+00:00",
        sender=sender,
        icon="🛡️" if action == "CustomGate" else None,
        notes=["N" * 700],
        files=[str(bundle_path / "preview.md")]
        if (bundle_path / "preview.md").exists()
        else [],
        action=action,
        action_data={
            "request_id": request_id,
            "request_kind": kind,
            "bundle_path": str(bundle_path),
            "request_path": str(bundle_path / "request.json"),
            "response_path": str(bundle_path / "response.json"),
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


def _callback(data: str, callback_id: str = "callback") -> SimpleNamespace:
    return SimpleNamespace(
        id=callback_id,
        data=data,
        message=SimpleNamespace(message_id=42, chat_id="chat-1"),
    )


def test_custom_gate_formatting_uses_icons_compact_callbacks_and_fallback(
    gate_home: Path,
) -> None:
    result = create_gate(_custom_spec())
    notification = _notification(result, action="CustomGate", sender="safety-agent")

    text, keyboard, attachments = format_notification(notification)

    assert text.startswith("🛡️ *Custom Request*")
    assert "**>" in text
    assert keyboard is not None
    assert [row[0].text for row in keyboard.inline_keyboard] == [
        "✅ Proceed safely",
        "❌ Cancel",
    ]
    assert (
        keyboard.inline_keyboard[0][0].callback_data
        == "gate:" + notification.id[:8] + ":c0"
    )
    assert attachments == notification.files

    notification.action_data["request_id"] = "missing"
    notification.action_data["bundle_path"] = str(gate_home / "missing")
    _, fallback_keyboard, _ = format_notification(notification)
    assert fallback_keyboard is None


def test_custom_gate_toggle_submit_and_duplicate_callback(
    gate_home: Path,
) -> None:
    result = create_gate(_custom_spec())
    notification = _notification(result, action="CustomGate", sender="safety-agent")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)
    pending = {prefix: action}

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ) as answer,
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ) as edit,
    ):
        _handle_callback(_callback(f"gate:{prefix}:c0", "choose"), pending)
        configured = edit.call_args.kwargs["reply_markup"]
        assert configured.inline_keyboard[2][0].text.startswith("☑️ 📝")
        assert configured.inline_keyboard[3][0].text.startswith("⬜ 🩺")

        _handle_callback(_callback(f"gate:{prefix}:x1", "toggle"), pending)
        _handle_callback(_callback(f"gate:{prefix}:submit", "submit"), pending)

        response_path = Path(notification.action_data["response_path"])
        response = json.loads(response_path.read_text(encoding="utf-8"))
        assert response["choice_id"] == "proceed"
        assert response["selected_extra_ids"] == ["audit", "verify"]
        assert response["source"] == "telegram"
        assert inbound.find_externally_handled({prefix: action}) == [
            (prefix, 42, "chat-1")
        ]

        before = response_path.read_text(encoding="utf-8")
        _handle_callback(_callback(f"gate:{prefix}:submit", "duplicate"), pending)
        assert response_path.read_text(encoding="utf-8") == before
        answer.assert_any_call("duplicate", "This action has already been handled")


def test_required_feedback_uses_two_step_text_flow(gate_home: Path) -> None:
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
        _handle_callback(_callback(f"gate:{prefix}:c0", "choose"), {prefix: action})
        _handle_callback(_callback(f"gate:{prefix}:submit", "submit"), {prefix: action})
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
    assert response["selected_extra_ids"] == ["audit"]


def test_neutral_hitl_uses_shared_executor_not_legacy_writer(gate_home: Path) -> None:
    result = create_gate(_hitl_spec())
    notification = _notification(result, action="HITL", sender="hitl")
    prefix = notification.id[:8]
    action = _pending(notification)
    pending_actions.add(prefix, action)

    _, keyboard, _ = format_notification(notification)
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].callback_data == f"hitl:{prefix}:c0"

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
    ):
        _handle_callback(_callback(f"hitl:{prefix}:c0"), {prefix: action})

    bundle = Path(notification.action_data["bundle_path"])
    response = json.loads((bundle / "response.json").read_text(encoding="utf-8"))
    assert response["choice_id"] == "accept"
    assert response["input"] == {"action": "accept", "approved": True}
    assert not (bundle / "hitl_response.json").exists()


def test_plan_approval_renders_and_submits_add_on_toggles(gate_home: Path) -> None:
    plan_file = gate_home / "plan.md"
    plan_file.write_text(VALID_TALE_PLAN, encoding="utf-8")
    result = create_plan_approval_gate(plan_file, "telegram-plan")
    notification = _notification(result, action="PlanApproval", sender="plan")
    bundle = Path(notification.action_data["bundle_path"])
    notification.files = [str(bundle / "plan.md")]
    prefix = notification.id[:8]
    action = _pending(notification)
    action["files"] = list(notification.files)
    action["plan_file"] = notification.files[0]
    pending_actions.add(prefix, action)

    _, keyboard, _ = format_notification(notification)
    assert keyboard is not None
    rows = keyboard.inline_keyboard
    assert [button.callback_data for button in rows[0]] == [
        f"plan:{prefix}:tale",
        f"plan:{prefix}:approve",
    ]
    assert rows[2][0].text.startswith("☑️ 💾 Commit plan file")
    assert rows[3][0].text.startswith("☑️ ▶️ Run coder follow-up")
    assert rows[4][0].callback_data == f"plan:{prefix}:submit"

    with (
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.answer_callback_query"
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound.telegram_client.edit_message_reply_markup"
        ),
        patch("sase_telegram.scripts.sase_tg_inbound.telegram_client.send_message"),
    ):
        pending = {prefix: action}
        _handle_callback(_callback(f"plan:{prefix}:x0", "commit-off"), pending)
        _handle_callback(_callback(f"plan:{prefix}:x1", "coder-off"), pending)
        _handle_callback(_callback(f"plan:{prefix}:submit", "submit"), pending)

    response = json.loads((bundle / "response.json").read_text(encoding="utf-8"))
    assert response["choice_id"] == "approve"
    assert response["selected_extra_ids"] == []
    assert response["result"]["commit_plan"] is False
    assert response["result"]["run_coder"] is False
