"""Tests for the Telegram /usage command, callbacks, and tick delivery."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _fake_facade(monkeypatch, **overrides):
    import sase_telegram.inbound_handlers.usage_command as usage

    facade = SimpleNamespace(
        USAGE_WINDOWS_REFRESH_TIMEOUT_SECONDS=75.0,
        usage_windows_report=MagicMock(
            return_value=SimpleNamespace(
                collection_enabled=True,
                providers=(),
                configured_providers=("codex",),
                diagnostics=(),
            )
        ),
        resolve_usage_provider=MagicMock(side_effect=lambda name: None),
        request_usage_windows_refresh=MagicMock(),
        live_usage_refresh_operations=MagicMock(return_value=frozenset()),
    )
    for key, value in overrides.items():
        setattr(facade, key, value)
    monkeypatch.setattr(usage, "_usage_facade", lambda: facade)
    return facade


def _callback(chat_id="7", message_id=42):
    message = SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id)
    return SimpleNamespace(id="cb1", data="usage:all:refresh", message=message)


def test_command_routing_dispatches_usage(monkeypatch) -> None:
    from sase_telegram.inbound_handlers.commands import _handle_command
    from tests import inbound_namespace as namespace

    with monkeypatch.context() as patch:
        mock = MagicMock()
        patch.setattr(namespace.INBOUND, "_handle_usage_command", mock)
        _handle_command("/usage codex")
    mock.assert_called_once()


def test_usage_registered_before_update() -> None:
    from sase_telegram.inbound_handlers.commands import _SLASH_COMMANDS

    assert ("usage", "Show LLM usage windows and resets") in _SLASH_COMMANDS
    names = [name for name, _desc in _SLASH_COMMANDS]
    assert names.index("usage") < names.index("update")


def test_usage_is_reserved() -> None:
    from sase_telegram.custom_commands import RESERVED_COMMAND_NAMES

    assert "usage" in RESERVED_COMMAND_NAMES


def test_unknown_filter_suggests_configured(monkeypatch) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    facade = _fake_facade(monkeypatch)
    facade.resolve_usage_provider.side_effect = lambda name: None
    sent: list = []
    monkeypatch.setattr(
        usage.telegram_client,
        "send_message",
        lambda *args, **kwargs: sent.append((args, kwargs)),
    )
    monkeypatch.setattr(usage.credentials, "get_chat_id", lambda: "7")
    usage._handle_usage_command("bogus")
    assert sent
    text = sent[0][0][1]
    assert "Unknown provider" in text
    assert "codex" in text


def test_import_error_fallback(monkeypatch) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    def _missing():
        raise ImportError("No module named 'sase.integrations.usage_windows'")

    monkeypatch.setattr(usage, "_usage_facade", _missing)
    sent: list = []
    monkeypatch.setattr(
        usage.telegram_client,
        "send_message",
        lambda *args, **kwargs: sent.append((args, kwargs)),
    )
    monkeypatch.setattr(usage.credentials, "get_chat_id", lambda: "7")
    usage._handle_usage_command("")
    assert "run /update" in sent[0][0][1]


def test_refresh_started_writes_record(monkeypatch, tmp_path) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    facade = _fake_facade(monkeypatch)
    facade.request_usage_windows_refresh.return_value = SimpleNamespace(
        summary="Refreshing usage: codex",
        operation_ids=("op1",),
        providers=("codex",),
    )
    facade.usage_windows_report.return_value = SimpleNamespace(
        collection_enabled=True,
        providers=(),
        configured_providers=("codex",),
        diagnostics=(),
    )
    monkeypatch.setattr(usage, "_USAGE_REFRESH_PENDING_DIR", tmp_path)
    answered: list = []
    edited: list = []
    monkeypatch.setattr(
        usage, "_answer_callback", lambda query, text: answered.append(text)
    )
    monkeypatch.setattr(
        usage.telegram_client,
        "edit_message_text",
        lambda *args, **kwargs: edited.append((args, kwargs)),
    )
    usage._handle_usage_callback(_callback(), "all", "refresh")
    assert answered and "Refreshing usage" in answered[0]
    assert edited
    records = list(tmp_path.glob("*.json"))
    assert len(records) == 1
    payload = json.loads(records[0].read_text())
    assert payload["operation_ids"] == ["op1"]
    assert payload["scope"] == "all"


def test_refresh_not_started_writes_no_record(monkeypatch, tmp_path) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    facade = _fake_facade(monkeypatch)
    facade.request_usage_windows_refresh.return_value = SimpleNamespace(
        summary="Usage refresh deferred: grok rate limited",
        operation_ids=(),
        providers=(),
    )
    monkeypatch.setattr(usage, "_USAGE_REFRESH_PENDING_DIR", tmp_path)
    monkeypatch.setattr(usage, "_answer_callback", lambda *args: None)
    monkeypatch.setattr(
        usage.telegram_client, "edit_message_text", lambda *args, **kwargs: True
    )
    usage._handle_usage_callback(_callback(), "all", "refresh")
    assert list(tmp_path.glob("*.json")) == []


def test_busy_and_duplicate_taps(monkeypatch, tmp_path) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    _fake_facade(monkeypatch)
    monkeypatch.setattr(usage, "_USAGE_REFRESH_PENDING_DIR", tmp_path)
    answered: list = []
    monkeypatch.setattr(
        usage, "_answer_callback", lambda query, text: answered.append(text)
    )
    usage._handle_usage_callback(_callback(), "all", "busy")
    assert answered == ["Still refreshing…"]
    record = tmp_path / "7_42.json"
    record.write_text(json.dumps({"version": 1}))
    answered.clear()
    usage._handle_usage_callback(_callback(), "all", "refresh")
    assert answered == ["Already refreshing…"]


def test_tick_delivers_settled_and_skips_live(monkeypatch, tmp_path) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    facade = _fake_facade(monkeypatch)
    monkeypatch.setattr(usage, "_USAGE_REFRESH_PENDING_DIR", tmp_path)
    record = {
        "version": 1,
        "chat_id": "7",
        "message_id": 42,
        "scope": "all",
        "operation_ids": ["op1"],
        "providers": ["codex"],
        "submitted_at": 1000.0,
        "deadline_at": 1075.0,
    }
    path = tmp_path / "7_42.json"
    path.write_text(json.dumps(record))
    monkeypatch.setattr(usage.time, "time", lambda: 1010.0)
    facade.live_usage_refresh_operations.return_value = frozenset({"op1"})
    assert usage._finish_ready_usage_refreshes() == 0
    assert path.exists()
    facade.live_usage_refresh_operations.return_value = frozenset()
    edited: list = []
    monkeypatch.setattr(
        usage.telegram_client,
        "edit_message_text",
        lambda *args, **kwargs: edited.append((args, kwargs)),
    )
    assert usage._finish_ready_usage_refreshes() == 1
    assert edited
    assert "Refreshed" in edited[0][0][2]
    assert not path.exists()


def test_tick_timeout_shows_latest_data(monkeypatch, tmp_path) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    facade = _fake_facade(monkeypatch)
    monkeypatch.setattr(usage, "_USAGE_REFRESH_PENDING_DIR", tmp_path)
    record = {
        "version": 1,
        "chat_id": "7",
        "message_id": 42,
        "scope": "all",
        "operation_ids": ["op1"],
        "providers": ["codex"],
        "submitted_at": 1000.0,
        "deadline_at": 1075.0,
    }
    path = tmp_path / "7_42.json"
    path.write_text(json.dumps(record))
    monkeypatch.setattr(usage.time, "time", lambda: 1100.0)
    facade.live_usage_refresh_operations.return_value = frozenset({"op1"})
    edited: list = []
    monkeypatch.setattr(
        usage.telegram_client,
        "edit_message_text",
        lambda *args, **kwargs: edited.append((args, kwargs)),
    )
    assert usage._finish_ready_usage_refreshes() == 1
    assert "Refresh still running for codex after 100s" in edited[0][0][2]
    keyboard = edited[0][1]["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "🔄 Refresh"
    assert not path.exists()


def test_tick_edit_failure_retains_record(monkeypatch, tmp_path) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    facade = _fake_facade(monkeypatch)
    monkeypatch.setattr(usage, "_USAGE_REFRESH_PENDING_DIR", tmp_path)
    record = {
        "version": 1,
        "chat_id": "7",
        "message_id": 42,
        "scope": "all",
        "operation_ids": ["op1"],
        "providers": ["codex"],
        "submitted_at": 1000.0,
        "deadline_at": 1075.0,
    }
    path = tmp_path / "7_42.json"
    path.write_text(json.dumps(record))
    monkeypatch.setattr(usage.time, "time", lambda: 1010.0)
    facade.live_usage_refresh_operations.return_value = frozenset()

    def _boom(*args, **kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(usage.telegram_client, "edit_message_text", _boom)
    assert usage._finish_ready_usage_refreshes() == 0
    assert path.exists()


def test_tick_deletes_expired_and_malformed(monkeypatch, tmp_path) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    _fake_facade(monkeypatch)
    monkeypatch.setattr(usage, "_USAGE_REFRESH_PENDING_DIR", tmp_path)
    expired = tmp_path / "7_42.json"
    expired.write_text(
        json.dumps(
            {
                "version": 1,
                "chat_id": "7",
                "message_id": 42,
                "scope": "all",
                "operation_ids": ["op1"],
                "providers": ["codex"],
                "submitted_at": 1000.0,
                "deadline_at": 1075.0,
            }
        )
    )
    malformed = tmp_path / "7_43.json"
    malformed.write_text("{not json")
    monkeypatch.setattr(usage.time, "time", lambda: 10000.0)
    assert usage._finish_ready_usage_refreshes() == 0
    assert not expired.exists()
    assert not malformed.exists()


def test_unrelated_import_error_is_not_unsupported(monkeypatch) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    def _broken():
        raise ImportError("No module named 'numpy'")

    monkeypatch.setattr(usage, "_usage_facade", _broken)
    sent: list = []
    monkeypatch.setattr(
        usage.telegram_client,
        "send_message",
        lambda *args, **kwargs: sent.append((args, kwargs)),
    )
    monkeypatch.setattr(usage.credentials, "get_chat_id", lambda: "7")
    usage._handle_usage_command("")
    assert sent
    assert "doesn't support /usage" not in sent[0][0][1]
    assert "Failed to build /usage view" in sent[0][0][1]


def test_refresh_started_overflow_writes_record_without_busy_send(
    monkeypatch, tmp_path
) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    facade = _fake_facade(monkeypatch)
    facade.request_usage_windows_refresh.return_value = SimpleNamespace(
        summary="Refreshing usage: codex",
        operation_ids=("op1",),
        providers=("codex",),
    )
    monkeypatch.setattr(usage, "_USAGE_REFRESH_PENDING_DIR", tmp_path)
    monkeypatch.setattr(
        usage, "_render_report_chunks", lambda *args, **kwargs: (["one", "two"], None)
    )
    monkeypatch.setattr(usage, "_answer_callback", lambda *args: None)
    edited: list = []
    sent: list = []
    monkeypatch.setattr(
        usage.telegram_client,
        "edit_message_text",
        lambda *args, **kwargs: edited.append((args, kwargs)),
    )
    monkeypatch.setattr(
        usage.telegram_client,
        "send_message",
        lambda *args, **kwargs: sent.append((args, kwargs)),
    )
    usage._handle_usage_callback(_callback(), "all", "refresh")
    assert edited == []
    assert sent == []
    records = list(tmp_path.glob("*.json"))
    assert len(records) == 1
    facade.live_usage_refresh_operations.return_value = frozenset()
    monkeypatch.setattr(
        usage, "_render_report_chunks", lambda *args, **kwargs: (["one", "two"], None)
    )
    assert usage._finish_ready_usage_refreshes() == 1
    assert len(sent) == 2
    keyboard = sent[-1][1]["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "🔄 Refresh"
    assert list(tmp_path.glob("*.json")) == []


def test_refresh_tap_store_error_edits_unreadable(monkeypatch, tmp_path) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    facade = _fake_facade(monkeypatch)
    facade.request_usage_windows_refresh.return_value = SimpleNamespace(
        summary="Refreshing usage: codex",
        operation_ids=("op1",),
        providers=("codex",),
    )
    facade.usage_windows_report.side_effect = OSError("store gone")
    monkeypatch.setattr(usage, "_USAGE_REFRESH_PENDING_DIR", tmp_path)
    monkeypatch.setattr(usage, "_answer_callback", lambda *args: None)
    edited: list = []
    monkeypatch.setattr(
        usage.telegram_client,
        "edit_message_text",
        lambda *args, **kwargs: edited.append((args, kwargs)),
    )
    usage._handle_usage_callback(_callback(), "all", "refresh")
    assert edited
    assert "Couldn't read usage data" in edited[0][0][2]
    keyboard = edited[0][1]["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "🔄 Refresh"
    assert list(tmp_path.glob("*.json")) == []


def test_settled_tick_store_error_edits_unreadable_and_deletes(
    monkeypatch, tmp_path
) -> None:
    import sase_telegram.inbound_handlers.usage_command as usage

    facade = _fake_facade(monkeypatch)
    facade.usage_windows_report.side_effect = OSError("store gone")
    facade.live_usage_refresh_operations.return_value = frozenset()
    monkeypatch.setattr(usage, "_USAGE_REFRESH_PENDING_DIR", tmp_path)
    record = {
        "version": 1,
        "chat_id": "7",
        "message_id": 42,
        "scope": "all",
        "operation_ids": ["op1"],
        "providers": ["codex"],
        "submitted_at": 1000.0,
        "deadline_at": 1075.0,
    }
    path = tmp_path / "7_42.json"
    path.write_text(json.dumps(record))
    monkeypatch.setattr(usage.time, "time", lambda: 1010.0)
    edited: list = []
    monkeypatch.setattr(
        usage.telegram_client,
        "edit_message_text",
        lambda *args, **kwargs: edited.append((args, kwargs)),
    )
    assert usage._finish_ready_usage_refreshes() == 1
    assert "Couldn't read usage data" in edited[0][0][2]
    keyboard = edited[0][1]["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "🔄 Refresh"
    assert not path.exists()
