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
from sase_telegram.receiver_runtime import RuntimeGeneration, RuntimeScanError
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


def _cursor_epoch() -> float:
    payload = json.loads(LAST_SENT_TEST_FILE.read_text(encoding="utf-8"))
    return datetime.fromisoformat(str(payload["activity_at"])).timestamp()


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
        patch(
            "sase_telegram.pending_actions.PENDING_ACTIONS_PATH",
            CORE_PENDING_TEST_FILE,
        ),
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
        patch(
            "sase_telegram.scripts.sase_tg_inbound.load_custom_commands",
            return_value={},
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

    @patch("sase_telegram.outbound._read_current_notification_snapshot")
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
    @patch("sase_telegram.outbound._read_current_notification_snapshot")
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
    @patch("sase_telegram.outbound._read_current_notification_snapshot")
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
    @patch("sase_telegram.outbound._read_current_notification_snapshot")
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
    @patch("sase_telegram.outbound._read_current_notification_snapshot")
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
        assert telegram["record"]["chat_id"] == "12345"
        assert telegram["record"]["message_id"] == 99
        assert telegram["record"]["action"] == "PlanApproval"
        assert telegram["record"]["action_data"] == n.action_data
        assert telegram["record"]["files"] == [str(tmp_path / "plan.md")]

    @patch("sase_telegram.scripts.sase_tg_outbound.send_message")
    @patch("sase_telegram.outbound._read_current_notification_snapshot")
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
            marks_during_send.append(_cursor_epoch())
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
        final = _cursor_epoch()
        assert final == pytest.approx(ts2_epoch, abs=1.0)

    @patch("sase_telegram.scripts.sase_tg_outbound.send_message")
    @patch("sase_telegram.outbound._read_current_notification_snapshot")
    @patch("sase_telegram.scripts.sase_tg_outbound.get_chat_id")
    def test_failed_send_does_not_advance_high_water_mark(
        self,
        mock_chat_id: MagicMock,
        mock_load: MagicMock,
        mock_send: MagicMock,
    ) -> None:
        """A send failure on n2 leaves the mark at n1 and blocks later sends."""
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
        n3 = _make_notification(
            id="n3000000-0000-0000-0000-000000000000",
            timestamp=datetime(2025, 6, 3, tzinfo=UTC).isoformat(),
        )
        mock_load.return_value = [n1, n2, n3]
        mock_send.side_effect = [MagicMock(message_id=1), RuntimeError("boom")]

        result = outbound_main([])
        assert result == 0
        assert mock_send.call_count == 2
        # n1 advanced the mark, n2 raised before mark_sent ran for it.
        final = _cursor_epoch()
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
            patch(
                "sase_telegram.outbound._read_current_notification_snapshot",
                return_value=[n],
            ),
        ):
            result = outbound_main(["--dry-run"])

        assert result == 0
        captured = capsys.readouterr()
        assert "Notification" in captured.out
        assert n.id in captured.out


