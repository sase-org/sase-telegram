from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sase_telegram.inbound_handlers import project_context


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
