"""Integration tests for outbound and inbound entry point scripts."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sase.notifications.models import Notification
from sase_telegram.scripts.sase_tg_outbound import main as outbound_main
from sase_telegram.scripts.sase_tg_inbound import main as inbound_main


LAST_SENT_TEST_FILE = Path("/tmp/test_integration_last_sent_ts")
PENDING_TEST_FILE = Path("/tmp/test_integration_pending.json")
RATE_LIMIT_TEST_FILE = Path("/tmp/test_integration_rate_limit.json")
OFFSET_TEST_FILE = Path("/tmp/test_integration_offset.txt")
AWAITING_TEST_FILE = Path("/tmp/test_integration_awaiting.json")
MEDIA_GROUP_TEST_FILE = Path("/tmp/test_integration_media_groups.json")
OUTBOUND_LOCK_TEST_FILE = Path("/tmp/test_integration_outbound.lock")
UPDATE_COMPLETION_TEST_DIR = Path("/tmp/test_integration_update_completions")
IMAGES_TEST_DIR = Path("/tmp/test_integration_images")
CORE_PENDING_TEST_FILE = Path("/tmp/test_integration_core_pending.json")


def _cleanup_files() -> None:
    for f in [
        LAST_SENT_TEST_FILE,
        PENDING_TEST_FILE,
        RATE_LIMIT_TEST_FILE,
        OFFSET_TEST_FILE,
        AWAITING_TEST_FILE,
        MEDIA_GROUP_TEST_FILE,
        OUTBOUND_LOCK_TEST_FILE,
        CORE_PENDING_TEST_FILE,
    ]:
        f.unlink(missing_ok=True)
    shutil.rmtree(UPDATE_COMPLETION_TEST_DIR, ignore_errors=True)
    shutil.rmtree(IMAGES_TEST_DIR, ignore_errors=True)


def _make_notification(
    id: str = "abcd1234-0000-0000-0000-000000000000",
    action: str | None = None,
    sender: str = "test",
    notes: list[str] | None = None,
    files: list[str] | None = None,
    action_data: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> Notification:
    if timestamp is None:
        timestamp = datetime.now(UTC).isoformat()
    return Notification(
        id=id,
        timestamp=timestamp,
        sender=sender,
        notes=notes or ["Test notification"],
        files=files or [],
        action=action,
        action_data=action_data or {},
    )


@pytest.fixture(autouse=True)
def _patch_paths():
    """Redirect all file paths to temp locations for isolation."""
    patchers = [
        patch("sase_telegram.outbound.LAST_SENT_FILE", LAST_SENT_TEST_FILE),
        patch("sase_telegram.outbound.OUTBOUND_LOCK_FILE", OUTBOUND_LOCK_TEST_FILE),
        patch("sase_telegram.pending_actions.PENDING_ACTIONS_PATH", PENDING_TEST_FILE),
        patch("sase_telegram.rate_limit.RATE_LIMIT_PATH", RATE_LIMIT_TEST_FILE),
        patch("sase_telegram.inbound.UPDATE_OFFSET_PATH", OFFSET_TEST_FILE),
        patch("sase_telegram.inbound.AWAITING_FEEDBACK_PATH", AWAITING_TEST_FILE),
        patch(
            "sase_telegram.scripts.sase_tg_inbound._MEDIA_GROUPS_PATH",
            MEDIA_GROUP_TEST_FILE,
        ),
        patch(
            "sase_telegram.scripts.sase_tg_inbound._UPDATE_COMPLETION_PENDING_DIR",
            UPDATE_COMPLETION_TEST_DIR,
        ),
        # Isolate the shared host pending-action store and point its legacy
        # source at the plugin's test pending file (mirrors production wiring).
        patch(
            "sase.notifications.pending_actions.PENDING_ACTIONS_PATH",
            CORE_PENDING_TEST_FILE,
        ),
        patch(
            "sase.notifications.pending_actions.LEGACY_TELEGRAM_PENDING_ACTIONS_PATH",
            PENDING_TEST_FILE,
        ),
    ]
    for p in patchers:
        p.start()
    yield
    for p in patchers:
        p.stop()
    _cleanup_files()


class TestOutboundIntegration:
    """Integration tests for the outbound main() entry point."""

    @patch("sase_telegram.outbound.load_notifications")
    def test_first_run_initializes_without_sending(
        self,
        mock_load: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """First run creates high-water mark but doesn't send backlog."""
        result = outbound_main(["--dry-run"])
        assert result == 0
        assert LAST_SENT_TEST_FILE.exists()
        mock_load.assert_not_called()
        captured = capsys.readouterr()
        assert "tg_outbound:" in captured.out
        assert "reason=no_unsent_notifications" in captured.out

    @patch("sase_telegram.outbound.try_acquire_outbound_lock", return_value=None)
    def test_lock_held_outputs_skip_summary(
        self,
        _mock_lock: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = outbound_main([])
        assert result == 0
        captured = capsys.readouterr()
        assert "tg_outbound:" in captured.out
        assert "reason=lock_held" in captured.out

    @patch("sase_telegram.scripts.sase_tg_outbound.send_message")
    @patch("sase_telegram.outbound.load_notifications")
    @patch("sase_telegram.scripts.sase_tg_outbound.get_chat_id")
    def test_sends_notification(
        self,
        mock_chat_id: MagicMock,
        mock_load: MagicMock,
        mock_send: MagicMock,
    ) -> None:
        """Full flow: unsent notification -> Telegram message sent."""
        mock_chat_id.return_value = "12345"
        mock_send.return_value = MagicMock(message_id=42)

        # Set up high-water mark in the past
        LAST_SENT_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SENT_TEST_FILE.write_text(
            str(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
        )

        n = _make_notification(
            sender="crs",
            notes=["Workflow completed successfully"],
            timestamp=datetime.now(UTC).isoformat(),
        )
        mock_load.return_value = [n]

        result = outbound_main([])
        assert result == 0
        mock_send.assert_called_once()
        call_args = mock_send.call_args
        # send_message(chat_id, text, reply_markup=keyboard) — text is 2nd positional arg
        assert "Agent Complete" in call_args[0][1]

    @patch("sase_telegram.scripts.sase_tg_outbound.send_message")
    @patch("sase_telegram.outbound.load_notifications")
    @patch("sase_telegram.scripts.sase_tg_outbound.get_chat_id")
    def test_saves_pending_action_for_plan_approval(
        self,
        mock_chat_id: MagicMock,
        mock_load: MagicMock,
        mock_send: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Plan approval notifications are saved as pending actions."""
        mock_chat_id.return_value = "12345"
        mock_send.return_value = MagicMock(message_id=99)

        LAST_SENT_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SENT_TEST_FILE.write_text(
            str(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
        )

        n = _make_notification(
            action="PlanApproval",
            sender="plan",
            notes=["Plan ready"],
            action_data={"response_dir": str(tmp_path), "session_id": "s1"},
            files=[str(tmp_path / "plan.md")],
        )
        mock_load.return_value = [n]

        result = outbound_main([])
        assert result == 0

        # Verify pending action was saved
        from sase_telegram import pending_actions

        pending = pending_actions.list_all()
        assert len(pending) == 1
        prefix = n.id[:8]
        assert prefix in pending
        assert pending[prefix]["action"] == "PlanApproval"
        assert pending[prefix]["message_id"] == 99
        captured = capsys.readouterr()
        assert "tg_outbound:" in captured.out
        assert "unsent=1" in captured.out
        assert "sent=1" in captured.out
        assert "pending_action_writes=1" in captured.out
        assert f"ids={n.id[:8]}" in captured.out

    @patch("sase_telegram.scripts.sase_tg_outbound.send_message")
    @patch("sase_telegram.scripts.sase_tg_outbound.send_document")
    @patch("sase_telegram.scripts.sase_tg_outbound.md_to_pdf")
    @patch("sase_telegram.outbound.load_notifications")
    @patch("sase_telegram.scripts.sase_tg_outbound.get_chat_id")
    def test_saves_pending_action_for_launch_approval(
        self,
        mock_chat_id: MagicMock,
        mock_load: MagicMock,
        mock_md_to_pdf: MagicMock,
        mock_send_document: MagicMock,
        mock_send: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Launch approval notifications are saved as pending actions."""
        mock_chat_id.return_value = "12345"
        mock_send.return_value = MagicMock(message_id=99)
        mock_md_to_pdf.return_value = None
        mock_send_document.return_value = MagicMock(message_id=100)

        LAST_SENT_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SENT_TEST_FILE.write_text(
            str(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
        )

        preview_file = tmp_path / "launch_preview.md"
        preview_file.write_text("# Launch Preview\n")
        n = _make_notification(
            action="LaunchApproval",
            sender="launch",
            notes=["Launch approval requested: 2 slots", "Source: telegram"],
            action_data={
                "response_dir": str(tmp_path / "responses"),
                "request_id": "req_1",
                "source_surface": "telegram",
                "slot_count": "2",
            },
            files=[str(preview_file)],
        )
        mock_load.return_value = [n]

        result = outbound_main([])
        assert result == 0

        from sase_telegram import pending_actions

        pending = pending_actions.list_all()
        prefix = n.id[:8]
        assert pending[prefix]["action"] == "LaunchApproval"
        assert pending[prefix]["action_data"]["request_id"] == "req_1"
        assert pending[prefix]["files"] == [str(preview_file)]
        assert pending[prefix]["message_id"] == 99
        mock_send_document.assert_called()
        captured = capsys.readouterr()
        assert "pending_action_writes=1" in captured.out

    @patch("sase_telegram.scripts.sase_tg_outbound.send_message")
    @patch("sase_telegram.outbound.load_notifications")
    @patch("sase_telegram.scripts.sase_tg_outbound.get_chat_id")
    def test_registers_telegram_transport_in_shared_store(
        self,
        mock_chat_id: MagicMock,
        mock_load: MagicMock,
        mock_send: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Sending a plan notification records a Telegram transport in the store."""
        from sase.notifications import pending_actions as core_pending

        mock_chat_id.return_value = "12345"
        mock_send.return_value = MagicMock(message_id=99)

        LAST_SENT_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SENT_TEST_FILE.write_text(
            str(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
        )

        n = _make_notification(
            action="PlanApproval",
            sender="plan",
            notes=["Plan ready"],
            action_data={"response_dir": str(tmp_path), "session_id": "s1"},
            files=[str(tmp_path / "plan.md")],
        )
        # The host store entry exists because the notification was registered
        # when it was appended; model that here.
        core_pending.register_notification(n, now=10.0)
        mock_load.return_value = [n]

        assert outbound_main([]) == 0

        store = core_pending.read_pending_action_store()
        entry = store["actions"][n.id[:8]]
        telegram = next(t for t in entry["transports"] if t["transport"] == "telegram")
        assert telegram["record"] == {"chat_id": "12345", "message_id": 99}

    @patch("sase_telegram.scripts.sase_tg_outbound.send_message")
    @patch("sase_telegram.outbound.load_notifications")
    @patch("sase_telegram.scripts.sase_tg_outbound.get_chat_id")
    def test_advances_high_water_mark_per_notification(
        self,
        mock_chat_id: MagicMock,
        mock_load: MagicMock,
        mock_send: MagicMock,
    ) -> None:
        """Each successful send advances the high-water mark before the next one.

        Pinning this behavior protects against regression to a "mark all at the
        end" model: if a later notification fails after this test passes,
        earlier notifications must not be re-sent on the next run.
        """
        mock_chat_id.return_value = "12345"
        mock_send.return_value = MagicMock(message_id=42)

        LAST_SENT_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SENT_TEST_FILE.write_text(
            str(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
        )

        ts1 = datetime(2025, 6, 1, tzinfo=UTC).isoformat()
        ts2 = datetime(2025, 6, 2, tzinfo=UTC).isoformat()
        n1 = _make_notification(
            id="n1000000-0000-0000-0000-000000000000", timestamp=ts1
        )
        n2 = _make_notification(
            id="n2000000-0000-0000-0000-000000000000", timestamp=ts2
        )
        mock_load.return_value = [n1, n2]

        # Snapshot the high-water mark each time send_message is called.
        # The first send should advance to ts1 BEFORE the second send fires.
        marks_during_send: list[float] = []

        def _record_mark(*_a: Any, **_kw: Any) -> MagicMock:
            marks_during_send.append(float(LAST_SENT_TEST_FILE.read_text().strip()))
            return MagicMock(message_id=42)

        mock_send.side_effect = _record_mark

        result = outbound_main([])
        assert result == 0
        assert mock_send.call_count == 2

        ts1_epoch = datetime.fromisoformat(ts1).timestamp()
        ts2_epoch = datetime.fromisoformat(ts2).timestamp()
        # Before first send the mark is still the original 2024 floor.
        assert marks_during_send[0] == pytest.approx(
            datetime(2024, 1, 1, tzinfo=UTC).timestamp(), abs=1.0
        )
        # Before the second send the mark has advanced to ts1 — proves
        # the advance happened between sends, not after the loop.
        assert marks_during_send[1] == pytest.approx(ts1_epoch, abs=1.0)
        # After the loop the mark is at ts2.
        final = float(LAST_SENT_TEST_FILE.read_text().strip())
        assert final == pytest.approx(ts2_epoch, abs=1.0)

    @patch("sase_telegram.scripts.sase_tg_outbound.send_message")
    @patch("sase_telegram.outbound.load_notifications")
    @patch("sase_telegram.scripts.sase_tg_outbound.get_chat_id")
    def test_failed_send_does_not_advance_high_water_mark(
        self,
        mock_chat_id: MagicMock,
        mock_load: MagicMock,
        mock_send: MagicMock,
    ) -> None:
        """A send failure on n2 leaves the mark at n1 so n2 retries on next run."""
        mock_chat_id.return_value = "12345"

        LAST_SENT_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SENT_TEST_FILE.write_text(
            str(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
        )

        ts1 = datetime(2025, 6, 1, tzinfo=UTC).isoformat()
        ts2 = datetime(2025, 6, 2, tzinfo=UTC).isoformat()
        n1 = _make_notification(
            id="n1000000-0000-0000-0000-000000000000", timestamp=ts1
        )
        n2 = _make_notification(
            id="n2000000-0000-0000-0000-000000000000", timestamp=ts2
        )
        mock_load.return_value = [n1, n2]
        mock_send.side_effect = [MagicMock(message_id=1), RuntimeError("boom")]

        result = outbound_main([])
        assert result == 0
        # n1 advanced the mark, n2 raised before mark_sent ran for it.
        final = float(LAST_SENT_TEST_FILE.read_text().strip())
        ts1_epoch = datetime.fromisoformat(ts1).timestamp()
        assert final == pytest.approx(ts1_epoch, abs=1.0)

    def test_dry_run_prints_without_sending(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Dry run outputs notification info without calling Telegram API."""
        LAST_SENT_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SENT_TEST_FILE.write_text(
            str(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
        )

        n = _make_notification(sender="crs", notes=["Done!"])

        with (
            patch("sase_telegram.outbound.load_notifications", return_value=[n]),
        ):
            result = outbound_main(["--dry-run"])

        assert result == 0
        captured = capsys.readouterr()
        assert "Notification" in captured.out
        assert n.id in captured.out


class TestInboundIntegration:
    """Integration tests for the inbound main() entry point."""

    def _process_plan_callback(
        self,
        mock_tg: MagicMock,
        tmp_path: Path,
        *,
        choice: str,
        expected_response: dict[str, Any],
        expected_confirmation: str | None,
        expected_answer: str | None = None,
        agent_name: str | None = "plan.agent",
        action_data_extra: dict[str, str] | None = None,
        expected_copy_text: str | None = "#fork:plan.agent ",
        expected_kill: str | None = None,
    ) -> None:
        response_dir = tmp_path / "responses"
        response_dir.mkdir()
        project_dir = tmp_path / "project"
        plan_file = project_dir / "sdd" / "tales" / "plan.md"
        plan_file.parent.mkdir(parents=True)
        plan_file.write_text("# Plan\n")

        from sase_telegram import pending_actions

        action_data = {
            "response_dir": str(response_dir),
            "project_dir": str(project_dir),
        }
        if agent_name is not None:
            action_data["agent_name"] = agent_name
        if action_data_extra:
            action_data.update(action_data_extra)

        pending_actions.add(
            "abcd1234",
            {
                "notification_id": "abcd1234-0000-0000-0000-000000000000",
                "action": "PlanApproval",
                "action_data": action_data,
                "plan_file": str(plan_file),
                "message_id": 42,
                "chat_id": "12345",
            },
        )

        callback_query = SimpleNamespace(
            id="cb_1",
            data=f"plan:abcd1234:{choice}",
            message=SimpleNamespace(message_id=42),
        )
        update = SimpleNamespace(
            update_id=100,
            callback_query=callback_query,
            message=None,
        )
        mock_tg.get_updates.return_value = [update]
        mock_tg.answer_callback_query.return_value = True
        mock_tg.edit_message_reply_markup.return_value = True

        with patch("sase.agent.running.kill_named_agent") as mock_kill:
            result = inbound_main(["--once"])
        assert result == 0

        response_file = response_dir / "plan_response.json"
        assert response_file.exists()
        assert json.loads(response_file.read_text()) == expected_response

        mock_tg.answer_callback_query.assert_called_with(
            "cb_1", expected_answer or expected_confirmation
        )
        mock_tg.edit_message_reply_markup.assert_called_once_with(
            "12345", 42, reply_markup=None
        )

        if expected_confirmation is None:
            mock_tg.send_message.assert_not_called()
        else:
            mock_tg.send_message.assert_called_once()
            chat_id, text = mock_tg.send_message.call_args.args[:2]
            assert chat_id == "12345"
            assert text == expected_confirmation
            keyboard = mock_tg.send_message.call_args.kwargs["reply_markup"]
            if expected_copy_text is None:
                assert keyboard is None
            else:
                copy_button = keyboard.inline_keyboard[0][0]
                assert copy_button.text == "🍴 Fork"
                assert copy_button.copy_text.text == expected_copy_text

        if expected_kill is None:
            mock_kill.assert_not_called()
        else:
            mock_kill.assert_called_once_with(expected_kill)

        assert pending_actions.get("abcd1234") is None

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_no_updates_exits_cleanly(
        self, mock_tg: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When there are no Telegram updates, exits with 0."""
        mock_tg.get_updates.return_value = []
        result = inbound_main(["--once"])
        assert result == 0
        captured = capsys.readouterr()
        assert "tg_inbound:" in captured.out
        assert "updates=0" in captured.out
        assert "reason=no_updates" in captured.out

    def _register_handled_shared_plan(self, response_dir: Path, plan_file: Path) -> str:
        """Seed a plan action that is registered + already_handled in the store."""
        from sase.notifications import pending_actions as core_pending
        from sase.notifications.models import Notification

        notif_id = "abcd1234-0000-0000-0000-000000000000"
        action_data = {
            "response_dir": str(response_dir),
            "agent_name": "plan.agent",
        }
        n = Notification(
            id=notif_id,
            timestamp="2026-05-06T12:00:00+00:00",
            sender="plan",
            files=[str(plan_file)],
            action="PlanApproval",
            action_data=action_data,
        )
        # Far-future timestamps keep the entry from looking stale, so the test
        # exercises the already_handled state rather than deadline expiry.
        core_pending.register_notification(n, now=2_000_000_000.0)
        core_pending.merge_transport_record(
            n.id,
            "telegram",
            {"chat_id": "12345", "message_id": 42},
            now=2_000_000_000.0,
        )
        core_pending.mark_already_handled(
            n.id, source="auto_approve", action="approve", now=2_000_000_001.0
        )
        from sase_telegram import pending_actions

        pending_actions.add(
            "abcd1234",
            {
                "notification_id": notif_id,
                "action": "PlanApproval",
                "action_data": action_data,
                "plan_file": str(plan_file),
                "message_id": 42,
                "chat_id": "12345",
            },
        )
        return notif_id

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_shared_store_handled_dismisses_keyboard(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """An auto-approved plan's stale keyboard is removed via shared state.

        The response dir still looks pending on disk (the auto path never wrote
        a response there), so only the shared already_handled state can drive
        cleanup.
        """
        from sase_telegram import pending_actions

        response_dir = tmp_path / "responses"
        response_dir.mkdir()
        (response_dir / "plan_request.json").write_text("{}")
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Plan\n")
        self._register_handled_shared_plan(response_dir, plan_file)

        mock_tg.get_updates.return_value = []

        assert inbound_main(["--once"]) == 0

        mock_tg.edit_message_reply_markup.assert_called_once_with(
            "12345", 42, reply_markup=None
        )
        assert pending_actions.get("abcd1234") is None
        assert not (response_dir / "plan_response.json").exists()

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_callback_on_already_handled_action_is_rejected(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """A late button press loses the race to an already-resolved plan."""
        from sase_telegram import pending_actions

        response_dir = tmp_path / "responses"
        response_dir.mkdir()
        (response_dir / "plan_request.json").write_text("{}")
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("# Plan\n")
        self._register_handled_shared_plan(response_dir, plan_file)

        callback_query = SimpleNamespace(
            id="cb_1",
            data="plan:abcd1234:approve",
            message=SimpleNamespace(message_id=42),
        )
        update = SimpleNamespace(
            update_id=100, callback_query=callback_query, message=None
        )
        mock_tg.get_updates.return_value = [update]
        mock_tg.answer_callback_query.return_value = True
        mock_tg.edit_message_reply_markup.return_value = True

        assert inbound_main(["--once"]) == 0

        # No competing response file is written behind the resolved action.
        assert not (response_dir / "plan_response.json").exists()
        mock_tg.answer_callback_query.assert_called_once_with(
            "cb_1", "This action has already been handled"
        )
        mock_tg.edit_message_reply_markup.assert_called_once_with(
            "12345", 42, reply_markup=None
        )
        assert pending_actions.get("abcd1234") is None

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_legacy_filesystem_handled_still_cleaned(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Legacy records with no shared state still clean via filesystem checks."""
        from sase_telegram import pending_actions

        response_dir = tmp_path / "responses"
        response_dir.mkdir()
        (response_dir / "plan_request.json").write_text("{}")
        # Handled on disk, but never registered in the shared store.
        (response_dir / "plan_response.json").write_text("{}")
        pending_actions.add(
            "abcd1234",
            {
                "notification_id": "abcd1234-0000-0000-0000-000000000000",
                "action": "PlanApproval",
                "action_data": {"response_dir": str(response_dir)},
                "message_id": 42,
                "chat_id": "12345",
            },
        )
        mock_tg.get_updates.return_value = []

        assert inbound_main(["--once"]) == 0

        mock_tg.edit_message_reply_markup.assert_called_once_with(
            "12345", 42, reply_markup=None
        )
        assert pending_actions.get("abcd1234") is None

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_processes_plan_approve_callback(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Full flow: plan approve callback -> response file and fork button."""
        self._process_plan_callback(
            mock_tg,
            tmp_path,
            choice="approve",
            expected_response={"action": "approve"},
            expected_confirmation="Plan approved",
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_processes_plan_approve_callback_with_vcs_fork_text(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Plan confirmation fork text carries the current ChangeSpec VCS ref."""
        self._process_plan_callback(
            mock_tg,
            tmp_path,
            choice="approve",
            expected_response={"action": "approve"},
            expected_confirmation="Plan approved",
            action_data_extra={
                "agent_cl_name": "sase_foobar_1",
                "agent_vcs_tag": "#gh:sase ",
            },
            expected_copy_text="#gh:sase_foobar_1 #fork:plan.agent ",
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_processes_plan_approve_callback_without_agent_name_has_no_button(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Legacy pending actions without an agent name get text only."""
        self._process_plan_callback(
            mock_tg,
            tmp_path,
            choice="approve",
            expected_response={"action": "approve"},
            expected_confirmation="Plan approved",
            agent_name=None,
            expected_copy_text=None,
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_processes_plan_run_callback_without_killing_agent(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Full flow: plan run callback -> response file and no agent kill."""
        self._process_plan_callback(
            mock_tg,
            tmp_path,
            choice="run",
            expected_response={
                "action": "approve",
                "commit_plan": False,
                "run_coder": True,
            },
            expected_confirmation="Plan approved",
            expected_answer="Running coder (no commit)",
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_processes_plan_reject_callback_kills_agent(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Full flow: plan reject callback -> response file, cleanup, and kill."""
        self._process_plan_callback(
            mock_tg,
            tmp_path,
            choice="reject",
            expected_response={"action": "reject"},
            expected_confirmation=None,
            expected_answer="Plan rejected",
            agent_name="9u.cld",
            expected_kill="9u.cld",
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_processes_plan_epic_callback_sends_fork_confirmation(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Full flow: plan epic callback -> response file and fork button."""
        self._process_plan_callback(
            mock_tg,
            tmp_path,
            choice="epic",
            expected_response={"action": "epic"},
            expected_confirmation="Epic created",
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_processes_plan_legend_callback_sends_fork_confirmation(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Full flow: plan legend callback -> response file and fork button."""
        self._process_plan_callback(
            mock_tg,
            tmp_path,
            choice="legend",
            expected_response={"action": "legend"},
            expected_confirmation="Legend created",
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_plan_feedback_callback_does_not_kill_agent(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Plan feedback starts the two-step flow and does not kill the agent."""
        from sase_telegram import pending_actions
        from sase_telegram.inbound import load_awaiting_feedback

        response_dir = tmp_path / "responses"
        response_dir.mkdir()
        (response_dir / "plan_request.json").write_text("{}")
        pending_actions.add(
            "abcd1234",
            {
                "notification_id": "abcd1234-0000-0000-0000-000000000000",
                "action": "PlanApproval",
                "action_data": {
                    "response_dir": str(response_dir),
                    "agent_name": "9u.cld",
                },
                "message_id": 42,
                "chat_id": "12345",
            },
        )
        callback_query = SimpleNamespace(
            id="cb_feedback",
            data="plan:abcd1234:feedback",
            message=SimpleNamespace(message_id=42),
        )
        update = SimpleNamespace(
            update_id=101,
            callback_query=callback_query,
            message=None,
        )
        mock_tg.get_updates.return_value = [update]
        mock_tg.answer_callback_query.return_value = True
        mock_tg.edit_message_reply_markup.return_value = True

        with patch("sase.agent.running.kill_named_agent") as mock_kill:
            result = inbound_main(["--once"])

        assert result == 0
        assert not (response_dir / "plan_response.json").exists()
        awaiting = load_awaiting_feedback("42")
        assert awaiting is not None
        assert awaiting["prefix"] == "abcd1234"
        assert awaiting["action_info"] == {
            "action_type": "plan",
            "response_dir": str(response_dir),
        }
        mock_tg.answer_callback_query.assert_called_with(
            "cb_feedback", "Send your feedback as a text message"
        )
        mock_tg.edit_message_reply_markup.assert_called_once_with(
            "12345", 42, reply_markup=None
        )
        mock_kill.assert_not_called()
        assert pending_actions.get("abcd1234") is not None

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_processes_hitl_accept_callback(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Full flow: HITL accept callback -> response file written."""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        from sase_telegram import pending_actions

        pending_actions.add(
            "hitl0001",
            {
                "notification_id": "hitl0001-0000-0000-0000-000000000000",
                "action": "HITL",
                "action_data": {"artifacts_dir": str(artifacts_dir)},
                "message_id": 42,
                "chat_id": "12345",
            },
        )

        callback_query = SimpleNamespace(
            id="cb_2",
            data="hitl:hitl0001:accept",
            message=SimpleNamespace(message_id=42),
        )
        update = SimpleNamespace(
            update_id=200,
            callback_query=callback_query,
            message=None,
        )
        mock_tg.get_updates.return_value = [update]
        mock_tg.answer_callback_query.return_value = True
        mock_tg.edit_message_reply_markup.return_value = True

        result = inbound_main(["--once"])
        assert result == 0

        response_file = artifacts_dir / "hitl_response.json"
        assert response_file.exists()
        data = json.loads(response_file.read_text())
        assert data == {"action": "accept", "approved": True}

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_processes_launch_approve_callback_through_executor(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Launch approve callback resolves through the shared host executor."""
        response_dir = tmp_path / "launch"
        response_dir.mkdir()
        (response_dir / "launch_request.json").write_text("{}")

        from sase_telegram import pending_actions

        pending_actions.add(
            "lnch0001",
            {
                "notification_id": "lnch0001-0000-0000-0000-000000000000",
                "action": "LaunchApproval",
                "action_data": {
                    "response_dir": str(response_dir),
                    "request_id": "req_1",
                },
                "message_id": 42,
                "chat_id": "12345",
            },
        )

        callback_query = SimpleNamespace(
            id="cb_launch",
            data="launch:lnch0001:approve",
            message=SimpleNamespace(message_id=42),
        )
        update = SimpleNamespace(
            update_id=210,
            callback_query=callback_query,
            message=None,
        )
        mock_tg.get_updates.return_value = [update]
        mock_tg.answer_callback_query.return_value = True
        mock_tg.edit_message_reply_markup.return_value = True

        with patch(
            "sase.agent.launch_request.dispatch_approved_launch_request",
            return_value=SimpleNamespace(launched_count=2),
        ) as mock_dispatch:
            result = inbound_main(["--once"])
        assert result == 0

        mock_dispatch.assert_called_once_with(response_dir)
        response_file = response_dir / "launch_response.json"
        assert response_file.exists()
        assert json.loads(response_file.read_text()) == {
            "action": "approve",
            "dispatch_status": "launched",
            "launched_count": 2,
        }
        mock_tg.answer_callback_query.assert_called_with(
            "cb_launch", "Launch approved and dispatched 2 agents"
        )
        mock_tg.edit_message_reply_markup.assert_called_once_with(
            "12345", 42, reply_markup=None
        )
        assert pending_actions.get("lnch0001") is None

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_launch_callback_conflict_is_answered(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """A late LaunchApproval callback gets a deterministic handled answer."""
        response_dir = tmp_path / "launch"
        response_dir.mkdir()

        from sase_telegram import pending_actions

        pending_actions.add(
            "lnch0001",
            {
                "notification_id": "lnch0001-0000-0000-0000-000000000000",
                "action": "LaunchApproval",
                "action_data": {"response_dir": str(response_dir)},
                "message_id": 42,
                "chat_id": "12345",
            },
        )
        callback_query = SimpleNamespace(
            id="cb_launch",
            data="launch:lnch0001:approve",
            message=SimpleNamespace(message_id=42),
        )
        mock_tg.get_updates.return_value = [
            SimpleNamespace(update_id=211, callback_query=callback_query, message=None)
        ]
        mock_tg.answer_callback_query.return_value = True
        mock_tg.edit_message_reply_markup.return_value = True

        assert inbound_main(["--once"]) == 0

        mock_tg.answer_callback_query.assert_called_with(
            "cb_launch", "This action has already been handled"
        )
        mock_tg.edit_message_reply_markup.assert_called_once_with(
            "12345", 42, reply_markup=None
        )
        assert pending_actions.get("lnch0001") is None

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_hitl_feedback_twostep_flow(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Two-step flow: feedback button -> text message -> response file."""
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        from sase_telegram import pending_actions

        pending_actions.add(
            "hitl0001",
            {
                "notification_id": "hitl0001-0000-0000-0000-000000000000",
                "action": "HITL",
                "action_data": {"artifacts_dir": str(artifacts_dir)},
                "message_id": 42,
                "chat_id": "12345",
            },
        )

        # Step 1: Feedback button press
        callback_query = SimpleNamespace(
            id="cb_3",
            data="hitl:hitl0001:feedback",
            message=SimpleNamespace(message_id=42),
        )
        update1 = SimpleNamespace(
            update_id=300,
            callback_query=callback_query,
            message=None,
        )
        mock_tg.get_updates.return_value = [update1]
        mock_tg.answer_callback_query.return_value = True
        mock_tg.edit_message_reply_markup.return_value = True

        inbound_main(["--once"])

        # Verify awaiting feedback state was saved
        assert AWAITING_TEST_FILE.exists()
        mock_tg.answer_callback_query.assert_called_with(
            "cb_3", "Send your feedback as a text message"
        )

        # Step 2: Text message with feedback
        text_msg = SimpleNamespace(
            text="Please fix the indentation on line 42",
            photo=None,
            document=None,
            entities=None,
            message_id=100,
        )
        update2 = SimpleNamespace(
            update_id=301,
            callback_query=None,
            message=text_msg,
        )
        mock_tg.get_updates.return_value = [update2]

        inbound_main(["--once"])

        # Verify response file
        response_file = artifacts_dir / "hitl_response.json"
        assert response_file.exists()
        data = json.loads(response_file.read_text())
        assert data["action"] == "feedback"
        assert data["approved"] is False
        assert data["feedback"] == "Please fix the indentation on line 42"

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_concurrent_feedback_flows_do_not_overwrite(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Two pending two-step flows resolve independently via reply_to_message."""
        from sase_telegram import pending_actions
        from sase_telegram.inbound import load_all_awaiting_feedback

        artifacts_a = tmp_path / "a"
        artifacts_a.mkdir()
        artifacts_b = tmp_path / "b"
        artifacts_b.mkdir()

        pending_actions.add(
            "hitlAAAA",
            {
                "notification_id": "hitlAAAA-0000-0000-0000-000000000000",
                "action": "HITL",
                "action_data": {"artifacts_dir": str(artifacts_a)},
                "message_id": 42,
                "chat_id": "12345",
            },
        )
        pending_actions.add(
            "hitlBBBB",
            {
                "notification_id": "hitlBBBB-0000-0000-0000-000000000000",
                "action": "HITL",
                "action_data": {"artifacts_dir": str(artifacts_b)},
                "message_id": 43,
                "chat_id": "12345",
            },
        )

        # Step 1: Press feedback button on message 42.
        cb_a = SimpleNamespace(
            id="cb_a",
            data="hitl:hitlAAAA:feedback",
            message=SimpleNamespace(message_id=42),
        )
        mock_tg.get_updates.return_value = [
            SimpleNamespace(update_id=600, callback_query=cb_a, message=None)
        ]
        mock_tg.answer_callback_query.return_value = True
        mock_tg.edit_message_reply_markup.return_value = True
        inbound_main(["--once"])

        # Step 2: Press feedback button on message 43 — must NOT overwrite A.
        cb_b = SimpleNamespace(
            id="cb_b",
            data="hitl:hitlBBBB:feedback",
            message=SimpleNamespace(message_id=43),
        )
        mock_tg.get_updates.return_value = [
            SimpleNamespace(update_id=601, callback_query=cb_b, message=None)
        ]
        inbound_main(["--once"])

        all_aw = load_all_awaiting_feedback()
        assert {"42", "43"} <= set(all_aw)
        assert all_aw["42"]["prefix"] == "hitlAAAA"
        assert all_aw["43"]["prefix"] == "hitlBBBB"

        # Step 3: Reply targeting message 43 — completes B, leaves A intact.
        text_msg = SimpleNamespace(
            text="fix B please",
            photo=None,
            document=None,
            entities=None,
            message_id=200,
            reply_to_message=SimpleNamespace(message_id=43),
        )
        mock_tg.get_updates.return_value = [
            SimpleNamespace(update_id=602, callback_query=None, message=text_msg)
        ]
        inbound_main(["--once"])

        response_b = artifacts_b / "hitl_response.json"
        assert response_b.exists()
        assert json.loads(response_b.read_text())["feedback"] == "fix B please"
        # A's response file must NOT have been written.
        assert not (artifacts_a / "hitl_response.json").exists()
        # A's awaiting entry survives.
        remaining = load_all_awaiting_feedback()
        assert "42" in remaining
        assert remaining["42"]["prefix"] == "hitlAAAA"
        assert "43" not in remaining

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_expired_action_handled_gracefully(self, mock_tg: MagicMock) -> None:
        """Callback for expired action returns 'expired' message."""
        from sase_telegram import pending_actions

        # Add pending action with a non-existent response dir
        pending_actions.add(
            "gone0001",
            {
                "notification_id": "gone0001-0000-0000-0000-000000000000",
                "action": "PlanApproval",
                "action_data": {"response_dir": "/nonexistent/dir"},
                "message_id": 42,
                "chat_id": "12345",
            },
        )

        callback_query = SimpleNamespace(
            id="cb_4",
            data="plan:gone0001:approve",
            message=SimpleNamespace(message_id=42),
        )
        update = SimpleNamespace(
            update_id=400,
            callback_query=callback_query,
            message=None,
        )
        mock_tg.get_updates.return_value = [update]
        mock_tg.answer_callback_query.return_value = True

        result = inbound_main(["--once"])
        assert result == 0

        # Verify the "expired" response was sent
        mock_tg.answer_callback_query.assert_called_with(
            "cb_4", "This request has expired"
        )

        # Pending action should be cleaned up
        assert pending_actions.get("gone0001") is None

    @patch("sase_telegram.scripts.sase_tg_inbound._launch_agent")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_saves_offset_after_processing(
        self,
        mock_tg: MagicMock,
        _mock_launch: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Offset file is updated after processing updates."""
        text_msg = SimpleNamespace(
            text="random message",
            photo=None,
            document=None,
            entities=None,
            message_id=500,
        )
        update = SimpleNamespace(
            update_id=500,
            callback_query=None,
            message=text_msg,
        )
        mock_tg.get_updates.return_value = [update]

        inbound_main(["--once"])

        assert OFFSET_TEST_FILE.exists()
        offset = int(OFFSET_TEST_FILE.read_text().strip())
        assert offset == 501  # update_id + 1
        captured = capsys.readouterr()
        assert "tg_inbound:" in captured.out
        assert "updates=1" in captured.out
        assert "text=1" in captured.out
        assert "next_offset=501" in captured.out

    @patch("sase_telegram.scripts.sase_tg_inbound._launch_agent")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_photo_message_downloads_and_launches_agent(
        self,
        mock_tg: MagicMock,
        mock_creds: MagicMock,
        mock_launch: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Full flow: photo update -> download -> agent launched with correct prompt."""
        mock_creds.get_chat_id.return_value = "12345"
        mock_tg.download_file.return_value = tmp_path / "photo.jpg"

        photo = SimpleNamespace(file_id="integtest_id_12345678")
        message = SimpleNamespace(
            photo=[photo],
            caption="What is this diagram?",
            caption_entities=None,
            text=None,
            document=None,
        )
        update = SimpleNamespace(
            update_id=600,
            callback_query=None,
            message=message,
        )
        mock_tg.get_updates.return_value = [update]

        with patch(
            "sase_telegram.scripts.sase_tg_inbound.IMAGES_DIR",
            tmp_path,
        ):
            result = inbound_main(["--once"])

        assert result == 0

        # Photo should have been downloaded
        mock_tg.download_file.assert_called_once()
        call_args = mock_tg.download_file.call_args
        assert call_args[0][0] == "integtest_id_12345678"

        # Agent should have been launched with a prompt referencing the image
        mock_launch.assert_called_once()
        prompt = mock_launch.call_args[0][0]
        assert "What is this diagram?" in prompt
        assert str(tmp_path) in prompt

        # Offset should have been saved
        assert OFFSET_TEST_FILE.exists()
        offset = int(OFFSET_TEST_FILE.read_text().strip())
        assert offset == 601

    @patch("sase_telegram.scripts.sase_tg_inbound._launch_agent")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_photo_album_stages_then_flushes_one_launch(
        self,
        mock_tg: MagicMock,
        mock_launch: MagicMock,
    ) -> None:
        """Media-group photos become one later launch containing both paths."""
        from sase_telegram.scripts import sase_tg_inbound as inbound

        def _download(_file_id: str, dest: Path) -> None:
            dest.write_text("image")

        mock_tg.download_file.side_effect = _download
        first = SimpleNamespace(file_id="album_one_12345678")
        second = SimpleNamespace(file_id="album_two_12345678")
        message1 = SimpleNamespace(
            photo=[first],
            caption="Compare these",
            caption_entities=None,
            media_group_id="album-1",
            message_id=10,
            chat=SimpleNamespace(id=12345),
            text=None,
            document=None,
        )
        message2 = SimpleNamespace(
            photo=[second],
            caption=None,
            caption_entities=None,
            media_group_id="album-1",
            message_id=11,
            chat=SimpleNamespace(id=12345),
            text=None,
            document=None,
        )
        mock_tg.get_updates.return_value = [
            SimpleNamespace(update_id=800, callback_query=None, message=message1),
            SimpleNamespace(update_id=801, callback_query=None, message=message2),
        ]

        with (
            patch.object(inbound, "IMAGES_DIR", IMAGES_TEST_DIR),
            patch.object(inbound, "_register_commands_if_needed"),
            patch.object(
                inbound.time,
                "time",
                side_effect=[100.0, 100.5, 100.5, 100.5, 100.5],
            ),
        ):
            assert inbound_main(["--once"]) == 0

        mock_launch.assert_not_called()
        assert MEDIA_GROUP_TEST_FILE.exists()
        assert int(OFFSET_TEST_FILE.read_text().strip()) == 802

        mock_tg.get_updates.return_value = []
        with (
            patch.object(inbound, "IMAGES_DIR", IMAGES_TEST_DIR),
            patch.object(inbound, "_register_commands_if_needed"),
            patch.object(inbound.time, "time", return_value=103.0),
        ):
            assert inbound_main(["--once"]) == 0

        mock_launch.assert_called_once()
        prompt = mock_launch.call_args.args[0]
        assert "Compare these" in prompt
        assert "1. " in prompt and "album_one_1" in prompt
        assert "2. " in prompt and "album_two_1" in prompt
        assert not MEDIA_GROUP_TEST_FILE.exists()
        assert mock_tg.download_file.call_count == 2