class TestInboundIntegration:
    """Integration tests for the inbound main() entry point."""

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

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_custom_commands_load_once_and_dispatch(self, mock_tg: MagicMock) -> None:
        from sase_telegram.custom_commands import CustomCommand

        command = CustomCommand(
            name="tasks",
            description="Tasks dashboard",
            argv=("tg_cmd_tasks",),
            output="message",
            timeout_seconds=60,
        )
        message = SimpleNamespace(
            photo=None,
            document=None,
            text="/tasks ready only",
            entities=None,
            message_id=42,
            reply_to_message=None,
        )
        mock_tg.get_updates.return_value = [
            SimpleNamespace(
                update_id=100,
                callback_query=None,
                message=message,
            )
        ]

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound.load_custom_commands",
                return_value={"tasks": command},
            ) as load,
            patch(
                "sase_telegram.scripts.sase_tg_inbound._handle_custom_command"
            ) as handle,
        ):
            assert inbound_main(["--once"]) == 0

        load.assert_called_once_with()
        handle.assert_called_once_with(command, "ready only")

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
            data="gate:abcd1234:c0",
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
            chat=SimpleNamespace(id=12345),
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
            # Control this poll's album clock without replacing the process-wide
            # clock used by pending-action cleanup and other dependencies.
            patch.object(
                inbound,
                "time",
                SimpleNamespace(time=lambda: 100.5),
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
            patch.object(inbound, "time", SimpleNamespace(time=lambda: 103.0)),
        ):
            assert inbound_main(["--once"]) == 0

        mock_launch.assert_called_once()
        prompt = mock_launch.call_args.args[0]
        assert "Compare these" in prompt
        assert "1. " in prompt and "album_one_1" in prompt
        assert "2. " in prompt and "album_two_1" in prompt
        assert not MEDIA_GROUP_TEST_FILE.exists()
        assert mock_tg.download_file.call_count == 2

    @patch("sase_telegram.scripts.sase_tg_inbound._handle_text_message")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_foreign_chat_text_message_is_rejected_before_dispatch(
        self,
        mock_tg: MagicMock,
        mock_creds: MagicMock,
        mock_handle_text: MagicMock,
    ) -> None:
        """A stranger's text message never reaches the handler."""
        mock_creds.get_chat_id.return_value = "12345"
        message = SimpleNamespace(
            text="launch something",
            photo=None,
            document=None,
            entities=None,
            message_id=1,
            chat=SimpleNamespace(id=99999),
        )
        update = SimpleNamespace(update_id=700, callback_query=None, message=message)
        mock_tg.get_updates.return_value = [update]

        assert inbound_main(["--once"]) == 0

        mock_handle_text.assert_not_called()
        assert int(OFFSET_TEST_FILE.read_text().strip()) == 701

    @patch("sase_telegram.scripts.sase_tg_inbound._handle_photo_message")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_foreign_chat_photo_message_is_rejected_before_dispatch(
        self,
        mock_tg: MagicMock,
        mock_creds: MagicMock,
        mock_handle_photo: MagicMock,
    ) -> None:
        """A stranger's photo message never reaches the handler."""
        mock_creds.get_chat_id.return_value = "12345"
        photo = SimpleNamespace(file_id="stranger_photo_id")
        message = SimpleNamespace(
            photo=[photo],
            caption=None,
            caption_entities=None,
            text=None,
            document=None,
            media_group_id=None,
            chat=SimpleNamespace(id=99999),
        )
        update = SimpleNamespace(update_id=701, callback_query=None, message=message)
        mock_tg.get_updates.return_value = [update]

        assert inbound_main(["--once"]) == 0

        mock_handle_photo.assert_not_called()

    @patch("sase_telegram.scripts.sase_tg_inbound._handle_document_image")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_foreign_chat_document_image_is_rejected_before_dispatch(
        self,
        mock_tg: MagicMock,
        mock_creds: MagicMock,
        mock_handle_document: MagicMock,
    ) -> None:
        """A stranger's document-image message never reaches the handler."""
        mock_creds.get_chat_id.return_value = "12345"
        document = SimpleNamespace(mime_type="image/png", file_name="x.png")
        message = SimpleNamespace(
            photo=None,
            document=document,
            caption=None,
            caption_entities=None,
            text=None,
            media_group_id=None,
            chat=SimpleNamespace(id=99999),
        )
        update = SimpleNamespace(update_id=702, callback_query=None, message=message)
        mock_tg.get_updates.return_value = [update]

        assert inbound_main(["--once"]) == 0

        mock_handle_document.assert_not_called()

    @patch("sase_telegram.scripts.sase_tg_inbound._handle_callback")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_foreign_chat_callback_is_rejected_before_dispatch(
        self,
        mock_tg: MagicMock,
        mock_creds: MagicMock,
        mock_handle_callback: MagicMock,
    ) -> None:
        """A stranger's callback query never reaches the handler."""
        mock_creds.get_chat_id.return_value = "12345"
        callback_query = SimpleNamespace(
            id="cb_stranger",
            data="gate:abcd1234:c0",
            message=SimpleNamespace(message_id=42, chat=SimpleNamespace(id=99999)),
            from_user=SimpleNamespace(id=99999),
        )
        update = SimpleNamespace(
            update_id=703, callback_query=callback_query, message=None
        )
        mock_tg.get_updates.return_value = [update]

        assert inbound_main(["--once"]) == 0

        mock_handle_callback.assert_not_called()

    @patch("sase_telegram.scripts.sase_tg_inbound._handle_callback")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_own_chat_callback_from_a_different_sender_is_rejected(
        self,
        mock_tg: MagicMock,
        mock_creds: MagicMock,
        mock_handle_callback: MagicMock,
    ) -> None:
        """Matching chat alone is not enough -- the presser is checked too."""
        mock_creds.get_chat_id.return_value = "12345"
        callback_query = SimpleNamespace(
            id="cb_stranger",
            data="gate:abcd1234:c0",
            message=SimpleNamespace(message_id=42, chat=SimpleNamespace(id=12345)),
            from_user=SimpleNamespace(id=99999),
        )
        update = SimpleNamespace(
            update_id=704, callback_query=callback_query, message=None
        )
        mock_tg.get_updates.return_value = [update]

        assert inbound_main(["--once"]) == 0

        mock_handle_callback.assert_not_called()

    @patch("sase_telegram.scripts.sase_tg_inbound._handle_callback")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_configured_chat_callback_reaches_the_handler(
        self,
        mock_tg: MagicMock,
        mock_creds: MagicMock,
        mock_handle_callback: MagicMock,
    ) -> None:
        """The owner's own chat and account are not rejected."""
        mock_creds.get_chat_id.return_value = "12345"
        callback_query = SimpleNamespace(
            id="cb_owner",
            data="gate:abcd1234:c0",
            message=SimpleNamespace(message_id=42, chat=SimpleNamespace(id=12345)),
            from_user=SimpleNamespace(id=12345),
        )
        update = SimpleNamespace(
            update_id=705, callback_query=callback_query, message=None
        )
        mock_tg.get_updates.return_value = [update]

        assert inbound_main(["--once"]) == 0

        mock_handle_callback.assert_called_once()


