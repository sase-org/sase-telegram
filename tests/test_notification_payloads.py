from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sase_telegram.inbound_handlers import project_context


def test_large_completion_prompt_reaches_formatting_and_pending_reader(
    monkeypatch, tmp_path: Path
) -> None:
    from sase.notifications import Notification, store
    from sase_telegram.formatting import format_notification

    monkeypatch.setattr(store, "NOTIFICATIONS_DIR", str(tmp_path))
    monkeypatch.setattr(
        store, "NOTIFICATIONS_FILE", str(tmp_path / "notifications.jsonl")
    )
    prompt = "Full prompt text\n" * 12_800
    store.append_notification(
        Notification(
            id="large-prompt",
            timestamp="2026-11-21T12:00:00+00:00",
            sender="user-agent",
            action="JumpToAgent",
            action_data={
                "prompt": prompt,
                "agent_name": "test-agent",
                "cl_name": "test",
            },
        )
    )
    stored = store.load_notifications()[0]
    assert store.notifications_file_path().stat().st_size < 8 * 1024
    assert stored.action_data["prompt"].startswith("sase-notification-blob:v1:")
    with patch(
        "sase_telegram.formatting.build_fork_copy_text", return_value=None
    ) as fork:
        text, _, _ = format_notification(stored)
    assert fork.call_args.kwargs["prompt"] == prompt
    assert "Full prompt text" in text
    assert "sase-notification-blob" not in text

    monkeypatch.setattr(
        project_context.pending_actions,
        "list_all",
        lambda: {"notification": {"action_data": stored.action_data}},
    )
    assert project_context._iter_pending_prompts() == [prompt]


def test_pending_prompt_reader_loads_prompt_path(monkeypatch, tmp_path: Path) -> None:
    prompt = "#gh:sase continue the work"
    prompt_path = tmp_path / "raw_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    monkeypatch.setattr(
        project_context.pending_actions,
        "list_all",
        lambda: {
            "notification": {
                "action_data": {"prompt_path": str(prompt_path)},
                "created_at": 1,
            }
        },
    )

    assert project_context._iter_pending_prompts() == [prompt]


def test_pending_prompt_reader_resolves_legacy_sidecar(monkeypatch) -> None:
    prompt = "#gh:sase continue the work"
    reference = "sase-notification-blob:v1:deadbeef:29"
    monkeypatch.setattr(
        "sase.notifications.payloads.resolve_notification_value",
        lambda _path, value: prompt if value == reference else None,
    )
    monkeypatch.setattr(
        project_context.pending_actions,
        "list_all",
        lambda: {
            "notification": {
                "action_data": {"prompt": reference},
                "created_at": 1,
            }
        },
    )

    assert project_context._iter_pending_prompts() == [prompt]
