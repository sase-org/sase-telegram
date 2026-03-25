"""Integration tests for outbound and inbound entry point scripts."""

from __future__ import annotations

import json
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


def _cleanup_files() -> None:
    for f in [
        LAST_SENT_TEST_FILE,
        PENDING_TEST_FILE,
        RATE_LIMIT_TEST_FILE,
        OFFSET_TEST_FILE,
        AWAITING_TEST_FILE,
    ]:
        f.unlink(missing_ok=True)


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
        patch("sase_telegram.pending_actions.PENDING_ACTIONS_PATH", PENDING_TEST_FILE),
        patch("sase_telegram.rate_limit.RATE_LIMIT_PATH", RATE_LIMIT_TEST_FILE),
        patch("sase_telegram.inbound.UPDATE_OFFSET_PATH", OFFSET_TEST_FILE),
        patch("sase_telegram.inbound.AWAITING_FEEDBACK_PATH", AWAITING_TEST_FILE),
    ]
    for p in patchers:
        p.start()
    yield
    for p in patchers:
        p.stop()
    _cleanup_files()


class TestOutboundIntegration:
    """Integration tests for the outbound main() entry point."""

    @patch("sase_telegram.scripts.sase_tg_outbound.is_idle", return_value=False)
    def test_exits_early_when_user_active(self, _mock_idle: MagicMock) -> None:
        """When user is active, no messages should be sent."""
        result = outbound_main(["--dry-run"])
        assert result == 0

    @patch("sase_telegram.outbound.load_notifications")
    @patch("sase_telegram.scripts.sase_tg_outbound.is_idle", return_value=True)
    def test_first_run_initializes_without_sending(
        self, _mock_idle: MagicMock, mock_load: MagicMock
    ) -> None:
        """First run creates high-water mark but doesn't send backlog."""
        result = outbound_main(["--dry-run"])
        assert result == 0
        assert LAST_SENT_TEST_FILE.exists()
        mock_load.assert_not_called()

    @patch("sase_telegram.scripts.sase_tg_outbound.send_message")
    @patch("sase_telegram.outbound.load_notifications")
    @patch("sase_telegram.scripts.sase_tg_outbound.is_idle", return_value=True)
    @patch("sase_telegram.scripts.sase_tg_outbound.get_chat_id")
    def test_sends_notification_when_inactive(
        self,
        mock_chat_id: MagicMock,
        _mock_idle: MagicMock,
        mock_load: MagicMock,
        mock_send: MagicMock,
    ) -> None:
        """Full flow: inactive user with unsent notification -> Telegram message sent."""
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
    @patch("sase_telegram.scripts.sase_tg_outbound.is_idle", return_value=True)
    @patch("sase_telegram.scripts.sase_tg_outbound.get_chat_id")
    def test_saves_pending_action_for_plan_approval(
        self,
        mock_chat_id: MagicMock,
        _mock_idle: MagicMock,
        mock_load: MagicMock,
        mock_send: MagicMock,
        tmp_path: Path,
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
            patch("sase_telegram.scripts.sase_tg_outbound.is_idle", return_value=True),
            patch("sase_telegram.outbound.load_notifications", return_value=[n]),
        ):
            result = outbound_main(["--dry-run"])

        assert result == 0
        captured = capsys.readouterr()
        assert "Notification" in captured.out
        assert n.id in captured.out


class TestInboundIntegration:
    """Integration tests for the inbound main() entry point."""

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_no_updates_exits_cleanly(self, mock_tg: MagicMock) -> None:
        """When there are no Telegram updates, exits with 0."""
        mock_tg.get_updates.return_value = []
        result = inbound_main(["--once"])
        assert result == 0

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_processes_plan_approve_callback(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        """Full flow: plan approve callback -> response file written."""
        response_dir = tmp_path / "responses"
        response_dir.mkdir()

        # Set up pending action
        from sase_telegram import pending_actions

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

        # Create a mock callback query update
        callback_query = SimpleNamespace(
            id="cb_1",
            data="plan:abcd1234:approve",
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

        result = inbound_main(["--once"])
        assert result == 0

        # Verify response file was written
        response_file = response_dir / "plan_response.json"
        assert response_file.exists()
        response_data = json.loads(response_file.read_text())
        assert response_data == {"action": "approve"}

        # Verify pending action was cleaned up
        assert pending_actions.get("abcd1234") is None

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
        self, mock_tg: MagicMock, _mock_launch: MagicMock
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