class TestInboundChopTick:
    """The default (bare) chop invocation: local cleanup + ensure-receiver.

    Network polling for updates now belongs to the persistent ``--receiver``
    proc, not this short-lived five-second tick -- these tests pin down that
    split so local cleanup (keyboard-removal retries, completion delivery)
    stays independent of however long the receiver's long poll is waiting.
    """

    @patch("sase_telegram.scripts.sase_tg_inbound.ensure_receiver_running")
    @patch("sase_telegram.scripts.sase_tg_inbound._retry_pending_keyboard_cleanups")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_bare_invocation_never_polls_and_ensures_the_receiver(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_retry_cleanup: MagicMock,
        mock_ensure: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_creds.get_bot_token.return_value = "token"

        assert inbound_main([]) == 0

        mock_tg.get_updates.assert_not_called()
        mock_retry_cleanup.assert_called_once_with()
        mock_ensure.assert_called_once_with()
        captured = capsys.readouterr()
        assert "reason=receiver_ensured" in captured.out
        assert "updates=0" in captured.out

    @patch("sase_telegram.scripts.sase_tg_inbound.ensure_receiver_running")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_bare_invocation_propagates_credential_error_without_ensuring(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_ensure: MagicMock,
    ) -> None:
        from sase_telegram.credentials import TelegramCredentialError

        mock_creds.get_bot_token.side_effect = TelegramCredentialError("no token")

        with pytest.raises(TelegramCredentialError):
            inbound_main([])

        mock_tg.get_updates.assert_not_called()
        mock_ensure.assert_not_called()


def _runtime_generation(digest: str = "stable") -> RuntimeGeneration:
    return RuntimeGeneration(
        digest=digest,
        executable="/venv/bin/sase_job_tg_inbound",
        roots=(("sase", "/sase"),),
    )


class TestReceiverLoop:
    """The persistent ``--receiver`` loop: durable per-update offsets."""

    @pytest.fixture(autouse=True)
    def _stable_runtime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        generation = _runtime_generation()
        monkeypatch.setattr(
            "sase_telegram.scripts.sase_tg_inbound.observe_runtime_generation",
            lambda: generation,
        )

    @patch("sase_telegram.scripts.sase_tg_inbound._launch_agent")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_offset_advances_per_update_past_a_poisoned_one(
        self,
        mock_tg: MagicMock,
        mock_launch: MagicMock,
    ) -> None:
        """A handler exception on one update does not wedge the batch.

        Matches a real process crash's effective behavior (the failing
        update is skipped, not retried forever) while still committing the
        offset per update rather than once for the whole batch, so a killed
        receiver cannot silently lose an update that never got this far.
        """
        from sase_telegram.scripts import sase_tg_inbound as inbound

        first = SimpleNamespace(
            update_id=900,
            callback_query=None,
            message=SimpleNamespace(
                text="first", photo=None, document=None, entities=None, message_id=1
            ),
        )
        second = SimpleNamespace(
            update_id=901,
            callback_query=None,
            message=SimpleNamespace(
                text="second", photo=None, document=None, entities=None, message_id=2
            ),
        )
        mock_tg.get_updates.return_value = [first, second]
        mock_launch.side_effect = [RuntimeError("boom"), None]

        result = inbound._poll_and_dispatch_updates(None, timeout=0, custom_commands={})

        assert mock_launch.call_count == 2
        assert result.next_offset == 902
        assert int(OFFSET_TEST_FILE.read_text().strip()) == 902
        # The poisoned update's handler raised, so it is not counted, but
        # processing continued to (and counted) the next one.
        assert result.counts["text"] == 1

    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.is_telegram_enabled")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_receiver_self_terminates_when_disabled(
        self,
        mock_tg: MagicMock,
        mock_enabled: MagicMock,
        mock_creds: MagicMock,
    ) -> None:
        """The receiver responds to config changes by exiting on its own.

        No external stop signal is required -- the next enabled,
        credentialed chop tick's ``ensure_receiver_running`` call re-arms a
        fresh receiver, which is why self-checking each iteration suffices.
        """
        from sase_telegram.scripts import sase_tg_inbound as inbound

        mock_creds.get_bot_token.return_value = "token"
        mock_tg.get_updates.return_value = []
        # Enabled for the first two iterations, then disabled.
        mock_enabled.side_effect = [True, True, False]

        result = inbound._run_receiver()

        assert result == 0
        assert mock_enabled.call_count == 3
        assert mock_tg.get_updates.call_count == 2
        # The receiver must long-poll, not busy-loop with a short timeout.
        for call in mock_tg.get_updates.call_args_list:
            assert call.kwargs["timeout"] == inbound._RECEIVER_POLL_TIMEOUT_SECONDS

    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.is_telegram_enabled")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_receiver_self_terminates_on_credential_loss(
        self,
        mock_tg: MagicMock,
        mock_enabled: MagicMock,
        mock_creds: MagicMock,
    ) -> None:
        from sase_telegram.credentials import TelegramCredentialError
        from sase_telegram.scripts import sase_tg_inbound as inbound

        mock_enabled.return_value = True
        mock_creds.get_bot_token.side_effect = TelegramCredentialError("rotated")

        result = inbound._run_receiver()

        assert result == 0
        mock_tg.get_updates.assert_not_called()

    @patch("sase_telegram.scripts.sase_tg_inbound.time")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.is_telegram_enabled")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_receiver_backs_off_and_retries_after_a_poll_failure(
        self,
        mock_tg: MagicMock,
        mock_enabled: MagicMock,
        mock_creds: MagicMock,
        mock_time: MagicMock,
    ) -> None:
        """A rate-limit/network failure that outlasts get_updates' own retries

        does not crash the receiver -- it backs off and keeps polling
        instead of falling back to the old multi-second application floor.
        """
        from sase_telegram.scripts import sase_tg_inbound as inbound

        mock_creds.get_bot_token.return_value = "token"
        mock_enabled.side_effect = [True, True, False]
        mock_tg.get_updates.side_effect = [RuntimeError("rate limited"), []]

        result = inbound._run_receiver()

        assert result == 0
        mock_time.sleep.assert_called_once_with(inbound._RECEIVER_ERROR_BACKOFF_SECONDS)
        assert mock_tg.get_updates.call_count == 2


