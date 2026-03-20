"""Tests for inbound Telegram message handling logic."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sase_telegram.inbound import (
    build_photo_prompt,
    clear_awaiting_feedback,
    get_last_offset,
    load_awaiting_feedback,
    make_image_filename,
    process_callback,
    process_callback_twostep,
    process_text_message,
    reconstruct_code_markers,
    save_awaiting_feedback,
    save_offset,
)

OFFSET_TEST_PATH = Path("/tmp/test_update_offset.txt")
AWAITING_TEST_PATH = Path("/tmp/test_awaiting_feedback.json")


def _cleanup() -> None:
    OFFSET_TEST_PATH.unlink(missing_ok=True)
    AWAITING_TEST_PATH.unlink(missing_ok=True)


def _make_pending_plan(prefix: str, response_dir: str) -> dict:
    return {
        prefix: {
            "notification_id": prefix + "00000000-0000-0000-0000-000000000000",
            "action": "PlanApproval",
            "action_data": {"response_dir": response_dir},
            "message_id": 42,
            "chat_id": "12345",
        }
    }


def _make_pending_hitl(prefix: str, artifacts_dir: str) -> dict:
    return {
        prefix: {
            "notification_id": prefix + "00000000-0000-0000-0000-000000000000",
            "action": "HITL",
            "action_data": {"artifacts_dir": artifacts_dir},
            "message_id": 42,
            "chat_id": "12345",
        }
    }


def _make_pending_question(prefix: str, response_dir: str) -> dict:
    return {
        prefix: {
            "notification_id": prefix + "00000000-0000-0000-0000-000000000000",
            "action": "UserQuestion",
            "action_data": {"response_dir": response_dir},
            "message_id": 42,
            "chat_id": "12345",
        }
    }


class TestOffsetPersistence:
    def setup_method(self) -> None:
        _cleanup()
        self._patchers = [
            patch("sase_telegram.inbound.UPDATE_OFFSET_PATH", OFFSET_TEST_PATH),
        ]
        for p in self._patchers:
            p.start()

    def teardown_method(self) -> None:
        for p in self._patchers:
            p.stop()
        _cleanup()

    def test_no_file_returns_none(self) -> None:
        assert get_last_offset() is None

    def test_save_and_load_roundtrip(self) -> None:
        save_offset(12345)
        assert get_last_offset() == 12345

    def test_overwrite(self) -> None:
        save_offset(100)
        save_offset(200)
        assert get_last_offset() == 200


class TestProcessCallbackPlan:
    def test_approve(self, tmp_path: Path) -> None:
        response_dir = str(tmp_path)
        pending = _make_pending_plan("abcd1234", response_dir)
        result = process_callback("plan:abcd1234:approve", pending)
        assert result is not None
        assert result.action_type == "plan"
        assert result.response_data == {"action": "approve"}
        assert result.response_path == tmp_path / "plan_response.json"

    def test_reject(self, tmp_path: Path) -> None:
        response_dir = str(tmp_path)
        pending = _make_pending_plan("abcd1234", response_dir)
        result = process_callback("plan:abcd1234:reject", pending)
        assert result is not None
        assert result.response_data == {"action": "reject"}

    def test_unknown_pending(self) -> None:
        result = process_callback("plan:unknown1:approve", {})
        assert result is None


class TestProcessCallbackHITL:
    def test_accept(self, tmp_path: Path) -> None:
        pending = _make_pending_hitl("hitl0001", str(tmp_path))
        result = process_callback("hitl:hitl0001:accept", pending)
        assert result is not None
        assert result.response_data == {"action": "accept", "approved": True}
        assert result.response_path == tmp_path / "hitl_response.json"

    def test_reject(self, tmp_path: Path) -> None:
        pending = _make_pending_hitl("hitl0001", str(tmp_path))
        result = process_callback("hitl:hitl0001:reject", pending)
        assert result is not None
        assert result.response_data == {"action": "reject", "approved": False}

    def test_feedback_returns_none(self, tmp_path: Path) -> None:
        pending = _make_pending_hitl("hitl0001", str(tmp_path))
        result = process_callback("hitl:hitl0001:feedback", pending)
        assert result is None


class TestProcessCallbackQuestion:
    def test_option_selection(self, tmp_path: Path) -> None:
        response_dir = str(tmp_path)
        request = {
            "questions": [
                {
                    "question": "Which approach?",
                    "options": [
                        {"label": "Option A", "description": "First"},
                        {"label": "Option B", "description": "Second"},
                    ],
                }
            ]
        }
        (tmp_path / "question_request.json").write_text(json.dumps(request))

        pending = _make_pending_question("ques0001", response_dir)
        result = process_callback("question:ques0001:0", pending)
        assert result is not None
        assert result.action_type == "question"
        assert result.response_data["answers"][0]["selected"] == ["Option A"]
        assert result.response_data["answers"][0]["question"] == "Which approach?"
        assert result.response_data["answers"][0]["custom_feedback"] is None
        assert result.response_data["global_note"] == "Answered via Telegram"

    def test_custom_returns_none(self, tmp_path: Path) -> None:
        pending = _make_pending_question("ques0001", str(tmp_path))
        result = process_callback("question:ques0001:custom", pending)
        assert result is None


class TestProcessCallbackTwostep:
    def test_hitl_feedback(self, tmp_path: Path) -> None:
        pending = _make_pending_hitl("hitl0001", str(tmp_path))
        result = process_callback_twostep("hitl:hitl0001:feedback", pending)
        assert result is not None
        prefix, info = result
        assert prefix == "hitl0001"
        assert info["action_type"] == "hitl"
        assert info["artifacts_dir"] == str(tmp_path)

    def test_question_custom(self, tmp_path: Path) -> None:
        request = {
            "questions": [{"question": "What do you think?", "options": []}]
        }
        (tmp_path / "question_request.json").write_text(json.dumps(request))

        pending = _make_pending_question("ques0001", str(tmp_path))
        result = process_callback_twostep("question:ques0001:custom", pending)
        assert result is not None
        prefix, info = result
        assert prefix == "ques0001"
        assert info["action_type"] == "question"
        assert info["question_text"] == "What do you think?"

    def test_non_twostep_returns_none(self, tmp_path: Path) -> None:
        pending = _make_pending_plan("abcd1234", str(tmp_path))
        result = process_callback_twostep("plan:abcd1234:approve", pending)
        assert result is None

    def test_unknown_pending_returns_none(self) -> None:
        result = process_callback_twostep("hitl:unknown1:feedback", {})
        assert result is None


class TestProcessTextMessage:
    def setup_method(self) -> None:
        _cleanup()
        self._patcher = patch(
            "sase_telegram.inbound.AWAITING_FEEDBACK_PATH", AWAITING_TEST_PATH
        )
        self._patcher.start()

    def teardown_method(self) -> None:
        self._patcher.stop()
        _cleanup()

    def test_with_hitl_awaiting(self, tmp_path: Path) -> None:
        save_awaiting_feedback(
            "hitl0001",
            {"action_type": "hitl", "artifacts_dir": str(tmp_path)},
        )
        result = process_text_message("Please fix the typo on line 5")
        assert result is not None
        assert result.action_type == "hitl"
        assert result.notif_id_prefix == "hitl0001"
        assert result.response_data == {
            "action": "feedback",
            "approved": False,
            "feedback": "Please fix the typo on line 5",
        }
        assert result.response_path == tmp_path / "hitl_response.json"

    def test_with_question_awaiting(self, tmp_path: Path) -> None:
        save_awaiting_feedback(
            "ques0001",
            {
                "action_type": "question",
                "response_dir": str(tmp_path),
                "question_text": "Which approach?",
            },
        )
        result = process_text_message("Use the second approach")
        assert result is not None
        assert result.action_type == "question"
        assert result.response_data["answers"][0]["custom_feedback"] == (
            "Use the second approach"
        )
        assert result.response_data["answers"][0]["selected"] == []

    def test_without_awaiting(self) -> None:
        result = process_text_message("Random text")
        assert result is None


class TestHandleTextMessageAgentLaunch:
    """Tests for _handle_text_message agent launch behavior (script module)."""

    def setup_method(self) -> None:
        _cleanup()
        self._patcher = patch(
            "sase_telegram.inbound.AWAITING_FEEDBACK_PATH", AWAITING_TEST_PATH
        )
        self._patcher.start()

    def teardown_method(self) -> None:
        self._patcher.stop()
        _cleanup()

    def test_launches_agent_for_plain_text(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_text_message,
        )

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._launch_agent"
        ) as mock_launch:
            _handle_text_message("List all open beads")
            mock_launch.assert_called_once_with("List all open beads")

    def test_slash_command_ignored(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_text_message,
        )

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._launch_agent"
        ) as mock_launch:
            _handle_text_message("/start")
            mock_launch.assert_not_called()

    def test_feedback_flow_does_not_launch_agent(self, tmp_path: Path) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_text_message,
        )

        save_awaiting_feedback(
            "hitl0001",
            {"action_type": "hitl", "artifacts_dir": str(tmp_path)},
        )
        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._launch_agent"
            ) as mock_launch,
            patch(
                "sase_telegram.scripts.sase_tg_inbound._write_response"
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.pending_actions"
            ),
        ):
            _handle_text_message("Some feedback text")
            mock_launch.assert_not_called()


class TestLaunchAgent:
    """Tests for the _launch_agent helper (script module)."""

    @patch(
        "sase_telegram.scripts.sase_tg_inbound.telegram_client"
    )
    @patch(
        "sase_telegram.scripts.sase_tg_inbound.credentials"
    )
    def test_success_sends_confirmation(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"
        mock_result = MagicMock()
        mock_result.pid = 42
        mock_result.workspace_num = 3

        with patch(
            "sase.agent_launcher.launch_agent_from_cwd",
            return_value=mock_result,
        ):
            _launch_agent("List all open beads")

        mock_tg.send_message.assert_called_once()
        call_args = mock_tg.send_message.call_args
        assert call_args[0][0] == "12345"
        assert "Launched" in call_args[0][1]
        assert "List all open beads" in call_args[0][1]

    @patch(
        "sase_telegram.scripts.sase_tg_inbound.telegram_client"
    )
    @patch(
        "sase_telegram.scripts.sase_tg_inbound.credentials"
    )
    def test_failure_sends_error(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"

        with patch(
            "sase.agent_launcher.launch_agent_from_cwd",
            side_effect=RuntimeError("No workspace available"),
        ):
            _launch_agent("Do something")

        mock_tg.send_message.assert_called_once()
        call_args = mock_tg.send_message.call_args
        assert "Failed to launch agent" in call_args[0][1]
        assert "No workspace available" in call_args[0][1]

    @patch(
        "sase_telegram.scripts.sase_tg_inbound.telegram_client"
    )
    @patch(
        "sase_telegram.scripts.sase_tg_inbound.credentials"
    )
    def test_auto_name_prepended_when_no_name_directive(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"
        mock_result = MagicMock()
        mock_result.pid = 42
        mock_result.workspace_num = 3

        with (
            patch(
                "sase.agent_names.get_next_auto_name",
                return_value="c",
            ),
            patch(
                "sase.agent_launcher.launch_agent_from_cwd",
                return_value=mock_result,
            ) as mock_launch,
        ):
            _launch_agent("List all open beads")

        # The prompt passed to launch_agent_from_cwd should start with %n:c
        launched_prompt = mock_launch.call_args[0][0]
        assert launched_prompt.startswith("%n:c ")
        assert "List all open beads" in launched_prompt

    @patch(
        "sase_telegram.scripts.sase_tg_inbound.telegram_client"
    )
    @patch(
        "sase_telegram.scripts.sase_tg_inbound.credentials"
    )
    def test_no_auto_name_when_name_directive_present(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"
        mock_result = MagicMock()
        mock_result.pid = 42
        mock_result.workspace_num = 3

        with patch(
            "sase.agent_launcher.launch_agent_from_cwd",
            return_value=mock_result,
        ) as mock_launch:
            _launch_agent("%n:foo List all open beads")

        # The prompt should pass through unchanged (no auto-name prepended)
        launched_prompt = mock_launch.call_args[0][0]
        assert not launched_prompt.startswith("%n:foo %n:")
        assert "%n:foo" in launched_prompt

    @patch(
        "sase_telegram.scripts.sase_tg_inbound.telegram_client"
    )
    @patch(
        "sase_telegram.scripts.sase_tg_inbound.credentials"
    )
    def test_launch_includes_wait_keyboard(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"
        mock_result = MagicMock()
        mock_result.pid = 42
        mock_result.workspace_num = 3

        with (
            patch(
                "sase.agent_names.get_next_auto_name",
                return_value="c",
            ),
            patch(
                "sase.agent_launcher.launch_agent_from_cwd",
                return_value=mock_result,
            ),
        ):
            _launch_agent("List all open beads")

        call_kwargs = mock_tg.send_message.call_args
        keyboard = call_kwargs.kwargs.get("reply_markup")
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        assert len(buttons) == 1
        assert len(buttons[0]) == 2
        assert buttons[0][0].text == "📋 Resume"
        assert buttons[0][0].copy_text.text == "#resume:c %w:c "
        assert buttons[0][1].text == "📋 Wait"
        assert buttons[0][1].copy_text.text == "%w:c "

    @patch(
        "sase_telegram.scripts.sase_tg_inbound.telegram_client"
    )
    @patch(
        "sase_telegram.scripts.sase_tg_inbound.credentials"
    )
    def test_launch_wait_keyboard_includes_vcs_tag(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_single_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"
        mock_result = MagicMock()
        mock_result.pid = 42
        mock_result.workspace_num = 3

        with (
            patch(
                "sase.agent_launcher.launch_agent_from_cwd",
                return_value=mock_result,
            ),
            patch(
                "sase.xprompt.extract_vcs_workflow_tag",
                return_value="#gh:sase ",
            ),
        ):
            _launch_single_agent("%n:foo #gh:sase Fix a bug")

        call_kwargs = mock_tg.send_message.call_args
        keyboard = call_kwargs.kwargs.get("reply_markup")
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        assert buttons[0][0].text == "📋 Resume"
        assert buttons[0][0].copy_text.text == "#gh:sase #resume:foo %w:foo "
        assert buttons[0][1].text == "📋 Wait"
        assert buttons[0][1].copy_text.text == "#gh:sase %w:foo "


class TestAwaitingFeedbackState:
    def setup_method(self) -> None:
        _cleanup()
        self._patcher = patch(
            "sase_telegram.inbound.AWAITING_FEEDBACK_PATH", AWAITING_TEST_PATH
        )
        self._patcher.start()

    def teardown_method(self) -> None:
        self._patcher.stop()
        _cleanup()

    def test_save_load_cycle(self) -> None:
        assert load_awaiting_feedback() is None
        save_awaiting_feedback("abcd1234", {"action_type": "hitl", "dir": "/tmp"})
        loaded = load_awaiting_feedback()
        assert loaded is not None
        assert loaded["prefix"] == "abcd1234"
        assert loaded["action_info"]["action_type"] == "hitl"

    def test_clear(self) -> None:
        save_awaiting_feedback("abcd1234", {"action_type": "hitl"})
        assert load_awaiting_feedback() is not None
        clear_awaiting_feedback()
        assert load_awaiting_feedback() is None

    def test_clear_when_no_file(self) -> None:
        # Should not raise
        clear_awaiting_feedback()
        assert load_awaiting_feedback() is None


class TestBuildPhotoPrompt:
    def test_with_caption(self, tmp_path: Path) -> None:
        image_path = tmp_path / "photo.jpg"
        result = build_photo_prompt(image_path, "What is this?")
        assert "What is this?" in result
        assert str(image_path) in result
        assert "respond to the user's request" in result

    def test_without_caption(self, tmp_path: Path) -> None:
        image_path = tmp_path / "photo.jpg"
        result = build_photo_prompt(image_path, None)
        assert str(image_path) in result
        assert "describe what you see" in result

    def test_empty_string_caption(self, tmp_path: Path) -> None:
        image_path = tmp_path / "photo.jpg"
        result = build_photo_prompt(image_path, "")
        # Empty string is falsy, should behave like None
        assert "describe what you see" in result


class TestMakeImageFilename:
    def test_format(self) -> None:
        filename = make_image_filename("ABCDEFghijklmnop")
        assert filename.endswith(".jpg")
        # Should contain the first 12 chars of file_id
        assert "ABCDEFghijkl" in filename
        # Should match format: YYYYMMDD_HHMMSS_<prefix>.jpg
        parts = filename.rsplit(".", 1)[0].split("_")
        assert len(parts) == 3  # date, time, file_id_prefix

    def test_different_file_ids_produce_different_names(self) -> None:
        name1 = make_image_filename("AAAAAAAAAAAA")
        name2 = make_image_filename("BBBBBBBBBBBB")
        assert name1 != name2


class TestReconstructCodeMarkers:
    def test_no_entities_returns_unchanged(self) -> None:
        assert reconstruct_code_markers("hello world", []) == "hello world"

    def test_none_entities_returns_unchanged(self) -> None:
        assert reconstruct_code_markers("hello world", None) == "hello world"

    def test_single_code_entity(self) -> None:
        entity = SimpleNamespace(type="code", offset=0, length=4)
        assert reconstruct_code_markers("#foo", [entity]) == "`#foo`"

    def test_multiple_code_entities(self) -> None:
        # "hello #foo and #bar"
        entities = [
            SimpleNamespace(type="code", offset=6, length=4),
            SimpleNamespace(type="code", offset=15, length=4),
        ]
        result = reconstruct_code_markers("hello #foo and #bar", entities)
        assert result == "hello `#foo` and `#bar`"

    def test_pre_entity(self) -> None:
        entity = SimpleNamespace(type="pre", offset=0, length=11, language=None)
        result = reconstruct_code_markers("print('hi')", [entity])
        assert result == "```\nprint('hi')\n```"

    def test_pre_entity_with_language(self) -> None:
        entity = SimpleNamespace(type="pre", offset=0, length=11, language="python")
        result = reconstruct_code_markers("print('hi')", [entity])
        assert result == "```python\nprint('hi')\n```"

    def test_mixed_code_and_pre(self) -> None:
        # "run #cmd then:\nprint('hi')"
        text = "run #cmd then:\nprint('hi')"
        entities = [
            SimpleNamespace(type="code", offset=4, length=4),
            SimpleNamespace(type="pre", offset=15, length=11, language=None),
        ]
        result = reconstruct_code_markers(text, entities)
        assert result == "run `#cmd` then:\n```\nprint('hi')\n```"

    def test_non_code_entities_ignored(self) -> None:
        entities = [
            SimpleNamespace(type="bold", offset=0, length=5),
            SimpleNamespace(type="italic", offset=6, length=5),
        ]
        assert reconstruct_code_markers("hello world", entities) == "hello world"


class TestHandlePhotoMessage:
    """Tests for _handle_photo_message (script module)."""

    @patch(
        "sase_telegram.scripts.sase_tg_inbound.telegram_client"
    )
    @patch(
        "sase_telegram.scripts.sase_tg_inbound.credentials"
    )
    def test_downloads_highest_res_and_launches_agent(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        tmp_path: Path,
    ) -> None:
        from types import SimpleNamespace

        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_photo_message,
        )

        mock_creds.get_chat_id.return_value = "12345"
        # download_file should write a file to the destination
        mock_tg.download_file.return_value = tmp_path / "photo.jpg"

        photo_small = SimpleNamespace(file_id="small_id_12345678")
        photo_large = SimpleNamespace(file_id="large_id_12345678")
        message = SimpleNamespace(
            photo=[photo_small, photo_large],
            caption="Describe this",
            caption_entities=None,
        )

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound.IMAGES_DIR",
                tmp_path,
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._launch_agent"
            ) as mock_launch,
        ):
            _handle_photo_message(message)

        # Should use highest-res photo (last in list)
        mock_tg.download_file.assert_called_once()
        call_args = mock_tg.download_file.call_args
        assert call_args[0][0] == "large_id_12345678"

        # Should launch agent with photo prompt
        mock_launch.assert_called_once()
        prompt = mock_launch.call_args[0][0]
        assert "Describe this" in prompt

    @patch(
        "sase_telegram.scripts.sase_tg_inbound.telegram_client"
    )
    @patch(
        "sase_telegram.scripts.sase_tg_inbound.credentials"
    )
    def test_download_failure_sends_error(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        tmp_path: Path,
    ) -> None:
        from types import SimpleNamespace

        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_photo_message,
        )

        mock_creds.get_chat_id.return_value = "12345"
        mock_tg.download_file.side_effect = RuntimeError("Network error")

        photo = SimpleNamespace(file_id="fail_id_12345678")
        message = SimpleNamespace(photo=[photo], caption=None, caption_entities=None)

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound.IMAGES_DIR",
                tmp_path,
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._launch_agent"
            ) as mock_launch,
        ):
            _handle_photo_message(message)

        mock_launch.assert_not_called()
        mock_tg.send_message.assert_called_once()
        error_msg = mock_tg.send_message.call_args[0][1]
        assert "Failed to download photo" in error_msg