class _ExecReplaced(Exception):
    """Sentinel raised by a mocked ``os.execvp`` that would have replaced us."""

    def __init__(self, path: str, argv: list[str]) -> None:
        super().__init__(path)
        self.path = path
        self.argv = argv


def _raise_execvp(path: str, argv: list[str]) -> None:
    raise _ExecReplaced(path, list(argv))


class TestReceiverRuntimeRefresh:
    """Re-exec the persistent receiver when its loaded runtime goes stale."""

    def _stub_observe(
        self,
        monkeypatch: pytest.MonkeyPatch,
        values: list[RuntimeGeneration | Exception],
    ) -> None:
        queue = list(values)

        def observe() -> RuntimeGeneration:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(
            "sase_telegram.scripts.sase_tg_inbound.observe_runtime_generation",
            observe,
        )

    def _stub_refresh_settle(
        self, monkeypatch: pytest.MonkeyPatch, generation: RuntimeGeneration
    ) -> None:
        monkeypatch.setattr(
            "sase_telegram.scripts.sase_tg_inbound.wait_for_settled_generation",
            lambda **_kwargs: generation,
        )
        monkeypatch.setattr(
            "sase_telegram.scripts.sase_tg_inbound.canonical_receiver_argv",
            lambda: ["/venv/bin/sase_job_tg_inbound", "--receiver"],
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.os.execvp")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.is_telegram_enabled")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_unchanged_generation_keeps_polling(
        self,
        mock_tg: MagicMock,
        mock_enabled: MagicMock,
        mock_creds: MagicMock,
        mock_execvp: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        generation = _runtime_generation("same")
        monkeypatch.setattr(
            "sase_telegram.scripts.sase_tg_inbound.observe_runtime_generation",
            lambda: generation,
        )
        mock_creds.get_bot_token.return_value = "token"
        mock_enabled.side_effect = [True, True, False]
        mock_tg.get_updates.return_value = []

        result = inbound._run_receiver()

        assert result == 0
        assert mock_tg.get_updates.call_count == 2
        mock_execvp.assert_not_called()

    @patch(
        "sase_telegram.scripts.sase_tg_inbound.os.execvp",
        side_effect=_raise_execvp,
    )
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.is_telegram_enabled")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_refresh_before_polling_skips_get_updates(
        self,
        mock_tg: MagicMock,
        mock_enabled: MagicMock,
        mock_creds: MagicMock,
        _mock_execvp: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        baseline = _runtime_generation("old")
        current = _runtime_generation("new")
        self._stub_observe(monkeypatch, [baseline, current])
        self._stub_refresh_settle(monkeypatch, current)
        mock_creds.get_bot_token.return_value = "token"
        mock_enabled.return_value = True

        with pytest.raises(_ExecReplaced) as caught:
            inbound._run_receiver()

        mock_tg.get_updates.assert_not_called()
        assert caught.value.path == "/venv/bin/sase_job_tg_inbound"
        assert caught.value.argv == [
            "/venv/bin/sase_job_tg_inbound",
            "--receiver",
        ]
        assert not OFFSET_TEST_FILE.exists()

    @patch(
        "sase_telegram.scripts.sase_tg_inbound.os.execvp",
        side_effect=_raise_execvp,
    )
    @patch("sase_telegram.scripts.sase_tg_inbound._launch_agent")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.is_telegram_enabled")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_refresh_after_fetch_does_not_advance_offset(
        self,
        mock_tg: MagicMock,
        mock_enabled: MagicMock,
        mock_creds: MagicMock,
        mock_launch: MagicMock,
        _mock_execvp: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        baseline = _runtime_generation("old")
        current = _runtime_generation("new")
        self._stub_observe(monkeypatch, [baseline, baseline, current])
        self._stub_refresh_settle(monkeypatch, current)
        mock_creds.get_bot_token.return_value = "token"
        mock_enabled.return_value = True
        update = SimpleNamespace(
            update_id=50,
            callback_query=None,
            message=SimpleNamespace(
                text="do not lose me",
                photo=None,
                document=None,
                entities=None,
                message_id=7,
            ),
        )
        mock_tg.get_updates.return_value = [update]

        with pytest.raises(_ExecReplaced) as caught:
            inbound._run_receiver()

        mock_tg.get_updates.assert_called_once()
        mock_launch.assert_not_called()
        assert not OFFSET_TEST_FILE.exists()
        assert caught.value.argv == [
            "/venv/bin/sase_job_tg_inbound",
            "--receiver",
        ]

    @patch(
        "sase_telegram.scripts.sase_tg_inbound.os.execvp",
        side_effect=_raise_execvp,
    )
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.is_telegram_enabled")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_refresh_uses_canonical_receiver_argv(
        self,
        mock_tg: MagicMock,
        mock_enabled: MagicMock,
        mock_creds: MagicMock,
        _mock_execvp: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        current = _runtime_generation("new")
        self._stub_observe(monkeypatch, [_runtime_generation("old"), current])
        self._stub_refresh_settle(monkeypatch, current)
        monkeypatch.setattr(
            "sase_telegram.scripts.sase_tg_inbound.canonical_receiver_argv",
            lambda: ["/opt/sase/bin/sase_job_tg_inbound", "--receiver"],
        )
        mock_creds.get_bot_token.return_value = "token"
        mock_enabled.return_value = True

        with pytest.raises(_ExecReplaced) as caught:
            inbound._run_receiver()

        mock_tg.get_updates.assert_not_called()
        assert caught.value.path == "/opt/sase/bin/sase_job_tg_inbound"
        assert caught.value.argv == [
            "/opt/sase/bin/sase_job_tg_inbound",
            "--receiver",
        ]

    @patch(
        "sase_telegram.scripts.sase_tg_inbound.os.execvp",
        side_effect=OSError("exec format error"),
    )
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.is_telegram_enabled")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_exec_failure_exits_nonzero(
        self,
        mock_tg: MagicMock,
        mock_enabled: MagicMock,
        mock_creds: MagicMock,
        _mock_execvp: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        current = _runtime_generation("new")
        self._stub_observe(monkeypatch, [_runtime_generation("old"), current])
        self._stub_refresh_settle(monkeypatch, current)
        mock_creds.get_bot_token.return_value = "token"
        mock_enabled.return_value = True

        with caplog.at_level("ERROR"):
            result = inbound._run_receiver()

        assert result == 1
        mock_tg.get_updates.assert_not_called()
        assert "Failed to refresh Telegram receiver runtime: exec format error" in (
            caplog.text
        )
        assert "Traceback" not in caplog.text

    @patch("sase_telegram.scripts.sase_tg_inbound.os.execvp")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    @patch("sase_telegram.scripts.sase_tg_inbound.is_telegram_enabled")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_unsettled_start_refreshes_without_polling(
        self,
        mock_tg: MagicMock,
        mock_enabled: MagicMock,
        mock_creds: MagicMock,
        mock_execvp: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        settled = _runtime_generation("settled")
        self._stub_observe(monkeypatch, [RuntimeScanError("torn install at start")])
        self._stub_refresh_settle(monkeypatch, settled)
        mock_execvp.side_effect = _raise_execvp

        with pytest.raises(_ExecReplaced):
            inbound._run_receiver()

        mock_enabled.assert_not_called()
        mock_creds.get_bot_token.assert_not_called()
        mock_tg.get_updates.assert_not_called()
