"""Tests for inbound Telegram message handling logic."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sase.agent.launcher import AgentLaunchResult

from sase_telegram.inbound import (
    build_image_prompt,
    build_photo_prompt,
    clear_awaiting_feedback,
    clear_awaiting_feedback_by_prefix,
    find_externally_handled,
    find_shared_handled_transports,
    get_last_offset,
    load_all_awaiting_feedback,
    load_awaiting_feedback,
    make_image_filename,
    normalize_launch_xprompt_at_refs,
    process_text_message,
    reconstruct_code_markers,
    save_awaiting_feedback,
    save_offset,
)

OFFSET_TEST_PATH = Path("/tmp/test_update_offset.txt")
AWAITING_TEST_PATH = Path("/tmp/test_awaiting_feedback.json")


def _launch_result(
    *,
    pid: int = 42,
    workspace_num: int = 3,
    project_name: str = "proj",
    timestamp: str = "260706_232107",
    artifacts_dir: str = "",
    agent_name: str | None = None,
) -> AgentLaunchResult:
    return AgentLaunchResult(
        pid=pid,
        workspace_num=workspace_num,
        workspace_dir="/tmp/workspace",
        output_path="/tmp/out.txt",
        project_name=project_name,
        timestamp=timestamp,
        artifacts_dir=artifacts_dir,
        agent_name=agent_name,
    )


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


def _make_pending_launch(prefix: str, response_dir: str) -> dict:
    return {
        prefix: {
            "notification_id": prefix + "00000000-0000-0000-0000-000000000000",
            "action": "LaunchApproval",
            "action_data": {
                "response_dir": response_dir,
                "request_id": "req_1",
            },
            "message_id": 42,
            "chat_id": "12345",
        }
    }


def _make_pending_gate(
    prefix: str,
    bundle: Path,
    *,
    request_path: Path | None = None,
    response_path: Path | None = None,
) -> dict:
    action_data = {"bundle_path": str(bundle)}
    if request_path is not None:
        action_data["request_path"] = str(request_path)
    if response_path is not None:
        action_data["response_path"] = str(response_path)
    return {
        prefix: {
            "notification_id": prefix + "00000000-0000-0000-0000-000000000000",
            "action": "CustomGate",
            "action_data": action_data,
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

    def test_with_gate_awaiting(self, tmp_path: Path) -> None:
        save_awaiting_feedback(
            "42",
            "gate0001",
            {
                "action_type": "gate",
                "bundle_path": str(tmp_path),
                "selected_option_ids": ["feedback"],
                "input_data": {},
                "feedback_is_command_input": True,
            },
        )
        result = process_text_message("Please fix the typo on line 5")
        assert result is not None
        assert result.action_type == "gate"
        assert result.notif_id_prefix == "gate0001"
        assert result.selected_option_ids == ("feedback",)
        assert result.feedback == "Please fix the typo on line 5"
        assert result.input_data == {"feedback": "Please fix the typo on line 5"}
        assert result.response_path == tmp_path / "response.json"

    def test_with_question_awaiting(self, tmp_path: Path) -> None:
        save_awaiting_feedback(
            "42",
            "ques0001",
            {
                "action_type": "question",
                "response_dir": str(tmp_path),
                "question_text": "Which approach?",
            },
        )
        result = process_text_message("Use the second approach")
        assert result is None

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

        msg = SimpleNamespace(text="List all open beads", entities=None, message_id=100)
        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._record_project_context"
            ) as mock_record,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            _handle_text_message(msg)
            mock_record.assert_called_once_with("List all open beads", msg)
            mock_launch.assert_called_once_with("List all open beads")

    def test_normalizes_vcs_at_ref_before_launch(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_text_message,
        )

        msg = SimpleNamespace(
            text="%n:a #gh@sase Fix the bug",
            entities=None,
            message_id=100,
        )
        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._record_project_context"
            ) as mock_record,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            _handle_text_message(msg)
            mock_record.assert_called_once_with("%n:a #gh:sase Fix the bug", msg)
            mock_launch.assert_called_once_with("%n:a #gh:sase Fix the bug")

    def test_plain_text_launch_disabled_by_empty_env_value(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_text_message,
        )

        msg = SimpleNamespace(text="List all open beads", entities=None, message_id=100)
        with (
            patch.dict("os.environ", {"SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED": ""}),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._record_project_context"
            ) as mock_record,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            _handle_text_message(msg)
            mock_record.assert_not_called()
            mock_launch.assert_not_called()

    def test_slash_command_dispatches_when_launches_disabled(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_text_message,
        )

        msg = SimpleNamespace(text="/list", entities=None, message_id=101)
        with (
            patch.dict("os.environ", {"SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED": "1"}),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._handle_command"
            ) as mock_handle,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            _handle_text_message(msg)
            mock_handle.assert_called_once_with("/list", msg)
            mock_launch.assert_not_called()

    def test_stale_gate_awaiting_does_not_consume_slash_command(
        self, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_text_message,
        )

        save_awaiting_feedback(
            "42",
            "gate0001",
            {"action_type": "gate", "bundle_path": str(tmp_path)},
        )
        msg = SimpleNamespace(
            text="/list",
            entities=None,
            message_id=101,
            chat=SimpleNamespace(id="12345"),
        )
        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._handle_command"
            ) as mock_handle,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
            patch("sase_telegram.scripts.sase_tg_inbound.pending_actions") as mock_pa,
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as mock_tg,
        ):
            mock_pa.get.return_value = None

            _handle_text_message(msg)

        mock_handle.assert_called_once_with("/list", msg)
        mock_launch.assert_not_called()
        mock_tg.send_message.assert_not_called()
        assert load_awaiting_feedback() is None

    def test_stale_gate_awaiting_clears_before_plain_text_launch(
        self, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_text_message,
        )

        save_awaiting_feedback(
            "42",
            "gate0001",
            {"action_type": "gate", "bundle_path": str(tmp_path)},
        )
        msg = SimpleNamespace(text="List all open beads", entities=None, message_id=100)
        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._record_project_context"
            ) as mock_record,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
            patch("sase_telegram.scripts.sase_tg_inbound.pending_actions") as mock_pa,
        ):
            mock_pa.get.return_value = None

            _handle_text_message(msg)

        assert load_awaiting_feedback() is None
        mock_record.assert_called_once_with("List all open beads", msg)
        mock_launch.assert_called_once_with("List all open beads")

    def test_reply_to_stale_gate_awaiting_sends_friendly_message(
        self, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_text_message,
        )

        save_awaiting_feedback(
            "42",
            "gate0001",
            {"action_type": "gate", "bundle_path": str(tmp_path)},
        )
        msg = SimpleNamespace(
            text="Too many agents",
            entities=None,
            message_id=100,
            reply_to_message=SimpleNamespace(message_id=42),
            chat=SimpleNamespace(id="12345"),
        )
        with (
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
            patch("sase_telegram.scripts.sase_tg_inbound.pending_actions") as mock_pa,
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as mock_tg,
        ):
            mock_pa.get.return_value = None

            _handle_text_message(msg)

        assert load_awaiting_feedback() is None
        mock_launch.assert_not_called()
        mock_tg.send_message.assert_called_once_with(
            "12345",
            "This action has already been handled",
            reply_to_message_id=100,
        )

    def test_slash_command_ignored(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_text_message,
        )

        msg = SimpleNamespace(text="/start", entities=None, message_id=101)
        with patch(
            "sase_telegram.scripts.sase_tg_inbound._launch_agent"
        ) as mock_launch:
            _handle_text_message(msg)
            mock_launch.assert_not_called()


class TestHandleQuestionFlow:
    def setup_method(self) -> None:
        _cleanup()
        self._patcher = patch(
            "sase_telegram.inbound.AWAITING_FEEDBACK_PATH", AWAITING_TEST_PATH
        )
        self._patcher.start()

    def teardown_method(self) -> None:
        self._patcher.stop()
        _cleanup()

    @staticmethod
    def _callback(data: str, message_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=f"cb-{message_id}",
            data=data,
            message=SimpleNamespace(
                message_id=message_id,
                chat=SimpleNamespace(id="12345"),
            ),
        )

    @staticmethod
    def _text_message(text: str, message_id: int, reply_to: int) -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            entities=None,
            message_id=message_id,
            reply_to_message=SimpleNamespace(message_id=reply_to),
            chat=SimpleNamespace(id="12345"),
        )

    def test_multi_question_callbacks_write_one_final_response(
        self, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_callback

        request = {
            "session_id": "s1",
            "questions": [
                {"question": "First?", "options": [{"label": "A"}]},
                {"question": "Second?", "options": [{"label": "B"}, {"label": "C"}]},
            ],
        }
        (tmp_path / "question_request.json").write_text(json.dumps(request))
        pending1 = _make_pending_question("ques0001", str(tmp_path))
        pending2 = _make_pending_question("ques0001", str(tmp_path))
        pending2["ques0001"]["message_id"] = 43

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._shared_action_resolution",
                return_value=None,
            ),
            patch("sase_telegram.scripts.sase_tg_inbound.pending_actions") as mock_pa,
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as mock_tg,
        ):
            mock_tg.send_message.side_effect = [
                SimpleNamespace(message_id=43),
                SimpleNamespace(message_id=99),
            ]

            _handle_callback(self._callback("question:ques0001:0", 42), pending1)

            assert not (tmp_path / "question_response.json").exists()
            progress = json.loads((tmp_path / "question_progress.json").read_text())
            assert progress["current_index"] == 1
            assert progress["active_message_id"] == 43
            assert progress["answers"][0]["selected"] == ["A"]
            assert mock_pa.add.call_args.args[0] == "ques0001"
            assert mock_pa.add.call_args.args[1]["message_id"] == 43

            _handle_callback(self._callback("question:ques0001:1", 43), pending2)

            response = json.loads((tmp_path / "question_response.json").read_text())
            assert response["answers"] == [
                {"question": "First?", "selected": ["A"], "custom_feedback": None},
                {"question": "Second?", "selected": ["C"], "custom_feedback": None},
            ]
            assert response["global_note"] == "Answered via Telegram"
            assert not (tmp_path / "question_progress.json").exists()
            assert mock_tg.edit_message_text.call_count == 2
            assert mock_tg.send_message.call_count == 2
            mock_pa.remove.assert_called_with("ques0001")

    def test_neutral_question_callback_uses_shared_executor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sase.notification_gates import paths
        from sase.notifications import pending_actions as sase_pending_actions
        from sase.notifications import store
        from sase.notifications.store import load_notifications
        from sase.user_question_actions import create_user_question_gate
        from sase_telegram.scripts.sase_tg_inbound import _handle_callback

        monkeypatch.setattr(paths, "INTERACTION_REQUESTS_DIR", tmp_path / "requests")
        monkeypatch.setattr(store, "NOTIFICATIONS_DIR", str(tmp_path / "notifications"))
        monkeypatch.setattr(
            store,
            "NOTIFICATIONS_FILE",
            str(tmp_path / "notifications" / "notifications.jsonl"),
        )
        monkeypatch.setattr(
            sase_pending_actions,
            "PENDING_ACTIONS_PATH",
            tmp_path / "shared-pending.json",
        )
        monkeypatch.setattr(
            sase_pending_actions,
            "LEGACY_TELEGRAM_PENDING_ACTIONS_PATH",
            tmp_path / "legacy-pending.json",
        )
        store._LOAD_CACHE.clear()
        gate = create_user_question_gate(
            [
                {
                    "question": "Which path?",
                    "options": [{"label": "Fast"}, {"label": "Safe"}],
                }
            ],
            session_id="telegram-question",
        )
        notification = load_notifications()[0]
        pending = {
            "ques0001": {
                "notification_id": notification.id,
                "action": "UserQuestion",
                "action_data": dict(notification.action_data),
                "message_id": 42,
                "chat_id": "12345",
            }
        }

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._shared_action_resolution",
                return_value=None,
            ),
            patch("sase_telegram.scripts.sase_tg_inbound.pending_actions") as mock_pa,
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client"),
        ):
            _handle_callback(self._callback("question:ques0001:1", 42), pending)

        response = json.loads(gate.response_path.read_text(encoding="utf-8"))
        assert response["selected_option_ids"] == ["submit"]
        assert response["source"] == "telegram"
        assert response["option_results"][0]["result"]["answers"] == [
            {
                "question": "Which path?",
                "selected": ["Safe"],
                "custom_feedback": None,
            }
        ]
        mock_pa.remove.assert_called_with("ques0001")

    def test_custom_text_advances_to_next_question(self, tmp_path: Path) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_callback,
            _handle_text_message,
        )

        request = {
            "session_id": "s1",
            "questions": [
                {"question": "Any notes?", "options": []},
                {"question": "Proceed?", "options": [{"label": "Yes"}]},
            ],
        }
        (tmp_path / "question_request.json").write_text(json.dumps(request))
        pending = _make_pending_question("ques0001", str(tmp_path))

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._shared_action_resolution",
                return_value=None,
            ),
            patch("sase_telegram.scripts.sase_tg_inbound.pending_actions") as mock_pa,
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as mock_tg,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as launch,
        ):
            mock_pa.get.return_value = pending["ques0001"]
            mock_tg.send_message.return_value = SimpleNamespace(message_id=43)

            _handle_callback(self._callback("question:ques0001:custom", 42), pending)
            _handle_text_message(self._text_message("Use the new tool", 100, 42))

            progress = json.loads((tmp_path / "question_progress.json").read_text())
            assert progress["current_index"] == 1
            assert progress["answers"] == [
                {
                    "question": "Any notes?",
                    "selected": ["Other"],
                    "custom_feedback": "Use the new tool",
                }
            ]
            assert progress["active_message_id"] == 43
            assert not AWAITING_TEST_PATH.exists()
            launch.assert_not_called()


class TestHandleImageMessageLaunchDisabled:
    """Tests launch-disabled behavior for image message handlers."""

    def test_photo_returns_before_download_when_launches_disabled(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_photo_message

        msg = SimpleNamespace()
        with (
            patch.dict("os.environ", {"SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED": "1"}),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.telegram_client.download_file"
            ) as mock_download,
            patch(
                "sase_telegram.scripts.sase_tg_inbound._record_project_context"
            ) as mock_record,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            _handle_photo_message(msg)
            mock_download.assert_not_called()
            mock_record.assert_not_called()
            mock_launch.assert_not_called()

    def test_document_image_returns_before_download_when_launches_disabled(
        self,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_document_image

        msg = SimpleNamespace()
        with (
            patch.dict("os.environ", {"SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED": "1"}),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.telegram_client.download_file"
            ) as mock_download,
            patch(
                "sase_telegram.scripts.sase_tg_inbound._record_project_context"
            ) as mock_record,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            _handle_document_image(msg)
            mock_download.assert_not_called()
            mock_record.assert_not_called()
            mock_launch.assert_not_called()


class TestMediaGroupImages:
    """Tests for Telegram album staging and launch flushing."""

    def _photo_message(
        self,
        file_id: str,
        *,
        message_id: int,
        caption: str | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            photo=[SimpleNamespace(file_id=file_id)],
            document=None,
            caption=caption,
            caption_entities=None,
            media_group_id="album-1",
            message_id=message_id,
            chat=SimpleNamespace(id=12345),
        )

    def _document_message(
        self,
        file_id: str,
        *,
        file_name: str | None,
        message_id: int,
        caption: str | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            photo=None,
            document=SimpleNamespace(
                file_id=file_id,
                file_name=file_name,
                mime_type="image/png",
            ),
            caption=caption,
            caption_entities=None,
            media_group_id="album-1",
            message_id=message_id,
            chat=SimpleNamespace(id=12345),
        )

    def test_grouped_photo_stages_without_immediate_launch(
        self, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        state_path = tmp_path / "media_groups.json"
        message = self._photo_message(
            "photo_one_12345678",
            message_id=10,
            caption="#gh@sase Compare these",
        )

        with (
            patch.object(inbound, "_MEDIA_GROUPS_PATH", state_path),
            patch.object(inbound.time, "time", return_value=100.0),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.telegram_client.download_file"
            ) as mock_download,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            assert inbound._stage_media_group_image(message, "photo") is True

        mock_download.assert_not_called()
        mock_launch.assert_not_called()
        state = json.loads(state_path.read_text())
        group = state["12345:album-1"]
        assert group["caption"] == "#gh:sase Compare these"
        assert group["items"] == [
            {
                "message_id": 10,
                "kind": "photo",
                "file_id": "photo_one_12345678",
                "file_name": None,
            }
        ]

    def test_flush_ready_group_downloads_all_images_and_launches_once(
        self, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        state_path = tmp_path / "media_groups.json"
        images_dir = tmp_path / "images"
        first = self._photo_message("photo_one_12345678", message_id=10)
        second = self._photo_message(
            "photo_two_12345678",
            message_id=11,
            caption="Compare these",
        )

        with (
            patch.object(inbound, "_MEDIA_GROUPS_PATH", state_path),
            patch.object(inbound.time, "time", side_effect=[100.0, 100.1]),
        ):
            inbound._stage_media_group_image(first, "photo")
            inbound._stage_media_group_image(second, "photo")

        def _download(_file_id: str, dest: Path) -> None:
            dest.write_text("image")

        with (
            patch.object(inbound, "_MEDIA_GROUPS_PATH", state_path),
            patch.object(inbound, "IMAGES_DIR", images_dir),
            patch.object(inbound.time, "time", return_value=103.0),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.telegram_client.download_file",
                side_effect=_download,
            ) as mock_download,
            patch(
                "sase_telegram.scripts.sase_tg_inbound._record_project_context"
            ) as mock_record,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            assert inbound._flush_ready_media_groups() == 1

        assert mock_download.call_count == 2
        mock_launch.assert_called_once()
        prompt = mock_launch.call_args.args[0]
        assert "Compare these" in prompt
        assert "1. " in prompt and "photo_one_12" in prompt
        assert "2. " in prompt and "photo_two_12" in prompt
        mock_record.assert_called_once()
        assert not state_path.exists()

    def test_grouped_document_filename_is_preserved_safely(
        self, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        state_path = tmp_path / "media_groups.json"
        images_dir = tmp_path / "images"
        message = self._document_message(
            "doc_file_12345678",
            file_name="../diagram.png",
            message_id=10,
            caption="Inspect this",
        )

        with (
            patch.object(inbound, "_MEDIA_GROUPS_PATH", state_path),
            patch.object(inbound.time, "time", return_value=100.0),
        ):
            inbound._stage_media_group_image(message, "document")

        def _download(_file_id: str, dest: Path) -> None:
            dest.write_text("image")

        with (
            patch.object(inbound, "_MEDIA_GROUPS_PATH", state_path),
            patch.object(inbound, "IMAGES_DIR", images_dir),
            patch.object(inbound.time, "time", return_value=103.0),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.telegram_client.download_file",
                side_effect=_download,
            ),
            patch("sase_telegram.scripts.sase_tg_inbound._record_project_context"),
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            inbound._flush_ready_media_groups()

        prompt = mock_launch.call_args.args[0]
        assert "diagram.png" in prompt
        assert "../diagram.png" not in prompt

    def test_launch_disabled_does_not_stage_grouped_images(
        self, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        state_path = tmp_path / "media_groups.json"
        message = self._photo_message("photo_one_12345678", message_id=10)

        with (
            patch.dict("os.environ", {"SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED": "1"}),
            patch.object(inbound, "_MEDIA_GROUPS_PATH", state_path),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.telegram_client.download_file"
            ) as mock_download,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            assert inbound._stage_media_group_image(message, "photo") is False
            assert inbound._flush_ready_media_groups() == 0

        assert not state_path.exists()
        mock_download.assert_not_called()
        mock_launch.assert_not_called()

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_download_failure_sends_one_error_and_does_not_launch(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        state_path = tmp_path / "media_groups.json"
        images_dir = tmp_path / "images"
        created: list[Path] = []

        first = self._photo_message("ok_photo_12345678", message_id=10)
        second = self._photo_message("bad_photo_12345678", message_id=11)

        with (
            patch.object(inbound, "_MEDIA_GROUPS_PATH", state_path),
            patch.object(inbound.time, "time", side_effect=[100.0, 100.1]),
        ):
            inbound._stage_media_group_image(first, "photo")
            inbound._stage_media_group_image(second, "photo")

        def _download(file_id: str, dest: Path) -> None:
            if file_id.startswith("bad"):
                raise RuntimeError("Network error")
            dest.write_text("image")
            created.append(dest)

        mock_tg.download_file.side_effect = _download
        with (
            patch.object(inbound, "_MEDIA_GROUPS_PATH", state_path),
            patch.object(inbound, "IMAGES_DIR", images_dir),
            patch.object(inbound.time, "time", return_value=103.0),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._record_project_context"
            ) as mock_record,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            assert inbound._flush_ready_media_groups() == 1

        mock_tg.send_message.assert_called_once()
        assert (
            "Failed to download image album" in mock_tg.send_message.call_args.args[1]
        )
        mock_launch.assert_not_called()
        mock_record.assert_not_called()
        assert not state_path.exists()
        assert all(not path.exists() for path in created)


class TestChangesCommandDispatch:
    """Tests for the /changes slash command."""

    def test_handle_command_dispatches_changes(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_command

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._handle_changes_command"
        ) as mock_handler:
            _handle_command("/changes project")

        mock_handler.assert_called_once_with("project")

    def test_changes_registered_as_slash_command(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _SLASH_COMMANDS

        assert ("changes", "Copy ChangeSpec workflow tags") in _SLASH_COMMANDS


class TestForkCommandDispatch:
    """Tests for the /fork slash command."""

    def test_handle_command_dispatches_fork(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_command

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._handle_fork_command"
        ) as mock_handler:
            _handle_command("/fork")

        mock_handler.assert_called_once_with()

    def test_legacy_command_is_not_dispatched(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_command

        legacy = "re" + "sume"
        with patch(
            "sase_telegram.scripts.sase_tg_inbound._handle_fork_command"
        ) as mock_handler:
            _handle_command(f"/{legacy}")

        mock_handler.assert_not_called()

    def test_fork_registered_as_slash_command(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _SLASH_COMMANDS

        assert ("fork", "Copy fork text for an agent") in _SLASH_COMMANDS
        assert not any(command == "re" + "sume" for command, _desc in _SLASH_COMMANDS)


class TestBeadCommandDispatch:
    """Tests for /bead command dispatch aliases."""

    def test_handle_command_dispatches_plural_beads_alias(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_command

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._handle_bead_command"
        ) as mock_handler:
            _handle_command("/beads")

        mock_handler.assert_called_once_with("", message=None)


class TestUpdateCommand:
    """Tests for the /update slash command."""

    def test_handle_command_dispatches_update(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_command

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._handle_update_command"
        ) as mock_handler:
            _handle_command("/update")

        mock_handler.assert_called_once_with()

    def test_install_is_not_handled(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_command

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._handle_update_command"
        ) as mock_handler:
            _handle_command("/install")

        mock_handler.assert_not_called()

    def test_update_registered_as_slash_command(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _SLASH_COMMANDS

        assert ("update", "Update SASE and restart axe") in _SLASH_COMMANDS
        assert not any(command == "install" for command, _desc in _SLASH_COMMANDS)

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_update_acknowledges_already_running(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_update_command

        mock_creds.get_chat_id.return_value = "12345"
        result = SimpleNamespace(status="already_running", message="busy")
        with patch(
            "sase_telegram.scripts.sase_tg_inbound.start_chat_install_worker",
            return_value=result,
        ):
            _handle_update_command()

        mock_tg.send_message.assert_called_once_with("12345", "Update already running.")

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_update_acknowledges_launched_with_log(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_update_command

        mock_creds.get_chat_id.return_value = "12345"
        result = SimpleNamespace(
            status="launched", message="Update worker started; log: /tmp/log"
        )
        with patch(
            "sase_telegram.scripts.sase_tg_inbound.start_chat_install_worker",
            return_value=result,
        ):
            _handle_update_command()

        mock_tg.send_message.assert_called_once_with(
            "12345",
            "Update worker started; log: /tmp/log",
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_update_launched_persists_completion_delivery_context(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        tmp_path: Path,
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        mock_creds.get_chat_id.return_value = "12345"
        result = SimpleNamespace(
            status="launched",
            message="Update worker started; log: /tmp/log",
            job_id="job-1",
            status_path=tmp_path / "core" / "job-1.json",
            log_path=tmp_path / "log.txt",
        )
        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound.start_chat_install_worker",
                return_value=result,
            ),
            patch.object(inbound, "_UPDATE_COMPLETION_PENDING_DIR", tmp_path / "tg"),
        ):
            inbound._handle_update_command()

        pending_path = tmp_path / "tg" / "job-1.json"
        payload = json.loads(pending_path.read_text())
        assert payload["job_id"] == "job-1"
        assert payload["chat_id"] == "12345"
        assert payload["status_path"] == str(tmp_path / "core" / "job-1.json")
        assert payload["log_path"] == str(tmp_path / "log.txt")
        mock_tg.send_message.assert_called_once_with(
            "12345",
            "Update worker started; log: /tmp/log",
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_update_completion_scan_sends_success_once(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        status_path = tmp_path / "completion.json"
        log_path = tmp_path / "worker.log"
        (pending_dir / "job-1.json").write_text(
            json.dumps(
                {
                    "job_id": "job-1",
                    "chat_id": "12345",
                    "status_path": str(status_path),
                    "log_path": str(log_path),
                }
            )
        )
        status_path.write_text(
            json.dumps(
                {
                    "job_id": "job-1",
                    "status": "success",
                    "exit_code": 0,
                    "log_path": str(log_path),
                    "message": "Already up to date.",
                }
            )
        )

        with patch.object(inbound, "_UPDATE_COMPLETION_PENDING_DIR", pending_dir):
            inbound._send_ready_update_completions()
            inbound._send_ready_update_completions()

        mock_tg.send_message.assert_called_once_with(
            "12345",
            f"Already up to date; log: {log_path}",
        )
        assert not (pending_dir / "job-1.json").exists()

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_update_completion_scan_prefers_failure_message(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        status_path = tmp_path / "completion.json"
        log_path = tmp_path / "worker.log"
        (pending_dir / "job-2.json").write_text(
            json.dumps(
                {
                    "job_id": "job-2",
                    "chat_id": "12345",
                    "status_path": str(status_path),
                    "log_path": str(log_path),
                }
            )
        )
        status_path.write_text(
            json.dumps(
                {
                    "job_id": "job-2",
                    "status": "failed",
                    "exit_code": 17,
                    "log_path": str(log_path),
                    "message": "Update failed: could not detect uv-tool install.",
                }
            )
        )

        with patch.object(inbound, "_UPDATE_COMPLETION_PENDING_DIR", pending_dir):
            inbound._send_ready_update_completions()

        mock_tg.send_message.assert_called_once_with(
            "12345",
            f"Update failed: could not detect uv-tool install; log: {log_path}",
        )
        assert not (pending_dir / "job-2.json").exists()

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_update_completion_scan_falls_back_to_exit_code(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        status_path = tmp_path / "completion.json"
        log_path = tmp_path / "worker.log"
        (pending_dir / "job-2.json").write_text(
            json.dumps(
                {
                    "job_id": "job-2",
                    "chat_id": "12345",
                    "status_path": str(status_path),
                    "log_path": str(log_path),
                }
            )
        )
        status_path.write_text(
            json.dumps(
                {
                    "job_id": "job-2",
                    "status": "failed",
                    "exit_code": 17,
                    "log_path": str(log_path),
                }
            )
        )

        with patch.object(inbound, "_UPDATE_COMPLETION_PENDING_DIR", pending_dir):
            inbound._send_ready_update_completions()

        mock_tg.send_message.assert_called_once_with(
            "12345",
            f"Update failed with exit code 17; log: {log_path}",
        )
        assert not (pending_dir / "job-2.json").exists()

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_update_completion_scan_waits_for_missing_completion(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        pending_path = pending_dir / "job-3.json"
        pending_path.write_text(
            json.dumps(
                {
                    "job_id": "job-3",
                    "chat_id": "12345",
                    "status_path": str(tmp_path / "missing.json"),
                    "log_path": "/tmp/log",
                }
            )
        )

        with patch.object(inbound, "_UPDATE_COMPLETION_PENDING_DIR", pending_dir):
            inbound._send_ready_update_completions()

        mock_tg.send_message.assert_not_called()
        assert pending_path.exists()

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_update_completion_scan_keeps_pending_on_send_failure(
        self, mock_tg: MagicMock, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        mock_tg.send_message.side_effect = RuntimeError("telegram down")
        pending_dir = tmp_path / "pending"
        pending_dir.mkdir()
        pending_path = pending_dir / "job-4.json"
        status_path = tmp_path / "completion.json"
        pending_path.write_text(
            json.dumps(
                {
                    "job_id": "job-4",
                    "chat_id": "12345",
                    "status_path": str(status_path),
                    "log_path": "/tmp/log",
                }
            )
        )
        status_path.write_text(
            json.dumps({"status": "success", "exit_code": 0, "log_path": "/tmp/log"})
        )

        with patch.object(inbound, "_UPDATE_COMPLETION_PENDING_DIR", pending_dir):
            inbound._send_ready_update_completions()

        mock_tg.send_message.assert_called_once_with(
            "12345",
            "Update completed successfully; log: /tmp/log",
        )
        assert pending_path.exists()

    def test_command_fingerprint_change_forces_registration(
        self, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        cache_path = tmp_path / "commands_registered_ts"
        cache_path.write_text(
            json.dumps(
                {"version": 1, "timestamp": 1000.0, "fingerprint": "old-fingerprint"}
            )
        )

        with (
            patch.object(inbound, "_COMMANDS_REGISTERED_PATH", cache_path),
            patch.object(inbound.time, "time", return_value=1001.0),
            patch.object(
                inbound.telegram_client, "set_my_commands", return_value=True
            ) as set_commands,
        ):
            inbound._register_commands_if_needed()

        set_commands.assert_called_once_with(inbound._SLASH_COMMANDS)
        payload = json.loads(cache_path.read_text())
        assert payload["version"] == 1
        assert payload["timestamp"] == 1001.0
        assert payload["fingerprint"] == inbound._slash_commands_fingerprint()

    def test_legacy_timestamp_cache_forces_registration(self, tmp_path: Path) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        cache_path = tmp_path / "commands_registered_ts"
        cache_path.write_text("1000.0")

        with (
            patch.object(inbound, "_COMMANDS_REGISTERED_PATH", cache_path),
            patch.object(inbound.time, "time", return_value=1001.0),
            patch.object(
                inbound.telegram_client, "set_my_commands", return_value=True
            ) as set_commands,
        ):
            inbound._register_commands_if_needed()

        set_commands.assert_called_once_with(inbound._SLASH_COMMANDS)

    @pytest.mark.parametrize("failure", ["false", "exception"])
    @pytest.mark.parametrize("existing_cache", [False, True])
    def test_failed_command_registration_does_not_write_cache(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        failure: str,
        existing_cache: bool,
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        cache_path = tmp_path / "commands_registered_ts"
        original_cache = json.dumps(
            {"version": 1, "timestamp": 1000.0, "fingerprint": "stale"},
            sort_keys=True,
        )
        if existing_cache:
            cache_path.write_text(original_cache)

        set_commands = MagicMock(return_value=False)
        if failure == "exception":
            set_commands.side_effect = RuntimeError("telegram down")
        with (
            patch.object(inbound, "_COMMANDS_REGISTERED_PATH", cache_path),
            patch.object(inbound.time, "time", return_value=1001.0),
            patch.object(
                inbound.telegram_client,
                "set_my_commands",
                set_commands,
            ),
        ):
            inbound._register_commands_if_needed()

        set_commands.assert_called_once_with(inbound._SLASH_COMMANDS)
        if existing_cache:
            assert cache_path.read_text() == original_cache
        else:
            assert not cache_path.exists()
        assert "will retry later" in caplog.text


class TestChangesCommand:
    """Tests for the /changes slash command."""

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_changes_rejects_multiple_args(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_changes_command

        mock_creds.get_chat_id.return_value = "12345"

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._list_changespec_xprompt_tags"
        ) as mock_list:
            _handle_changes_command("one two")

        mock_list.assert_not_called()
        mock_tg.send_message.assert_called_once_with(
            "12345", "Usage: /changes [project]"
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_changes_empty_without_project(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_changes_command

        mock_creds.get_chat_id.return_value = "12345"
        listing = SimpleNamespace(entries=[], skipped=[])

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._list_changespec_xprompt_tags",
            return_value=listing,
        ) as mock_list:
            _handle_changes_command("")

        mock_list.assert_called_once_with(None)
        mock_tg.send_message.assert_called_once_with("12345", "No active ChangeSpecs.")

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_changes_project_filter_empty(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_changes_command

        mock_creds.get_chat_id.return_value = "12345"
        listing = SimpleNamespace(entries=[], skipped=[])

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._list_changespec_xprompt_tags",
            return_value=listing,
        ) as mock_list:
            _handle_changes_command("sase")

        mock_list.assert_called_once_with("sase")
        mock_tg.send_message.assert_called_once_with(
            "12345", "No active ChangeSpecs for sase."
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_changes_entries_use_copy_text_buttons(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_changes_command

        mock_creds.get_chat_id.return_value = "12345"
        listing = SimpleNamespace(
            entries=[
                SimpleNamespace(project="sase", name="foo", tag="#hg:foo"),
                SimpleNamespace(project="sase-telegram", name="bar", tag="#git:bar"),
            ],
            skipped=["broken/missing: could not detect workflow type"],
        )

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._list_changespec_xprompt_tags",
            return_value=listing,
        ):
            _handle_changes_command("")

        call_args = mock_tg.send_message.call_args
        assert call_args.args[:2] == (
            "12345",
            "Active ChangeSpecs (2)\n"
            "Skipped 1 active ChangeSpec with unavailable workflow metadata.",
        )
        keyboard = call_args.kwargs["reply_markup"]
        buttons = keyboard.inline_keyboard
        assert buttons[0][0].text == "sase/foo"
        assert buttons[0][0].copy_text.text == "#hg:foo"
        assert buttons[1][0].text == "sase-telegram/bar"
        assert buttons[1][0].copy_text.text == "#git:bar"

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_changes_humanizes_unfiltered_button_labels_and_copy_vcs_refs(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_changes_command

        mock_creds.get_chat_id.return_value = "12345"
        listing = SimpleNamespace(
            entries=[
                SimpleNamespace(
                    project="sase",
                    name="sase_foo",
                    tag="#hg:gh_sase-org__sase_foo",
                )
            ],
            skipped=[],
        )

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._list_changespec_xprompt_tags",
                return_value=listing,
            ) as mock_list,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_project_name",
                side_effect=lambda project: (
                    "SASE Core" if project == "sase" else project
                ),
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_cl_name",
                side_effect=lambda name: (
                    "SASE Core_foo" if name == "sase_foo" else name
                ),
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_vcs_refs_in_text",
                side_effect=lambda text: text.replace("gh_sase-org__sase", "sase"),
            ),
        ):
            _handle_changes_command("")

        mock_list.assert_called_once_with(None)
        keyboard = mock_tg.send_message.call_args.kwargs["reply_markup"]
        button = keyboard.inline_keyboard[0][0]
        assert button.text == "SASE Core/SASE Core_foo"
        assert button.copy_text.text == "#hg:sase_foo"

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_changes_project_filter_uses_short_labels(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_changes_command

        mock_creds.get_chat_id.return_value = "12345"
        listing = SimpleNamespace(
            entries=[SimpleNamespace(project="sase", name="foo", tag="#hg:foo")],
            skipped=[],
        )

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._list_changespec_xprompt_tags",
            return_value=listing,
        ):
            _handle_changes_command("sase")

        assert mock_tg.send_message.call_args.args[:2] == (
            "12345",
            "Active ChangeSpecs for sase (1)",
        )
        keyboard = mock_tg.send_message.call_args.kwargs["reply_markup"]
        buttons = keyboard.inline_keyboard
        assert buttons[0][0].text == "foo"
        assert buttons[0][0].copy_text.text == "#hg:foo"

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_changes_project_filter_humanizes_header_but_uses_raw_filter(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_changes_command

        mock_creds.get_chat_id.return_value = "12345"
        listing = SimpleNamespace(
            entries=[SimpleNamespace(project="sase", name="sase_foo", tag="#hg:foo")],
            skipped=[],
        )

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._list_changespec_xprompt_tags",
                return_value=listing,
            ) as mock_list,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_project_name",
                return_value="SASE Core",
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_cl_name",
                return_value="SASE Core_foo",
            ),
        ):
            _handle_changes_command("sase")

        mock_list.assert_called_once_with("sase")
        assert mock_tg.send_message.call_args.args[:2] == (
            "12345",
            "Active ChangeSpecs for SASE Core (1)",
        )
        keyboard = mock_tg.send_message.call_args.kwargs["reply_markup"]
        button = keyboard.inline_keyboard[0][0]
        assert button.text == "SASE Core_foo"
        assert button.copy_text.text == "#hg:foo"

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_changes_chunks_large_result_sets(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_changes_command

        mock_creds.get_chat_id.return_value = "12345"
        listing = SimpleNamespace(
            entries=[
                SimpleNamespace(project="sase", name=f"c{i}", tag=f"#gh:c{i}")
                for i in range(51)
            ],
            skipped=[],
        )

        with patch(
            "sase_telegram.scripts.sase_tg_inbound._list_changespec_xprompt_tags",
            return_value=listing,
        ):
            _handle_changes_command("")

        assert mock_tg.send_message.call_count == 2
        first = mock_tg.send_message.call_args_list[0]
        second = mock_tg.send_message.call_args_list[1]
        assert first.args[:2] == (
            "12345",
            "Active ChangeSpecs (51)\nShowing 1-50 of 51",
        )
        assert second.args[:2] == (
            "12345",
            "Active ChangeSpecs (51)\nShowing 51-51 of 51",
        )
        assert len(first.kwargs["reply_markup"].inline_keyboard) == 50
        assert len(second.kwargs["reply_markup"].inline_keyboard) == 1


class TestLaunchAgent:
    """Tests for the _launch_agent helper (script module)."""

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_success_sends_confirmation(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
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
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[mock_result],
            ),
            patch("sase.agent.names.allocate_retry_name", return_value="c.r1"),
        ):
            _launch_agent("List all open beads")

        mock_tg.send_message.assert_called_once()
        call_args = mock_tg.send_message.call_args
        assert call_args[0][0] == "12345"
        assert "Launched" in call_args[0][1]
        assert "List all open beads" in call_args[0][1]

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_success_sends_confirmation_without_default_llm_provider(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _launch_agent

        mock_creds.get_chat_id.return_value = "12345"
        mock_result = SimpleNamespace(pid=42, workspace_num=3)

        with (
            patch(
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[mock_result],
            ),
            patch(
                "sase.llm_provider.registry.get_default_provider_name",
                side_effect=RuntimeError(
                    "No LLM provider is available. Install a provider plugin "
                    "or set llm_provider.provider explicitly."
                ),
            ),
            patch(
                "sase.llm_provider.registry.get_provider",
                side_effect=AssertionError("default provider fallback should stop"),
            ),
        ):
            _launch_agent("List all open beads")

        mock_tg.send_message.assert_called_once()
        call_args = mock_tg.send_message.call_args
        assert call_args[0][0] == "12345"
        assert "Agent Launched" in call_args[0][1]
        assert "List all open beads" in call_args[0][1]

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_success_uses_explicit_model_label_without_default_llm_provider(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _launch_agent

        mock_creds.get_chat_id.return_value = "12345"
        mock_result = SimpleNamespace(pid=42, workspace_num=3)

        with (
            patch(
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[mock_result],
            ),
            patch(
                "sase.llm_provider.registry.resolve_model_provider",
                return_value=(None, "opus"),
            ) as mock_resolve,
            patch(
                "sase.llm_provider.registry.get_default_provider_name",
                side_effect=RuntimeError(
                    "No LLM provider is available. Install a provider plugin "
                    "or set llm_provider.provider explicitly."
                ),
            ),
        ):
            _launch_agent("%model:opus List all open beads")

        mock_resolve.assert_called_once_with("opus")
        mock_tg.send_message.assert_called_once()
        call_args = mock_tg.send_message.call_args
        assert call_args[0][0] == "12345"
        assert "opus Launched" in call_args[0][1]
        assert "List all open beads" in call_args[0][1]

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_failure_sends_error(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"

        with patch(
            "sase.agent.launcher.launch_agents_from_cwd",
            side_effect=RuntimeError("No workspace available"),
        ):
            _launch_agent("Do something")

        mock_tg.send_message.assert_called_once()
        call_args = mock_tg.send_message.call_args
        assert "Failed to launch agent" in call_args[0][1]
        assert "No workspace available" in call_args[0][1]

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_no_auto_name_prepended_when_no_name_directive(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
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
                "sase.agent.names.get_next_auto_name",
                side_effect=AssertionError("Telegram must not allocate names"),
            ) as mock_auto,
            patch(
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[mock_result],
            ) as mock_launch,
        ):
            _launch_agent("List all open beads")

        launched_prompt = mock_launch.call_args[0][0]
        assert launched_prompt == "List all open beads"
        mock_auto.assert_not_called()

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_no_auto_name_when_name_directive_present(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"
        mock_result = MagicMock()
        mock_result.pid = 42
        mock_result.workspace_num = 3

        with patch(
            "sase.agent.launcher.launch_agents_from_cwd",
            return_value=[mock_result],
        ) as mock_launch:
            _launch_agent("%n:foo List all open beads")

        # The prompt should pass through unchanged (no auto-name prepended)
        launched_prompt = mock_launch.call_args[0][0]
        assert not launched_prompt.startswith("%n:foo %n:")
        assert "%n:foo" in launched_prompt

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_no_auto_name_for_repeat_prompt(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
    ) -> None:
        # Repeat prompts must flow through to spawn_repeat_batch without
        # a %n:<auto> prepend — prepending turns the auto-name into an
        # explicit base and triggers the strict collision check against
        # orphan child-named agents.
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"
        mock_result = MagicMock()
        mock_result.pid = 42
        mock_result.workspace_num = 3

        with (
            patch(
                "sase.agent.names.get_next_auto_name",
                return_value="c",
            ) as mock_auto,
            patch(
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[mock_result],
            ) as mock_launch,
        ):
            _launch_agent("%r:3 List all open beads")

        launched_prompt = mock_launch.call_args[0][0]
        assert "%n:" not in launched_prompt
        assert "%r:3" in launched_prompt
        mock_auto.assert_not_called()

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_launch_uses_result_agent_name_without_polling_meta(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
    ) -> None:
        """When the launch result carries an agent_name, Telegram skips the poll."""
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"
        # No agent_meta.json on disk: the result name alone must be enough to
        # render the inline keyboard.
        result = _launch_result(project_name="", timestamp="", agent_name="c")

        with (
            patch(
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[result],
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._resolve_launch_result_agent_name",
                side_effect=AssertionError("must not poll when result name is set"),
            ),
        ):
            _launch_agent("List all open beads")

        call_kwargs = mock_tg.send_message.call_args
        keyboard = call_kwargs.kwargs.get("reply_markup")
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        assert buttons[0][0].text == "🍴 Fork"
        assert buttons[0][0].copy_text.text == "#fork:c "
        assert buttons[1][0].text == "🗡️ Kill"
        assert buttons[1][0].callback_data == "kill:c:go"

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_launch_humanizes_visible_agent_name_only(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _launch_agent

        mock_creds.get_chat_id.return_value = "12345"
        result = _launch_result(
            project_name="",
            timestamp="",
            agent_name="sase_agent",
        )

        with (
            patch(
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[result],
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_cl_name",
                side_effect=lambda name: (
                    "SASE Core_agent" if name == "sase_agent" else name
                ),
            ),
        ):
            _launch_agent("List all open beads")

        call_args = mock_tg.send_message.call_args
        assert "SASE Core\\_agent" in call_args.args[1]
        keyboard = call_args.kwargs.get("reply_markup")
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        assert buttons[0][0].copy_text.text == "#fork:sase_agent "
        assert buttons[0][1].copy_text.text == "%w:sase_agent "
        assert buttons[1][0].callback_data == "kill:sase_agent:go"

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_launch_includes_wait_keyboard(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
        tmp_path: Path,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"
        result = _launch_result(artifacts_dir=str(tmp_path))
        (tmp_path / "agent_meta.json").write_text(
            json.dumps({"name": "c"}), encoding="utf-8"
        )

        with (
            patch(
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[result],
            ),
            patch("sase.agent.names.allocate_retry_name", return_value="c.r1"),
        ):
            _launch_agent("List all open beads")

        call_kwargs = mock_tg.send_message.call_args
        keyboard = call_kwargs.kwargs.get("reply_markup")
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        assert len(buttons) == 2
        assert len(buttons[0]) == 2
        assert buttons[0][0].text == "🍴 Fork"
        assert buttons[0][0].copy_text.text == "#fork:c "
        assert buttons[0][1].text == "⏳ Wait"
        assert buttons[0][1].copy_text.text == "%w:c "
        assert len(buttons[1]) == 2
        assert buttons[1][0].text == "🗡️ Kill"
        assert buttons[1][0].callback_data == "kill:c:go"
        assert buttons[1][1].text == "🔄 Retry"
        assert buttons[1][1].copy_text.text == "%n:c.r1\nList all open beads"

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_launch_fallback_reads_day_sharded_agent_meta(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        monkeypatch.setenv("SASE_HOME", str(tmp_path / ".sase"))
        mock_creds.get_chat_id.return_value = "12345"
        artifacts_dir = (
            tmp_path
            / ".sase"
            / "projects"
            / "proj"
            / "artifacts"
            / "ace-run"
            / "202607"
            / "06"
            / "20260706232107"
        )
        artifacts_dir.mkdir(parents=True)
        (artifacts_dir / "agent_meta.json").write_text(
            json.dumps({"name": "c"}), encoding="utf-8"
        )
        result = _launch_result(
            project_name="proj",
            timestamp="260706_232107",
        )

        with (
            patch(
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[result],
            ),
            patch("sase.agent.names.allocate_retry_name", return_value="c.r1"),
        ):
            _launch_agent("List all open beads")

        keyboard = mock_tg.send_message.call_args.kwargs.get("reply_markup")
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        assert buttons[0][0].text == "🍴 Fork"
        assert buttons[0][0].copy_text.text == "#fork:c "
        assert buttons[0][1].text == "⏳ Wait"
        assert buttons[0][1].copy_text.text == "%w:c "
        assert buttons[1][0].text == "🗡️ Kill"
        assert buttons[1][0].callback_data == "kill:c:go"
        assert buttons[1][1].text == "🔄 Retry"
        mock_pa.add.assert_called_once()
        assert mock_pa.add.call_args.args[0] == "kill-c"

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_launch_retry_button_replaces_existing_name_directive(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"
        mock_result = MagicMock()
        mock_result.pid = 42
        mock_result.workspace_num = 3
        mock_result.agent_name = "c"

        with (
            patch(
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[mock_result],
            ),
            patch("sase.agent.names.allocate_retry_name", return_value="c.r1"),
        ):
            _launch_agent("%n:foo List all open beads")

        keyboard = mock_tg.send_message.call_args.kwargs.get("reply_markup")
        assert keyboard is not None
        retry_button = keyboard.inline_keyboard[1][1]
        assert retry_button.text == "🔄 Retry"
        assert retry_button.copy_text.text == "%n:c.r1 List all open beads"

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_launch_retry_button_uses_callback_for_long_prompt(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _launch_agent,
        )

        mock_creds.get_chat_id.return_value = "12345"
        mock_result = MagicMock()
        mock_result.pid = 42
        mock_result.workspace_num = 3

        long_prompt = "x" * 250

        with (
            patch(
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[mock_result],
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._resolve_launch_result_agent_name",
                return_value="c",
            ),
            patch("sase.agent.names.allocate_retry_name", return_value="c.r1"),
        ):
            _launch_agent(long_prompt)

        call_kwargs = mock_tg.send_message.call_args
        keyboard = call_kwargs.kwargs.get("reply_markup")
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        assert buttons[1][1].text == "🔄 Retry"
        assert buttons[1][1].callback_data == "retry:c:go"
        assert buttons[1][1].copy_text is None
        mock_pa.add.assert_any_call(
            "retry-c",
            {"action": "retry", "prompt": f"%n:c.r1\n{long_prompt}"},
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_launch_copy_buttons_humanize_vcs_tags(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
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
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[mock_result],
            ),
            patch(
                "sase.xprompt.extract_vcs_workflow_tag",
                return_value="#gh:gh_sase-org__sase ",
            ),
            patch("sase.agent.names.allocate_retry_name", return_value="foo.r1"),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_vcs_refs_in_text",
                side_effect=lambda text: text.replace("gh_sase-org__sase", "sase"),
            ),
        ):
            _launch_agent("%n:foo #gh:gh_sase-org__sase Fix a bug")

        call_kwargs = mock_tg.send_message.call_args
        keyboard = call_kwargs.kwargs.get("reply_markup")
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        assert buttons[0][0].text == "🍴 Fork"
        assert buttons[0][0].copy_text.text == "#gh:sase #fork:foo "
        assert buttons[0][1].text == "⏳ Wait"
        assert buttons[0][1].copy_text.text == "#gh:sase %w:foo "
        assert buttons[1][1].text == "🔄 Retry"
        assert buttons[1][1].copy_text.text == "%n:foo.r1 #gh:sase Fix a bug"

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_launch_vcs_tag_uses_at_name_when_pr_present(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
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
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[mock_result],
            ),
            patch(
                "sase.xprompt.extract_vcs_workflow_tag",
                return_value="#gh:sase ",
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._prompt_has_pr_xprompt",
                return_value=True,
            ),
            patch(
                "sase.xprompt.replace_ref_in_vcs_tag",
                return_value="#gh:@foo ",
            ),
        ):
            _launch_agent("%n:foo #gh:sase #pr(fix_bug) Fix a bug")

        call_kwargs = mock_tg.send_message.call_args
        keyboard = call_kwargs.kwargs.get("reply_markup")
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        assert buttons[0][0].text == "🍴 Fork"
        assert buttons[0][0].copy_text.text == "#gh:@foo #fork:foo "
        assert buttons[0][1].text == "⏳ Wait"
        assert buttons[0][1].copy_text.text == "#gh:@foo %w:foo "

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_launch_vcs_tag_unchanged_without_pr(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
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
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[mock_result],
            ),
            patch(
                "sase.xprompt.extract_vcs_workflow_tag",
                return_value="#gh:sase ",
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._prompt_has_pr_xprompt",
                return_value=False,
            ),
        ):
            _launch_agent("%n:foo #gh:sase Fix a bug without pr")

        call_kwargs = mock_tg.send_message.call_args
        keyboard = call_kwargs.kwargs.get("reply_markup")
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        assert buttons[0][0].copy_text.text == "#gh:sase #fork:foo "
        assert buttons[0][1].copy_text.text == "#gh:sase %w:foo "

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_multi_model_launches_via_canonical_pipeline(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
    ) -> None:
        # %{%m:opus | %m:sonnet} must dispatch through ``launch_agents_from_cwd``
        # ONCE — never per-model — so workspace allocation, naming, and
        # retries all happen inside one shared execute_launch_plan invocation.
        from sase_telegram.scripts.sase_tg_inbound import _launch_agent

        mock_creds.get_chat_id.return_value = "12345"
        result_opus = MagicMock()
        result_opus.pid = 100
        result_opus.workspace_num = 100
        result_sonnet = MagicMock()
        result_sonnet.pid = 101
        result_sonnet.workspace_num = 101

        slot_prompts = [
            "%name:c.cld-opus %model:opus Do work",
            "%name:c.cld-sonnet %model:sonnet Do work",
        ]

        with (
            patch(
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[result_opus, result_sonnet],
            ) as mock_launch,
            patch(
                "sase_telegram.scripts.sase_tg_inbound._resolve_slot_prompts",
                return_value=slot_prompts,
            ),
        ):
            _launch_agent("%{%m:opus | %m:sonnet} Do work")

        # The canonical pipeline must be called exactly once with the original
        # multi-model prompt — not split per-model upstream.
        assert mock_launch.call_count == 1
        launched_prompt = mock_launch.call_args[0][0]
        assert not launched_prompt.startswith("%n:")
        assert "%{%m:opus | %m:sonnet}" in launched_prompt
        assert "Do work" in launched_prompt

        # One Telegram launch notification per spawned agent, with the
        # correct workspace numbers preserved.
        assert mock_tg.send_message.call_count == 2
        first_call = mock_tg.send_message.call_args_list[0]
        second_call = mock_tg.send_message.call_args_list[1]
        # Workspace numbers are escaped for MarkdownV2 (``\#`` rather than ``#``).
        assert "workspace \\#100" in first_call[0][1]
        assert "workspace \\#101" in second_call[0][1]
        # Per-slot agent names appear in their respective notifications.
        assert "c\\.cld\\-opus" in first_call[0][1]
        assert "c\\.cld\\-sonnet" in second_call[0][1]

        # Two pending_actions kill entries registered with distinct names.
        kill_keys = sorted(
            call.args[0]
            for call in mock_pa.add.call_args_list
            if call.args and call.args[0].startswith("kill-")
        )
        assert kill_keys == ["kill-c.cld-opus", "kill-c.cld-sonnet"]
        for call in mock_pa.add.call_args_list:
            if call.args and call.args[0].startswith("kill-"):
                # Kill confirmations reuse the source prompt for Redo.
                assert call.args[1]["prompt"] == "%{%m:opus | %m:sonnet} Do work"

    @patch("sase_telegram.scripts.sase_tg_inbound._record_project_context")
    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_multi_model_photo_records_project_context_once(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pa: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        # Multi-model photo launches still record project context once per
        # message and reference the photo file in the launched prompt.
        from sase_telegram.scripts.sase_tg_inbound import _handle_photo_message

        mock_creds.get_chat_id.return_value = "12345"
        result_opus = MagicMock()
        result_opus.pid = 100
        result_opus.workspace_num = 100
        result_sonnet = MagicMock()
        result_sonnet.pid = 101
        result_sonnet.workspace_num = 101

        photo = MagicMock()
        photo.file_id = "abc123"
        message = MagicMock()
        message.photo = [photo]
        message.caption = "%{%m:opus | %m:sonnet} describe this"
        message.caption_entities = []

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound.telegram_client.download_file"
            ),
            patch(
                "sase.agent.launcher.launch_agents_from_cwd",
                return_value=[result_opus, result_sonnet],
            ) as mock_launch,
            patch(
                "sase_telegram.scripts.sase_tg_inbound._resolve_slot_prompts",
                return_value=[
                    "%name:c.cld-opus %model:opus describe this",
                    "%name:c.cld-sonnet %model:sonnet describe this",
                ],
            ),
        ):
            _handle_photo_message(message)

        # Project context recorded once per inbound photo message — not per
        # spawned agent.
        assert mock_record.call_count == 1

        # Single launch call references the downloaded photo file.
        assert mock_launch.call_count == 1
        launched_prompt = mock_launch.call_args[0][0]
        assert not launched_prompt.startswith("%n:")
        assert "%{%m:opus | %m:sonnet}" in launched_prompt
        assert "describe this" in launched_prompt

        # Two notifications, one per spawned agent.
        assert mock_tg.send_message.call_count == 2


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
        save_awaiting_feedback("42", "abcd1234", {"action_type": "gate", "dir": "/tmp"})
        loaded = load_awaiting_feedback("42")
        assert loaded is not None
        assert loaded["prefix"] == "abcd1234"
        assert loaded["action_info"]["action_type"] == "gate"

    def test_clear_specific_key(self) -> None:
        save_awaiting_feedback("42", "abcd1234", {"action_type": "gate"})
        assert load_awaiting_feedback("42") is not None
        clear_awaiting_feedback("42")
        assert load_awaiting_feedback("42") is None

    def test_clear_all_when_no_file(self) -> None:
        # Should not raise
        clear_awaiting_feedback()
        assert load_awaiting_feedback() is None

    def test_concurrent_entries_do_not_overwrite(self) -> None:
        save_awaiting_feedback(
            "42", "abcd1234", {"action_type": "gate", "dir": "/tmp/a"}
        )
        save_awaiting_feedback(
            "43", "efgh5678", {"action_type": "question", "dir": "/tmp/b"}
        )
        all_aw = load_all_awaiting_feedback()
        assert set(all_aw) == {"42", "43"}
        assert all_aw["42"]["prefix"] == "abcd1234"
        assert all_aw["43"]["prefix"] == "efgh5678"

    def test_clear_one_leaves_others_intact(self) -> None:
        save_awaiting_feedback("42", "abcd1234", {"action_type": "gate"})
        save_awaiting_feedback("43", "efgh5678", {"action_type": "question"})
        clear_awaiting_feedback("42")
        assert load_awaiting_feedback("42") is None
        remaining = load_awaiting_feedback("43")
        assert remaining is not None
        assert remaining["prefix"] == "efgh5678"

    def test_clear_by_prefix_finds_matching_entry(self) -> None:
        save_awaiting_feedback("42", "abcd1234", {"action_type": "gate"})
        save_awaiting_feedback("43", "efgh5678", {"action_type": "question"})
        cleared = clear_awaiting_feedback_by_prefix("abcd1234")
        assert cleared == "42"
        assert load_awaiting_feedback("42") is None
        assert load_awaiting_feedback("43") is not None

    def test_clear_by_prefix_no_match_returns_none(self) -> None:
        save_awaiting_feedback("42", "abcd1234", {"action_type": "gate"})
        assert clear_awaiting_feedback_by_prefix("zzzz9999") is None
        assert load_awaiting_feedback("42") is not None

    def test_load_without_key_returns_unique_entry(self) -> None:
        save_awaiting_feedback("42", "abcd1234", {"action_type": "gate"})
        loaded = load_awaiting_feedback()
        assert loaded is not None
        assert loaded["prefix"] == "abcd1234"

    def test_load_without_key_returns_none_when_ambiguous(self) -> None:
        save_awaiting_feedback("42", "abcd1234", {"action_type": "gate"})
        save_awaiting_feedback("43", "efgh5678", {"action_type": "question"})
        # With multiple entries and no key, the caller cannot disambiguate.
        assert load_awaiting_feedback() is None

    def test_legacy_single_entry_file_loads(self, tmp_path: Path) -> None:
        # Old format: a flat single-entry object (no per-key map).
        AWAITING_TEST_PATH.write_text(
            json.dumps(
                {
                    "prefix": "legacy01",
                    "action_info": {"action_type": "gate", "dir": str(tmp_path)},
                }
            )
        )
        # No specific key known — falls back to the lone entry.
        loaded = load_awaiting_feedback()
        assert loaded is not None
        assert loaded["prefix"] == "legacy01"
        # Prefix-based clear still works against the normalized entry.
        cleared = clear_awaiting_feedback_by_prefix("legacy01")
        assert cleared is not None
        assert load_awaiting_feedback() is None

    def test_process_text_message_keyed_lookup(self, tmp_path: Path) -> None:
        gate_bundle = tmp_path / "gate"
        gate_bundle.mkdir()
        save_awaiting_feedback(
            "42",
            "gate0001",
            {
                "action_type": "gate",
                "bundle_path": str(gate_bundle),
                "selected_option_ids": ["feedback"],
                "input_data": {},
                "feedback_is_command_input": False,
            },
        )
        save_awaiting_feedback(
            "43",
            "ques0001",
            {
                "action_type": "question",
                "response_dir": str(tmp_path),
                "question_text": "?",
            },
        )
        # Reply targeting message 42 -> unified gate flow.
        result = process_text_message("fix it", key="42")
        assert result is not None
        assert result.action_type == "gate"
        assert result.notif_id_prefix == "gate0001"
        assert result.selected_option_ids == ("feedback",)
        assert result.feedback == "fix it"

    def test_process_text_message_ambiguous_returns_none(self, tmp_path: Path) -> None:
        save_awaiting_feedback("42", "a", {"action_type": "gate"})
        save_awaiting_feedback("43", "b", {"action_type": "question"})
        # No key, two entries -> cannot disambiguate.
        assert process_text_message("fix it") is None


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

    def test_multi_image_prompt_lists_all_paths(self, tmp_path: Path) -> None:
        first = tmp_path / "first.jpg"
        second = tmp_path / "second.jpg"
        result = build_image_prompt([first, second], "#gh@sase Compare these")
        assert "#gh:sase Compare these" in result
        assert f"1. {first}" in result
        assert f"2. {second}" in result
        assert "respond to the user's request" in result


class TestBeadProjectContext:
    """Tests for resolving the project context used by /bead."""

    def test_extract_project_from_prompt(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _extract_project_from_prompt

        assert _extract_project_from_prompt("#gh:sase Fix the bug") == "sase"
        assert _extract_project_from_prompt("#gh@sase Fix the bug") == "sase"
        assert _extract_project_from_prompt("%n:foo #gh_sase Fix the bug") == "sase"
        assert _extract_project_from_prompt("#git(sase-telegram) Fix it") == (
            "sase-telegram"
        )
        assert _extract_project_from_prompt("#fork:foo #gh:zorg Continue") == "zorg"
        assert _extract_project_from_prompt("#sase__research #gh:zorg Fix") == "zorg"
        assert _extract_project_from_prompt("#gh:@foo Continue work") is None
        assert _extract_project_from_prompt("plain prompt") is None

    def test_extract_project_from_wrapped_image_prompt(self, tmp_path: Path) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _extract_project_from_prompt

        assert (
            _extract_project_from_prompt(
                build_photo_prompt(tmp_path / "photo.jpg", "#gh_sase Fix the bug")
            )
            == "sase"
        )
        assert (
            _extract_project_from_prompt(
                build_photo_prompt(tmp_path / "photo.jpg", "#gh:zorg Fix the bug")
            )
            == "zorg"
        )

    def test_records_project_context_for_chat(self, tmp_path: Path) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        workspace = tmp_path / "zorg"
        workspace.mkdir()
        context_path = tmp_path / "project_context.json"
        message = SimpleNamespace(chat=SimpleNamespace(id=12345))

        with (
            patch.object(inbound, "_PROJECT_CONTEXT_PATH", context_path),
            patch.object(inbound.time, "time", return_value=1777770889.0),
            patch.object(
                inbound, "_resolve_workspace_for_project", return_value=str(workspace)
            ) as resolve_workspace,
        ):
            inbound._record_project_context("#gh:zorg Fix the bug", message)

        resolve_workspace.assert_called_once_with("zorg", "launch_prompt")
        payload = json.loads(context_path.read_text())
        assert payload == {
            "12345": {
                "project": "zorg",
                "workspace": str(workspace),
                "updated_at": 1777770889.0,
                "source": "launch_prompt",
            }
        }

    def test_persisted_chat_context_beats_newer_pending_prompt(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        from pytest import MonkeyPatch

        assert isinstance(monkeypatch, MonkeyPatch)
        from sase_telegram.scripts import sase_tg_inbound as inbound

        zorg_workspace = tmp_path / "zorg"
        zorg_workspace.mkdir()
        context_path = tmp_path / "project_context.json"
        context_path.write_text(
            json.dumps(
                {
                    "12345": {
                        "project": "zorg",
                        "workspace": str(zorg_workspace),
                        "updated_at": 1.0,
                        "source": "launch_prompt",
                    }
                }
            )
        )
        message = SimpleNamespace(chat=SimpleNamespace(id=12345))
        monkeypatch.delenv("SASE_TELEGRAM_BEAD_PROJECT", raising=False)

        with (
            patch.object(inbound, "_PROJECT_CONTEXT_PATH", context_path),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.pending_actions.list_all",
                return_value={
                    "newer": {
                        "prompt": "#gh:sase Newer unrelated prompt",
                        "chat_id": "12345",
                        "created_at": 2,
                    }
                },
            ) as list_all_mock,
            patch.object(inbound, "_resolve_workspace_for_project") as resolve_mock,
        ):
            assert inbound._resolve_bead_cwd(message=message) == str(zorg_workspace)

        list_all_mock.assert_not_called()
        resolve_mock.assert_not_called()

    def test_pending_prompt_resolution_is_chat_scoped(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        from pytest import MonkeyPatch

        assert isinstance(monkeypatch, MonkeyPatch)
        from sase_telegram.scripts import sase_tg_inbound as inbound

        zorg_workspace = tmp_path / "zorg"
        zorg_workspace.mkdir()
        sase_workspace = tmp_path / "sase"
        sase_workspace.mkdir()
        context_path = tmp_path / "missing_context.json"
        message = SimpleNamespace(chat=SimpleNamespace(id=12345))
        monkeypatch.delenv("SASE_TELEGRAM_BEAD_PROJECT", raising=False)

        def fake_workspace(project: str, _slot: int) -> str:
            return str(tmp_path / project)

        with (
            patch.object(inbound, "_PROJECT_CONTEXT_PATH", context_path),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.pending_actions.list_all",
                return_value={
                    "other-chat-newer": {
                        "prompt": "#gh:sase Newer unrelated prompt",
                        "chat_id": "999",
                        "created_at": 2,
                    },
                    "same-chat-older": {
                        "prompt": "#gh:zorg Older relevant prompt",
                        "chat_id": "12345",
                        "created_at": 1,
                    },
                },
            ),
            patch(
                "sase.running_field.get_workspace_directory",
                side_effect=fake_workspace,
            ) as get_workspace,
        ):
            assert inbound._resolve_bead_cwd(message=message) == str(zorg_workspace)

        get_workspace.assert_called_once_with("zorg", 1)

    def test_env_project_takes_precedence(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        from pytest import MonkeyPatch

        assert isinstance(monkeypatch, MonkeyPatch)
        workspace = tmp_path / "override"
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setenv("SASE_TELEGRAM_BEAD_PROJECT", "override")

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound.pending_actions.list_all",
                return_value={
                    "new": {
                        "prompt": "#gh:sase Fix the bug",
                        "created_at": 2,
                    }
                },
            ) as list_all_mock,
            patch(
                "sase.running_field.get_workspace_directory",
                return_value=str(workspace),
            ) as get_workspace_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                return_value=completed,
            ) as run_mock,
        ):
            from sase_telegram.scripts.sase_tg_inbound import _run_bead_command

            _run_bead_command(["list"])

        get_workspace_mock.assert_called_once_with("override", 1)
        list_all_mock.assert_not_called()
        assert run_mock.call_args.kwargs["cwd"] == str(workspace)

    def test_resolves_project_from_pending_prompt(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        from pytest import MonkeyPatch

        assert isinstance(monkeypatch, MonkeyPatch)
        workspace = tmp_path / "sase"
        monkeypatch.delenv("SASE_TELEGRAM_BEAD_PROJECT", raising=False)

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound.pending_actions.list_all",
                return_value={
                    "old": {"prompt": "#gh:other Old task", "created_at": 1},
                    "new": {
                        "action_data": {"prompt": "%n:c #gh_sase Fix the bug"},
                        "created_at": 2,
                    },
                },
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._PROJECT_CONTEXT_PATH",
                tmp_path / "missing_context.json",
            ),
            patch(
                "sase.running_field.get_workspace_directory",
                return_value=str(workspace),
            ) as get_workspace_mock,
        ):
            from sase_telegram.scripts.sase_tg_inbound import _resolve_bead_cwd

            assert _resolve_bead_cwd() == str(workspace)

        get_workspace_mock.assert_called_once_with("sase", 1)

    def test_project_file_workspace_fallback(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        from pytest import MonkeyPatch

        assert isinstance(monkeypatch, MonkeyPatch)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        project_dir = tmp_path / ".sase" / "projects" / "sase"
        project_dir.mkdir(parents=True)
        (project_dir / "sase.sase").write_text(f"WORKSPACE_DIR: {workspace}\n")
        monkeypatch.delenv("SASE_TELEGRAM_BEAD_PROJECT", raising=False)

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound.Path.home",
                return_value=tmp_path,
            ),
            patch(
                "sase.running_field.get_workspace_directory",
                side_effect=RuntimeError("workspace plugin unavailable"),
            ),
        ):
            from sase_telegram.scripts.sase_tg_inbound import (
                _resolve_workspace_for_project,
            )

            assert _resolve_workspace_for_project("sase", "test") == str(workspace)

    def test_project_file_workspace_fallback_legacy_gp(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        """Legacy ``.gp`` project spec resolves when no ``.sase`` file exists."""
        from pytest import MonkeyPatch

        assert isinstance(monkeypatch, MonkeyPatch)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        project_dir = tmp_path / ".sase" / "projects" / "sase"
        project_dir.mkdir(parents=True)
        (project_dir / "sase.gp").write_text(f"WORKSPACE_DIR: {workspace}\n")
        monkeypatch.delenv("SASE_TELEGRAM_BEAD_PROJECT", raising=False)

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound.Path.home",
                return_value=tmp_path,
            ),
            patch(
                "sase.running_field.get_workspace_directory",
                side_effect=RuntimeError("workspace plugin unavailable"),
            ),
        ):
            from sase_telegram.scripts.sase_tg_inbound import (
                _resolve_workspace_for_project,
            )

            assert _resolve_workspace_for_project("sase", "test") == str(workspace)

    def test_run_bead_command_uses_resolved_cwd(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        from pytest import MonkeyPatch

        assert isinstance(monkeypatch, MonkeyPatch)
        workspace = tmp_path / "sase"
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.delenv("SASE_TELEGRAM_BEAD_PROJECT", raising=False)

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound.pending_actions.list_all",
                return_value={"ctx": {"prompt": "#gh:sase Fix", "created_at": 1}},
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._PROJECT_CONTEXT_PATH",
                tmp_path / "missing_context.json",
            ),
            patch(
                "sase.running_field.get_workspace_directory",
                return_value=str(workspace),
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                return_value=completed,
            ) as run_mock,
        ):
            from sase_telegram.scripts.sase_tg_inbound import _run_bead_command

            _run_bead_command(["show", "sase-13"])

        assert run_mock.call_args[0][0] == ["sase", "bead", "show", "sase-13"]
        assert run_mock.call_args.kwargs["cwd"] == str(workspace)

    def test_run_bead_command_falls_back_without_context(
        self, monkeypatch: object
    ) -> None:
        from pytest import MonkeyPatch

        assert isinstance(monkeypatch, MonkeyPatch)
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.delenv("SASE_TELEGRAM_BEAD_PROJECT", raising=False)

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound.pending_actions.list_all",
                return_value={},
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._PROJECT_CONTEXT_PATH",
                Path("/tmp/missing_sase_telegram_project_context.json"),
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                return_value=completed,
            ) as run_mock,
        ):
            from sase_telegram.scripts.sase_tg_inbound import _run_bead_command

            _run_bead_command(["list"])

        assert run_mock.call_args[0][0] == ["sase", "bead", "list"]
        assert "cwd" not in run_mock.call_args.kwargs


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


class TestNormalizeLaunchXpromptAtRefs:
    def test_normalizes_known_workspace_workflow_refs(self) -> None:
        assert normalize_launch_xprompt_at_refs("#gh@sase Fix") == "#gh:sase Fix"
        assert (
            normalize_launch_xprompt_at_refs("%n:a #git@repo Fix")
            == "%n:a #git:repo Fix"
        )
        assert normalize_launch_xprompt_at_refs("(#hg@change)") == "(#hg:change)"
        assert (
            normalize_launch_xprompt_at_refs('"#jj@workspace" #p4@client')
            == '"#jj:workspace" #p4:client'
        )
        assert normalize_launch_xprompt_at_refs("#cd@repo Fix") == "#cd:repo Fix"

    def test_preserves_existing_canonical_forms(self) -> None:
        assert normalize_launch_xprompt_at_refs("#gh:sase Fix") == "#gh:sase Fix"
        assert normalize_launch_xprompt_at_refs("#gh_sase Fix") == "#gh_sase Fix"

    def test_does_not_rewrite_non_launch_text(self) -> None:
        assert normalize_launch_xprompt_at_refs("@someone") == "@someone"
        assert normalize_launch_xprompt_at_refs("name@example.com") == (
            "name@example.com"
        )
        assert normalize_launch_xprompt_at_refs("/start@bot") == "/start@bot"
        assert normalize_launch_xprompt_at_refs("#topic@sase") == "#topic@sase"
        assert normalize_launch_xprompt_at_refs("word#gh@sase") == "word#gh@sase"

    def test_skips_inline_and_fenced_code(self) -> None:
        text = "run `#gh@sase` then #gh@zorg"
        assert normalize_launch_xprompt_at_refs(text) == "run `#gh@sase` then #gh:zorg"

        fenced = "```\n#gh@sase\n```\n#git@repo"
        assert (
            normalize_launch_xprompt_at_refs(fenced) == "```\n#gh@sase\n```\n#git:repo"
        )


class TestHandlePhotoMessage:
    """Tests for _handle_photo_message (script module)."""

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
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
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
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

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_normalizes_caption_before_wrapping_prompt(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        tmp_path: Path,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_photo_message

        mock_creds.get_chat_id.return_value = "12345"
        photo = SimpleNamespace(file_id="photo_id_12345678")
        message = SimpleNamespace(
            photo=[photo],
            caption="#gh@sase Describe this",
            caption_entities=None,
        )

        with (
            patch("sase_telegram.scripts.sase_tg_inbound.IMAGES_DIR", tmp_path),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._record_project_context"
            ) as mock_record,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            _handle_photo_message(message)

        prompt = mock_launch.call_args[0][0]
        assert "#gh:sase Describe this" in prompt
        assert "#gh@sase" not in prompt
        mock_record.assert_called_once_with(prompt, message)

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
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
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            _handle_photo_message(message)

        mock_launch.assert_not_called()
        mock_tg.send_message.assert_called_once()
        error_msg = mock_tg.send_message.call_args[0][1]
        assert "Failed to download photo" in error_msg


class TestHandleDocumentImage:
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_normalizes_caption_before_wrapping_prompt(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        tmp_path: Path,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_document_image

        mock_creds.get_chat_id.return_value = "12345"
        doc = SimpleNamespace(file_id="doc_id_12345678", file_name="image.png")
        message = SimpleNamespace(
            document=doc,
            caption="#git@repo Inspect this",
            caption_entities=None,
        )

        with (
            patch("sase_telegram.scripts.sase_tg_inbound.IMAGES_DIR", tmp_path),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._record_project_context"
            ) as mock_record,
            patch("sase_telegram.scripts.sase_tg_inbound._launch_agent") as mock_launch,
        ):
            _handle_document_image(message)

        prompt = mock_launch.call_args[0][0]
        assert "#git:repo Inspect this" in prompt
        assert "#git@repo" not in prompt
        mock_record.assert_called_once_with(prompt, message)


class TestSendKillResult:
    """Tests for _send_kill_result (kill confirmation message)."""

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_short_prompt_includes_redo_button(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pending: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _send_kill_result

        mock_creds.get_chat_id.return_value = "12345"
        result = SimpleNamespace(success=True, message="Killed")
        kill_info = {"prompt": "short prompt", "chat_id": "12345", "message_id": 1}

        _send_kill_result("a", result, kill_info)

        call_kwargs = mock_tg.send_message.call_args
        keyboard = call_kwargs.kwargs.get("reply_markup")
        assert keyboard is not None
        assert keyboard.inline_keyboard[0][0].text == "🔄 Redo"
        assert keyboard.inline_keyboard[0][0].copy_text.text == "short prompt"

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_success_humanizes_visible_agent_name_only(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pending: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _send_kill_result

        mock_creds.get_chat_id.return_value = "12345"
        result = SimpleNamespace(success=True, message="Killed")
        kill_info = {"prompt": "short prompt", "chat_id": "12345", "message_id": 1}

        with patch(
            "sase_telegram.scripts.sase_tg_inbound.display_cl_name",
            side_effect=lambda name: (
                "SASE Core_agent" if name == "sase_agent" else name
            ),
        ):
            _send_kill_result("sase_agent", result, kill_info)

        call_args = mock_tg.send_message.call_args
        assert "SASE Core\\_agent" in call_args.args[1]
        keyboard = call_args.kwargs.get("reply_markup")
        assert keyboard is not None
        assert keyboard.inline_keyboard[0][0].copy_text.text == "short prompt"
        mock_pending.remove.assert_called_once_with("kill-sase_agent")

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_long_prompt_uses_callback_redo_button(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pending: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _send_kill_result

        mock_creds.get_chat_id.return_value = "12345"
        result = SimpleNamespace(success=True, message="Killed")
        long_prompt = "x" * 300
        kill_info = {"prompt": long_prompt, "chat_id": "12345", "message_id": 1}

        _send_kill_result("a", result, kill_info)

        # Should store the original prompt in pending_actions for callback retrieval.
        mock_pending.add.assert_called_once_with(
            "retry-a",
            {"action": "retry", "prompt": long_prompt},
        )

        # Should include a callback-based Redo button.
        call_kwargs = mock_tg.send_message.call_args
        keyboard = call_kwargs.kwargs.get("reply_markup")
        assert keyboard is not None
        btn = keyboard.inline_keyboard[0][0]
        assert btn.text == "🔄 Redo"
        assert btn.callback_data == "retry:a:go"

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_artifact_fallback_preserves_existing_name_directive(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pending: MagicMock,
        tmp_path: Path,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_kill_from_callback

        (tmp_path / "raw_xprompt.md").write_text(
            "%n:a #gh:gh_sase-org__sase Do work",
            encoding="utf-8",
        )
        mock_creds.get_chat_id.return_value = "12345"
        mock_pending.get.return_value = None
        result = SimpleNamespace(success=True, message="Killed")
        callback = SimpleNamespace(id="cb1")

        with (
            patch(
                "sase.agent.names.find_named_agent",
                return_value=SimpleNamespace(artifacts_dir=str(tmp_path)),
            ),
            patch("sase.agent.running.kill_named_agent", return_value=result),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_vcs_refs_in_text",
                side_effect=lambda text: text.replace("gh_sase-org__sase", "sase"),
            ),
        ):
            _handle_kill_from_callback(callback, "a")

        call_kwargs = mock_tg.send_message.call_args
        keyboard = call_kwargs.kwargs.get("reply_markup")
        assert keyboard is not None
        assert keyboard.inline_keyboard[0][0].text == "🔄 Redo"
        assert keyboard.inline_keyboard[0][0].copy_text.text == (
            "%n:a #gh:sase Do work"
        )


def _running_agent(
    name: str | None,
    *,
    project: str = "proj",
    pid: int | None = 1234,
    model: str = "opus-4-7",
    workspace_num: int | None = 1,
    duration: str = "5m",
    approve: bool = False,
    prompt: str | None = None,
    status: str = "RUNNING",
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        project=project,
        pid=pid,
        model=model,
        provider="claude",
        workspace_num=workspace_num,
        duration=duration,
        approve=approve,
        prompt=prompt,
        status=status,
    )


def _list_entry(
    name: str,
    *,
    project: str = "sase",
    pid: int = 1234,
    model: str | None = "opus",
    provider_badge: str | None = "🎭",
    workspace_num: int | None = 1,
    duration: str = "5m",
    prompt: str | None = None,
    status: str = "RUNNING",
    status_bucket: str = "Running",
    status_glyph: str = "▶",
    reasoning_effort: str | None = None,
    vcs_provider_display: str | None = None,
    approve: bool = False,
    activity: str | None = None,
    finished_at: object | None = None,
    wait: SimpleNamespace | None = None,
    retry: SimpleNamespace | None = None,
    children_badge: str | None = None,
    children_count: int = 0,
    children_status_counts: tuple[tuple[str, int], ...] = (),
    is_terminal: bool | None = None,
    tribe: str | None = None,
    agent_clan: str | None = None,
    agent_clan_generation: str | None = None,
    clan_tribe: str | None = None,
    agent_family: str | None = None,
    agent_family_role: str | None = None,
    parent_agent_name: str | None = None,
) -> SimpleNamespace:
    wait_info = wait or SimpleNamespace(
        wait_for=(),
        wait_duration_seconds=None,
        wait_until=None,
        remaining_seconds=None,
        has_wait=False,
    )
    retry_info = retry or SimpleNamespace(
        retry_attempt=None,
        retry_of_timestamp=None,
        retried_as_timestamp=None,
        retry_error_category=None,
        has_retry=False,
    )
    return SimpleNamespace(
        name=name,
        project=project,
        pid=pid,
        model=model,
        provider_badge=provider_badge,
        workspace_num=workspace_num,
        duration=duration,
        duration_seconds=300,
        started_at=None,
        finished_at=finished_at,
        prompt=prompt,
        status=status,
        status_bucket=status_bucket,
        status_glyph=status_glyph,
        approve=approve,
        artifacts_dir="/tmp/artifacts",
        timestamp="20260709120000",
        reasoning_effort=reasoning_effort,
        vcs_provider_display=vcs_provider_display,
        tag=None,
        bead_id=None,
        changespec_name=None,
        cl_name=None,
        workflow_name=None,
        tribe=tribe,
        agent_clan=agent_clan,
        agent_clan_generation=agent_clan_generation,
        clan_tribe=clan_tribe,
        agent_family=agent_family,
        agent_family_role=agent_family_role,
        parent_agent_name=parent_agent_name,
        plan=False,
        plan_approved=False,
        plan_action=None,
        auto_approve_plan_action=None,
        pending_question=status == "QUESTION",
        question_answered=status == "ANSWERED",
        wait=wait_info,
        retry=retry_info,
        children=SimpleNamespace(
            badge=children_badge,
            count=children_count,
            status_counts=children_status_counts,
        ),
        activity=activity,
        output_variables={},
        artifact_count=0,
        commit_count=0,
        error=None,
        traceback=None,
        has_file_changes=False,
        auto_badge="⚡" if approve else None,
        is_terminal=(
            status_bucket in {"Done", "Failed"} if is_terminal is None else is_terminal
        ),
        needs_user_action=status in {"PLAN", "QUESTION"},
    )


class TestHandleListCommand:
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_empty_result(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_list_command

        mock_creds.get_chat_id.return_value = "12345"
        with patch(
            "sase.integrations.agent_list_entries.agent_list_entries",
            return_value=[],
        ):
            _handle_list_command()

        mock_tg.send_message.assert_called_once()
        assert mock_tg.send_message.call_args.args[:2] == (
            "12345",
            "🤖 <b>Agents</b> · 0 active\n\nNo running agents.",
        )
        assert mock_tg.send_message.call_args.kwargs["parse_mode"] == "HTML"

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_running_group_with_details_and_html_escaping(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_list_command

        mock_creds.get_chat_id.return_value = "12345"
        agents = [
            _list_entry(
                "<alpha>",
                project="sase&core",
                model="opus<4>",
                prompt="do <thing>\nnow",
                reasoning_effort="high",
                vcs_provider_display="GitHub",
                activity="writing <tests>",
                approve=True,
            )
        ]
        with patch(
            "sase.integrations.agent_list_entries.agent_list_entries",
            return_value=agents,
        ):
            _handle_list_command()

        mock_tg.send_message.assert_called_once()
        args = mock_tg.send_message.call_args.args
        kwargs = mock_tg.send_message.call_args.kwargs
        text = args[1]
        assert kwargs["parse_mode"] == "HTML"
        assert text.startswith("🤖 <b>Agents</b> · 1 active")
        assert "<b>▶ Running (1)</b>" in text
        assert "🎭 <b>&lt;alpha&gt;</b> · opus&lt;4&gt; @ high · ▶ 5m ⚡" in text
        assert "sase&amp;core · ws#1 · PID 1234 · GitHub" in text
        assert "<i>writing &lt;tests&gt;</i>" in text
        assert "<blockquote>do &lt;thing&gt; now</blockquote>" in text
        assert kwargs["reply_markup"] is not None

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_project_detail_uses_display_project_name(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_list_command

        mock_creds.get_chat_id.return_value = "12345"
        agents = [_list_entry("alpha", project="sase-core")]
        with (
            patch(
                "sase.integrations.agent_list_entries.agent_list_entries",
                return_value=agents,
            ),
            patch(
                "sase_telegram.agent_format.display_project_name",
                return_value="SASE & Core",
            ),
        ):
            _handle_list_command()

        text = mock_tg.send_message.call_args.args[1]
        assert "SASE &amp; Core · ws#1 · PID 1234" in text
        assert "sase-core · ws#1" not in text

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_prompt_snippet_humanizes_vcs_refs(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_list_command

        mock_creds.get_chat_id.return_value = "12345"
        agents = [_list_entry("alpha", prompt="#gh:gh_sase-org__sase Fix")]
        with (
            patch(
                "sase.integrations.agent_list_entries.agent_list_entries",
                return_value=agents,
            ),
            patch(
                "sase_telegram.agent_format.display_cl_names_in_text",
                side_effect=lambda text: text.replace("gh_sase-org__sase", "sase"),
            ),
        ):
            _handle_list_command()

        text = mock_tg.send_message.call_args.args[1]
        assert "<blockquote>#gh:sase Fix</blockquote>" in text

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_multiple_statuses_use_bucket_order_and_preserve_agent_order(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_list_command

        mock_creds.get_chat_id.return_value = "12345"
        agents = [
            _list_entry("run-1", status="RUNNING", status_bucket="Running"),
            _list_entry(
                "done-1", status="DONE", status_bucket="Done", status_glyph="✓"
            ),
            _list_entry("run-2", status="RUNNING", status_bucket="Running"),
            _list_entry(
                "question-1",
                status="QUESTION",
                status_bucket="Stopped",
                status_glyph="▲",
            ),
        ]
        with patch(
            "sase.integrations.agent_list_entries.agent_list_entries",
            return_value=agents,
        ):
            _handle_list_command("all")

        text = mock_tg.send_message.call_args.args[1]
        needs_idx = text.index("<b>▲ Stopped (1)</b>")
        running_idx = text.index("<b>▶ Running (2)</b>")
        done_idx = text.index("<b>✓ Done (1)</b>")
        assert needs_idx < running_idx < done_idx
        assert text.index("run-1") < text.index("run-2")

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_name_arg_renders_detail_with_buttons(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_list_command

        mock_creds.get_chat_id.return_value = "12345"
        agents = [
            _list_entry(
                "alpha",
                reasoning_effort="high",
                prompt="Full prompt",
                vcs_provider_display="GitHub",
                activity="writing tests",
            )
        ]
        with (
            patch(
                "sase.integrations.agent_list_entries.agent_list_entries",
                return_value=agents,
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._get_agent_retry_prompt",
                return_value="Full prompt",
            ),
        ):
            _handle_list_command("alpha")

        text = mock_tg.send_message.call_args.args[1]
        assert "🎭 <b>alpha</b> — details" in text
        assert "<pre>" in text
        assert "Model      opus @ high" in text
        assert "VCS        GitHub" in text
        assert "Activity   writing tests" in text
        assert "<blockquote expandable>Full prompt</blockquote>" in text
        keyboard = mock_tg.send_message.call_args.kwargs["reply_markup"]
        buttons = keyboard.inline_keyboard
        assert buttons[0][0].text == "🍴 Fork"
        assert buttons[0][1].text == "⏳ Wait"
        assert buttons[1][0].callback_data == "kill:alpha:go"

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_project_arg_filters_overview(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_list_command

        mock_creds.get_chat_id.return_value = "12345"
        agents = [
            _list_entry("alpha", project="sase"),
            _list_entry("bravo", project="zorg"),
        ]
        with patch(
            "sase.integrations.agent_list_entries.agent_list_entries",
            return_value=agents,
        ):
            _handle_list_command("zorg")

        text = mock_tg.send_message.call_args.args[1]
        assert "zorg" in text
        assert "bravo" in text
        assert "alpha" not in text
        assert mock_tg.send_message.call_args.kwargs["reply_markup"] is None

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_refresh_callback_edits_list_message(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_callback

        mock_creds.get_chat_id.return_value = "12345"
        callback = SimpleNamespace(
            id="cb1",
            data="list:active:refresh",
            message=SimpleNamespace(
                message_id=99,
                chat=SimpleNamespace(id="12345"),
            ),
        )
        with patch(
            "sase.integrations.agent_list_entries.agent_list_entries",
            return_value=[_list_entry("alpha")],
        ):
            _handle_callback(callback, {})

        mock_tg.edit_message_text.assert_called_once()
        args = mock_tg.edit_message_text.call_args.args
        kwargs = mock_tg.edit_message_text.call_args.kwargs
        assert args[:2] == ("12345", 99)
        assert "alpha" in args[2]
        assert kwargs["parse_mode"] == "HTML"
        assert kwargs["reply_markup"] is not None
        mock_tg.answer_callback_query.assert_called_once_with("cb1", "Refreshed")


class TestHandleShowCommand:
    def test_dispatches_show_with_message_context(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_command

        message = SimpleNamespace(chat=SimpleNamespace(id="12345"))
        with patch(
            "sase_telegram.scripts.sase_tg_inbound._handle_show_command"
        ) as handler:
            _handle_command("/show review", message=message)

        handler.assert_called_once_with("review", message=message)

    def test_show_is_registered_as_slash_command(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _SLASH_COMMANDS

        assert ("show", "Show an agent, clan, family, or tribe") in _SLASH_COMMANDS

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_bare_show_sends_kinship_index(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_show_command

        mock_creds.get_chat_id.return_value = "12345"
        with patch(
            "sase.integrations.agent_list_entries.agent_list_entries",
            return_value=[],
        ):
            _handle_show_command()

        args = mock_tg.send_message.call_args.args
        kwargs = mock_tg.send_message.call_args.kwargs
        assert args[0] == "12345"
        assert "🧭 <b>Agents by kinship</b>" in args[1]
        assert "No grouped agents yet" in args[1]
        assert kwargs["parse_mode"] == "HTML"

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_agent_show_uses_enriched_detail_and_jump_buttons(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pending: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_show_command
        from sase_telegram.show_entities import ShowTarget

        mock_creds.get_chat_id.return_value = "12345"
        entry = _list_entry(
            "alpha",
            tribe="perf",
            agent_clan="review",
            agent_clan_generation="generation-12345678",
            agent_family="migrate",
            agent_family_role="planner",
            parent_agent_name="parent",
            children_count=2,
            children_status_counts=(("Running", 1), ("Done", 1)),
            prompt="Full prompt",
        )
        target = ShowTarget(kind="agent", name="alpha", entry=entry, entries=(entry,))
        with (
            patch(
                "sase.integrations.agent_list_entries.agent_list_entries",
                return_value=[entry],
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.resolve_show_reference",
                return_value=target,
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound._get_agent_retry_prompt",
                return_value="Full prompt",
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.generate_key",
                side_effect=["clankey", "familykey"],
            ),
        ):
            _handle_show_command("alpha")

        text = mock_tg.send_message.call_args.args[1]
        assert "Clan       ⛺ review · gen 12345678" in text
        assert "Tribe      @perf" in text
        assert "Family     migrate · planner" in text
        assert "Parent     parent" in text
        assert "Children   2 · 1 running, 1 done" in text
        keyboard = mock_tg.send_message.call_args.kwargs["reply_markup"]
        assert [button.text for button in keyboard.inline_keyboard[-1]] == [
            "⛺ Clan",
            "🧬 Family",
        ]
        assert keyboard.inline_keyboard[-1][0].callback_data == "show:clankey:open"
        assert keyboard.inline_keyboard[-1][1].callback_data == ("show:familykey:open")
        assert mock_pending.add.call_count == 2

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_clan_show_sends_group_view(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        _mock_pending: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_show_command
        from sase_telegram.show_entities import ShowTarget

        mock_creds.get_chat_id.return_value = "12345"
        member = SimpleNamespace(name="review.a", outcome="completed")
        clan = SimpleNamespace(
            name="review",
            generation="generation-12345678",
            members=(member,),
            is_complete=True,
        )
        target = ShowTarget(
            kind="clan",
            name="review",
            clan=clan,
            clan_tribe="perf",
            clan_summary="Review the hot path",
        )
        with (
            patch(
                "sase.integrations.agent_list_entries.agent_list_entries",
                return_value=[],
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.resolve_show_reference",
                return_value=target,
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.generate_key",
                side_effect=["refreshkey", "memberkey"],
            ),
        ):
            _handle_show_command("review")

        text = mock_tg.send_message.call_args.args[1]
        assert "⛺ <b>review</b> — clan · @perf · gen 12345678 · ✓ complete" in text
        assert "<i>Review the hot path</i>" in text
        keyboard = mock_tg.send_message.call_args.kwargs["reply_markup"]
        assert keyboard.inline_keyboard[0][0].callback_data == (
            "show:refreshkey:refresh"
        )
        assert keyboard.inline_keyboard[0][1].copy_text.text == "#fork:review "

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_open_callback_sends_fresh_message(
        self, mock_tg: MagicMock, mock_pending: MagicMock
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_callback

        callback = SimpleNamespace(
            id="cb1",
            data="show:selection:open",
            message=SimpleNamespace(
                message_id=99,
                chat=SimpleNamespace(id="12345"),
            ),
        )
        mock_pending.get.return_value = {"action": "show", "ref": "review"}
        with patch(
            "sase_telegram.scripts.sase_tg_inbound._render_show_reference",
            return_value=(["<b>review</b>"], None),
        ):
            _handle_callback(callback, {})

        mock_tg.send_message.assert_called_once_with(
            "12345",
            "<b>review</b>",
            parse_mode="HTML",
            reply_markup=None,
        )
        mock_tg.answer_callback_query.assert_called_once_with("cb1", "Opened")

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_refresh_callback_edits_single_chunk(
        self, mock_tg: MagicMock, mock_pending: MagicMock
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_callback

        callback = SimpleNamespace(
            id="cb1",
            data="show:selection:refresh",
            message=SimpleNamespace(
                message_id=99,
                chat=SimpleNamespace(id="12345"),
            ),
        )
        mock_pending.get.return_value = {"action": "show", "ref": "review"}
        with patch(
            "sase_telegram.scripts.sase_tg_inbound._render_show_reference",
            return_value=(["<b>review</b>"], None),
        ):
            _handle_callback(callback, {})

        mock_tg.edit_message_text.assert_called_once_with(
            "12345",
            99,
            "<b>review</b>",
            reply_markup=None,
            parse_mode="HTML",
        )
        mock_tg.answer_callback_query.assert_called_once_with("cb1", "Refreshed")

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_expired_callback_is_answered(
        self, mock_tg: MagicMock, mock_pending: MagicMock
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_callback

        mock_pending.get.return_value = None
        callback = SimpleNamespace(id="cb1", data="show:expired:open")
        _handle_callback(callback, {})

        mock_tg.answer_callback_query.assert_called_once_with(
            "cb1", "Selection expired — run /show again"
        )
        mock_tg.send_message.assert_not_called()

    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_error_path_sends_friendly_failure(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_show_command

        mock_creds.get_chat_id.return_value = "12345"
        with (
            patch(
                "sase.integrations.agent_list_entries.agent_list_entries",
                return_value=[],
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.resolve_show_reference",
                side_effect=RuntimeError("bad artifact"),
            ),
        ):
            _handle_show_command("review")

        mock_tg.send_message.assert_called_once_with(
            "12345", "Failed to build /show view."
        )


class TestHandleKillSelection:
    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_humanizes_visible_labels_and_persists_callback_key(
        self,
        mock_tg: MagicMock,
        mock_pending: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _show_kill_selection

        agents = [_running_agent("sase_agent")]
        with (
            patch("sase.agent.running.list_running_agents", return_value=agents),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.generate_key",
                return_value="killkey1",
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_cl_name",
                side_effect=lambda name: (
                    "SASE Core_agent" if name == "sase_agent" else name
                ),
            ),
        ):
            _show_kill_selection("12345")

        call_args = mock_tg.send_message.call_args
        text = call_args.args[1]
        assert "<b>SASE Core_agent</b>" in text
        keyboard = call_args.kwargs["reply_markup"]
        button = keyboard.inline_keyboard[0][0]
        assert button.text == "SASE Core_agent"
        assert button.callback_data == "kill:killkey1:select"
        mock_pending.add.assert_called_once_with(
            "kill-selection",
            {
                "action": "kill-selection",
                "agent_names": {"killkey1": "sase_agent"},
            },
        )

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_long_agent_name_roundtrips_through_persisted_key(
        self,
        mock_tg: MagicMock,
        mock_pending: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import (
            _handle_callback,
            _show_kill_selection,
        )

        long_name = "split_file." + ("very_long_clan_member." * 4)
        agents = [_running_agent(long_name)]
        with (
            patch("sase.agent.running.list_running_agents", return_value=agents),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.generate_key",
                return_value="longkey1",
            ),
        ):
            _show_kill_selection("12345")

        keyboard = mock_tg.send_message.call_args.kwargs["reply_markup"]
        callback_data = keyboard.inline_keyboard[0][0].callback_data
        assert callback_data == "kill:longkey1:select"
        assert len(callback_data.encode("utf-8")) <= 64

        mock_pending.get.return_value = {
            "action": "kill-selection",
            "agent_names": {"longkey1": long_name},
        }
        callback = SimpleNamespace(id="cb1", data=callback_data)
        with patch(
            "sase_telegram.scripts.sase_tg_inbound._handle_kill_from_callback"
        ) as mock_kill:
            _handle_callback(callback, {})

        mock_kill.assert_called_once_with(callback, long_name)

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_bad_button_is_skipped_without_aborting_selection(
        self,
        mock_tg: MagicMock,
        mock_pending: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _show_kill_selection

        agents = [_running_agent("bad-agent"), _running_agent("good-agent")]

        def encode_selection(action: str, key: str, choice: str) -> str:
            if key == "badkey":
                raise ValueError("cannot encode this button")
            return f"{action}:{key}:{choice}"

        with (
            patch("sase.agent.running.list_running_agents", return_value=agents),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.generate_key",
                side_effect=["badkey", "goodkey"],
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.encode",
                side_effect=encode_selection,
            ),
            caplog.at_level("WARNING"),
        ):
            _show_kill_selection("12345")

        keyboard = mock_tg.send_message.call_args.kwargs["reply_markup"]
        assert len(keyboard.inline_keyboard) == 1
        assert keyboard.inline_keyboard[0][0].text == "good-agent"
        assert keyboard.inline_keyboard[0][0].callback_data == ("kill:goodkey:select")
        mock_pending.add.assert_called_once_with(
            "kill-selection",
            {
                "action": "kill-selection",
                "agent_names": {"goodkey": "good-agent"},
            },
        )
        assert "Skipping /kill selection button for agent bad-agent" in caplog.text

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    def test_expired_selection_is_answered_without_killing(
        self,
        mock_tg: MagicMock,
        mock_pending: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_callback

        mock_pending.get.return_value = None
        callback = SimpleNamespace(id="cb1", data="kill:missing:select")
        with patch(
            "sase_telegram.scripts.sase_tg_inbound._handle_kill_from_callback"
        ) as mock_kill:
            _handle_callback(callback, {})

        mock_kill.assert_not_called()
        mock_tg.answer_callback_query.assert_called_once_with(
            "cb1",
            "This kill selection has expired",
        )


class TestHandleForkCommand:
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_humanizes_visible_labels_and_copy_vcs_but_keeps_agent_ref_raw(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_fork_command

        mock_creds.get_chat_id.return_value = "12345"
        agents = [_running_agent("sase_agent", prompt="#gh:gh_sase-org__sase Fix")]
        with (
            patch("sase.agent.running.list_running_agents", return_value=agents),
            patch(
                "sase.xprompt.extract_vcs_workflow_tag",
                return_value="#gh:gh_sase-org__sase ",
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_cl_name",
                side_effect=lambda name: (
                    "SASE Core_agent" if name == "sase_agent" else name
                ),
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_vcs_refs_in_text",
                side_effect=lambda text: text.replace("gh_sase-org__sase", "sase"),
            ),
        ):
            _handle_fork_command()

        call_args = mock_tg.send_message.call_args
        assert "<b>SASE Core_agent</b>" in call_args.args[1]
        keyboard = call_args.kwargs["reply_markup"]
        button = keyboard.inline_keyboard[0][0]
        assert button.text == "🍴 SASE Core_agent"
        assert button.copy_text.text == "#gh:sase #fork:sase_agent "


class TestHandleRetryFromCallback:
    """Tests for _handle_retry_from_callback."""

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_sends_prompt_as_message(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pending: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_retry_from_callback

        mock_creds.get_chat_id.return_value = "12345"
        prompt = "%n:agent1.r1\n" + ("x" * 500)
        mock_pending.get.return_value = {"action": "retry", "prompt": prompt}

        cb = SimpleNamespace(id="cb1")
        _handle_retry_from_callback(cb, "agent1")

        mock_tg.send_message.assert_called_once_with("12345", prompt)
        mock_tg.answer_callback_query.assert_called_once_with("cb1", "Prompt sent")
        mock_pending.remove.assert_called_once_with("retry-agent1")

    @patch("sase_telegram.scripts.sase_tg_inbound.pending_actions")
    @patch("sase_telegram.scripts.sase_tg_inbound.telegram_client")
    @patch("sase_telegram.scripts.sase_tg_inbound.credentials")
    def test_expired_retry_shows_unavailable(
        self,
        mock_creds: MagicMock,
        mock_tg: MagicMock,
        mock_pending: MagicMock,
    ) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_retry_from_callback

        mock_pending.get.return_value = None

        cb = SimpleNamespace(id="cb1")
        _handle_retry_from_callback(cb, "agent1")

        mock_tg.send_message.assert_not_called()
        mock_tg.answer_callback_query.assert_called_once_with(
            "cb1", "Retry prompt no longer available"
        )


class TestXpromptsCommand:
    """Tests for _handle_xprompts_command."""

    def test_xprompts_command_builds_and_sends_pdf(self, tmp_path: Path) -> None:
        from datetime import datetime

        from sase.xprompt.catalog import CatalogArtifact, CatalogStats

        pdf_path = tmp_path / "catalog.pdf"
        pdf_path.write_bytes(b"%PDF-fake")
        fake_artifact = CatalogArtifact(
            pdf_path=pdf_path,
            stats=CatalogStats(
                total=5,
                by_source={"built-in": 5},
                by_project={},
                by_tag={},
                with_description=5,
                with_inputs=0,
                skills=0,
                generated_at=datetime(2026, 4, 24),
            ),
        )
        with (
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as tc_mock,
            patch("sase_telegram.scripts.sase_tg_inbound.credentials") as cred_mock,
            patch(
                "sase.xprompt.catalog.build_xprompts_catalog",
                return_value=fake_artifact,
            ) as build_mock,
        ):
            cred_mock.get_chat_id.return_value = "12345"
            from sase_telegram.scripts.sase_tg_inbound import (
                _handle_xprompts_command,
            )

            _handle_xprompts_command()

        build_mock.assert_called_once()
        assert tc_mock.send_message.call_count == 1  # the ack
        tc_mock.send_document.assert_called_once()
        _args, kwargs = tc_mock.send_document.call_args
        assert kwargs.get("parse_mode") == "HTML"
        assert "xprompts Catalog" in kwargs.get("caption", "")

    def test_xprompts_command_handles_pdf_engine_unavailable(
        self, tmp_path: Path
    ) -> None:
        from sase.xprompt.catalog import PdfEngineUnavailable

        with (
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as tc_mock,
            patch("sase_telegram.scripts.sase_tg_inbound.credentials") as cred_mock,
            patch(
                "sase.xprompt.catalog.build_xprompts_catalog",
                side_effect=PdfEngineUnavailable("no engine"),
            ),
        ):
            cred_mock.get_chat_id.return_value = "12345"
            from sase_telegram.scripts.sase_tg_inbound import (
                _handle_xprompts_command,
            )

            _handle_xprompts_command()

        assert tc_mock.send_message.call_count == 2  # ack + error
        tc_mock.send_document.assert_not_called()


class TestBeadCommand:
    """Tests for _handle_bead_command."""

    def setup_method(self) -> None:
        self._resolve_patcher = patch(
            "sase_telegram.scripts.sase_tg_inbound._resolve_bead_cwd",
            return_value=None,
        )
        self._known_projects_patcher = patch(
            "sase_telegram.scripts.sase_tg_inbound._iter_known_project_workspaces",
            return_value=[],
        )
        self._resolve_patcher.start()
        self._known_projects_patcher.start()

    def teardown_method(self) -> None:
        self._known_projects_patcher.stop()
        self._resolve_patcher.stop()

    def test_missing_arg_shows_picker(self) -> None:
        stdout = (
            "○ sase-13 · DELTAS ChangeSpec Field\n"
            "◐ sase-13.5 · Phase 5: Lifecycle Wiring ← sase-13\n"
        )
        completed = SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        with (
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as tc_mock,
            patch("sase_telegram.scripts.sase_tg_inbound.credentials") as cred_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                return_value=completed,
            ) as run_mock,
        ):
            cred_mock.get_chat_id.return_value = "12345"
            from sase_telegram.scripts.sase_tg_inbound import _handle_bead_command

            _handle_bead_command("")

        run_mock.assert_called_once()
        assert run_mock.call_args[0][0] == [
            "sase",
            "bead",
            "list",
            "--status=open",
            "--status=in_progress",
        ]
        tc_mock.send_message.assert_called_once()
        args, kwargs = tc_mock.send_message.call_args
        assert args[0] == "12345"
        assert kwargs.get("parse_mode") == "HTML"

        keyboard = kwargs.get("reply_markup")
        assert keyboard is not None
        rows = keyboard.inline_keyboard
        assert len(rows) == 2

        from sase_telegram.callback_data import decode

        assert decode(rows[0][0].callback_data) == ("bead", "sase-13", "show")
        assert decode(rows[1][0].callback_data) == ("bead", "sase-13.5", "show")

    def test_missing_arg_project_override_uses_active_status_filters(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        from pytest import MonkeyPatch

        assert isinstance(monkeypatch, MonkeyPatch)
        monkeypatch.setenv("SASE_TELEGRAM_BEAD_PROJECT", "override")

        from sase_telegram.scripts import sase_tg_inbound as inbound

        workspace = tmp_path / "override"
        workspace.mkdir()
        completed = SimpleNamespace(
            returncode=0, stdout="No issues found.\n", stderr=""
        )
        with (
            patch.object(inbound, "_resolve_bead_cwd", return_value=str(workspace)),
            patch.object(
                inbound,
                "_iter_known_project_workspaces",
                side_effect=AssertionError(
                    "override should bypass project aggregation"
                ),
            ),
            patch.object(inbound, "telegram_client") as tc_mock,
            patch.object(inbound, "credentials") as cred_mock,
            patch.object(inbound.subprocess, "run", return_value=completed) as run_mock,
        ):
            cred_mock.get_chat_id.return_value = "12345"
            inbound._handle_bead_command("")

        assert run_mock.call_args[0][0] == [
            "sase",
            "bead",
            "list",
            "--status=open",
            "--status=in_progress",
        ]
        assert run_mock.call_args.kwargs["cwd"] == str(workspace)
        tc_mock.send_message.assert_called_once_with("12345", "No active beads.")

    def test_missing_arg_lists_all_known_project_beads(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        from pytest import MonkeyPatch

        assert isinstance(monkeypatch, MonkeyPatch)
        monkeypatch.delenv("SASE_TELEGRAM_BEAD_PROJECT", raising=False)

        from sase_telegram.scripts import sase_tg_inbound as inbound

        closed_workspace = tmp_path / "done"
        sase_workspace = tmp_path / "sase"
        zorg_workspace = tmp_path / "zorg"
        closed_workspace.mkdir()
        sase_workspace.mkdir()
        zorg_workspace.mkdir()

        projects = [
            inbound._KnownProjectWorkspace("done", str(closed_workspace)),
            inbound._KnownProjectWorkspace("sase", str(sase_workspace)),
            inbound._KnownProjectWorkspace("zorg", str(zorg_workspace)),
        ]

        def fake_run(
            cmd: list[str],
            *,
            capture_output: bool,
            text: bool,
            check: bool,
            cwd: str | None = None,
        ) -> SimpleNamespace:
            assert cmd == [
                "sase",
                "bead",
                "list",
                "--status=open",
                "--status=in_progress",
            ]
            assert capture_output is True
            assert text is True
            assert check is False
            if cwd == str(closed_workspace):
                # Explicit active filters suppress the CLI's closed fallback.
                return SimpleNamespace(
                    returncode=0, stdout="No issues found.\n", stderr=""
                )
            if cwd == str(sase_workspace):
                return SimpleNamespace(
                    returncode=0,
                    stdout="○ sase-1 · Build all-project bead picker\n",
                    stderr="",
                )
            if cwd == str(zorg_workspace):
                return SimpleNamespace(
                    returncode=0,
                    stdout="◐ zorg-2 · Follow-up routing\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected cwd: {cwd}")

        with (
            patch.object(
                inbound, "_iter_known_project_workspaces", return_value=projects
            ),
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as tc_mock,
            patch("sase_telegram.scripts.sase_tg_inbound.credentials") as cred_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                side_effect=fake_run,
            ) as run_mock,
        ):
            cred_mock.get_chat_id.return_value = "12345"
            inbound._handle_bead_command("")

        assert run_mock.call_count == 3
        tc_mock.send_message.assert_called_once()
        _args, kwargs = tc_mock.send_message.call_args
        keyboard = kwargs.get("reply_markup")
        assert keyboard is not None
        rows = keyboard.inline_keyboard
        assert len(rows) == 2

        from sase_telegram.callback_data import decode

        assert rows[0][0].text == "○ sase-1: Build all-project bead picker"
        assert decode(rows[0][0].callback_data) == ("bead", "sase/sase-1", "show")
        assert rows[1][0].text == "◐ zorg-2: Follow-up routing"
        assert decode(rows[1][0].callback_data) == ("bead", "zorg/zorg-2", "show")

    def test_duplicate_project_bead_labels_use_display_project_only(self) -> None:
        from sase_telegram.callback_data import decode
        from sase_telegram.scripts import sase_tg_inbound as inbound

        entries = [
            inbound._ProjectBeadEntry(
                project="sase",
                workspace="/tmp/sase",
                icon="○",
                bead_id="same-1",
                title="First",
            ),
            inbound._ProjectBeadEntry(
                project="zorg",
                workspace="/tmp/zorg",
                icon="◐",
                bead_id="same-1",
                title="Second",
            ),
        ]

        with (
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as tc_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_project_name",
                side_effect=lambda project: {
                    "sase": "SASE Core",
                    "zorg": "Zorg App",
                }.get(project, project),
            ),
        ):
            inbound._render_bead_selection("12345", entries)

        keyboard = tc_mock.send_message.call_args.kwargs["reply_markup"]
        rows = keyboard.inline_keyboard
        assert rows[0][0].text == "○ SASE Core/same-1: First"
        assert decode(rows[0][0].callback_data) == ("bead", "sase/same-1", "show")
        assert rows[1][0].text == "◐ Zorg App/same-1: Second"
        assert decode(rows[1][0].callback_data) == ("bead", "zorg/same-1", "show")

    def test_project_bead_errors_use_display_project_name(self) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        projects = [inbound._KnownProjectWorkspace("sase", "/tmp/sase")]
        failed = SimpleNamespace(returncode=1, stdout="", stderr="db locked")

        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._run_bead_command",
                return_value=failed,
            ),
            patch(
                "sase_telegram.scripts.sase_tg_inbound.display_project_name",
                return_value="SASE Core",
            ),
        ):
            entries, errors = inbound._project_bead_entries(projects)

        assert entries == []
        assert errors == ["SASE Core: db locked"]

    def test_missing_arg_list_uses_resolved_bead_cwd(self, tmp_path: Path) -> None:
        workspace = tmp_path / "sase"
        workspace.mkdir()
        completed = SimpleNamespace(
            returncode=0,
            stdout="○ sase-13 · DELTAS ChangeSpec Field\n",
            stderr="",
        )
        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._resolve_bead_cwd",
                return_value=str(workspace),
            ),
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client"),
            patch("sase_telegram.scripts.sase_tg_inbound.credentials") as cred_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                return_value=completed,
            ) as run_mock,
        ):
            cred_mock.get_chat_id.return_value = "12345"
            from sase_telegram.scripts.sase_tg_inbound import _handle_bead_command

            _handle_bead_command("")

        assert run_mock.call_args[0][0] == [
            "sase",
            "bead",
            "list",
            "--status=open",
            "--status=in_progress",
        ]
        assert run_mock.call_args.kwargs["cwd"] == str(workspace)

    def test_missing_arg_empty_list(self) -> None:
        completed = SimpleNamespace(
            returncode=0, stdout="No issues found.\n", stderr=""
        )
        with (
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as tc_mock,
            patch("sase_telegram.scripts.sase_tg_inbound.credentials") as cred_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                return_value=completed,
            ),
        ):
            cred_mock.get_chat_id.return_value = "12345"
            from sase_telegram.scripts.sase_tg_inbound import _handle_bead_command

            _handle_bead_command("")

        tc_mock.send_message.assert_called_once_with("12345", "No active beads.")

    def test_missing_arg_subprocess_error(self) -> None:
        completed = SimpleNamespace(
            returncode=1, stdout="", stderr="Error: db locked\n"
        )
        with (
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as tc_mock,
            patch("sase_telegram.scripts.sase_tg_inbound.credentials") as cred_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                return_value=completed,
            ),
        ):
            cred_mock.get_chat_id.return_value = "12345"
            from sase_telegram.scripts.sase_tg_inbound import _handle_bead_command

            _handle_bead_command("")

        tc_mock.send_message.assert_called_once()
        _args, kwargs = tc_mock.send_message.call_args
        assert kwargs.get("parse_mode") == "MarkdownV2"
        body = tc_mock.send_message.call_args[0][1]
        assert body.startswith("```\n")
        assert body.endswith("\n```")
        assert "db locked" in body

    def test_callback_invokes_bead_show(self) -> None:
        show_completed = SimpleNamespace(
            returncode=0,
            stdout="○ sase-13 · DELTAS   [OPEN]\nType: plan · Owner: x@y\n",
            stderr="",
        )
        with (
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as tc_mock,
            patch("sase_telegram.scripts.sase_tg_inbound.credentials") as cred_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                return_value=show_completed,
            ) as run_mock,
        ):
            cred_mock.get_chat_id.return_value = "12345"
            from sase_telegram.scripts.sase_tg_inbound import _handle_callback

            cb = SimpleNamespace(id="cb1", data="bead:sase-13:show")
            _handle_callback(cb, {})

        tc_mock.answer_callback_query.assert_called_once_with("cb1", "Loading sase-13…")
        run_mock.assert_called_once()
        assert run_mock.call_args[0][0] == ["sase", "bead", "show", "sase-13"]

    def test_project_aware_callback_invokes_bead_show_in_project(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "zorg"
        workspace.mkdir()
        show_completed = SimpleNamespace(
            returncode=0,
            stdout="○ zorg-1 · Routing   [OPEN]\nType: plan · Owner: x@y\n",
            stderr="",
        )
        with (
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as tc_mock,
            patch("sase_telegram.scripts.sase_tg_inbound.credentials") as cred_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound._resolve_workspace_for_project",
                return_value=str(workspace),
            ) as resolve_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                return_value=show_completed,
            ) as run_mock,
        ):
            cred_mock.get_chat_id.return_value = "12345"
            from sase_telegram.scripts.sase_tg_inbound import _handle_callback

            cb = SimpleNamespace(id="cb1", data="bead:zorg/zorg-1:show")
            _handle_callback(cb, {})

        tc_mock.answer_callback_query.assert_called_once_with("cb1", "Loading zorg-1…")
        resolve_mock.assert_called_once_with("zorg", "bead callback")
        run_mock.assert_called_once()
        assert run_mock.call_args[0][0] == ["sase", "bead", "show", "zorg-1"]
        assert run_mock.call_args.kwargs["cwd"] == str(workspace)

    def test_success_renders_markdown(self) -> None:
        stdout = (
            "○ sase-13 · DELTAS ChangeSpec Field   [OPEN]\n"
            "Type: plan · Owner: bryanbugyi34@gmail.com\n"
            "\n"
            "CHILDREN\n"
            "  ✓ sase-13.1: Phase 1\n"
        )
        completed = SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        with (
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as tc_mock,
            patch("sase_telegram.scripts.sase_tg_inbound.credentials") as cred_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                return_value=completed,
            ) as run_mock,
        ):
            cred_mock.get_chat_id.return_value = "12345"
            from sase_telegram.scripts.sase_tg_inbound import _handle_bead_command

            _handle_bead_command("sase-13")

        run_mock.assert_called_once()
        cmd = run_mock.call_args[0][0]
        assert cmd == ["sase", "bead", "show", "sase-13"]
        tc_mock.send_message.assert_called_once()
        _args, kwargs = tc_mock.send_message.call_args
        assert kwargs.get("parse_mode") == "MarkdownV2"
        body = tc_mock.send_message.call_args[0][1]
        # markdown_to_telegram_v2 escapes punctuation; check the bead id is present.
        assert "sase\\-13" in body
        assert "Children" in body or "CHILDREN" in body

    def test_subprocess_error_forwards_stderr(self) -> None:
        completed = SimpleNamespace(
            returncode=1, stdout="", stderr="Error: issue not found: bogus\n"
        )
        with (
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client") as tc_mock,
            patch("sase_telegram.scripts.sase_tg_inbound.credentials") as cred_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                return_value=completed,
            ),
        ):
            cred_mock.get_chat_id.return_value = "12345"
            from sase_telegram.scripts.sase_tg_inbound import _handle_bead_command

            _handle_bead_command("bogus")

        tc_mock.send_message.assert_called_once()
        _args, kwargs = tc_mock.send_message.call_args
        assert kwargs.get("parse_mode") == "MarkdownV2"
        body = tc_mock.send_message.call_args[0][1]
        assert body.startswith("```\n")
        assert body.endswith("\n```")
        assert "issue not found: bogus" in body

    def test_strips_extra_whitespace_and_takes_first_token(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout="○ sase-1 · X   [OPEN]\nType: phase · Owner: x@y\n",
            stderr="",
        )
        with (
            patch("sase_telegram.scripts.sase_tg_inbound.telegram_client"),
            patch("sase_telegram.scripts.sase_tg_inbound.credentials") as cred_mock,
            patch(
                "sase_telegram.scripts.sase_tg_inbound.subprocess.run",
                return_value=completed,
            ) as run_mock,
        ):
            cred_mock.get_chat_id.return_value = "12345"
            from sase_telegram.scripts.sase_tg_inbound import _handle_bead_command

            _handle_bead_command("  sase-1   extra args\n")

        cmd = run_mock.call_args[0][0]
        assert cmd == ["sase", "bead", "show", "sase-1"]


class TestFindExternallyHandled:
    """Tests for external completion of v2 gates and questions."""

    def test_gate_response_detected(self, tmp_path: Path) -> None:
        bundle = tmp_path / "gate"
        bundle.mkdir()
        request_path = bundle / "request.json"
        response_path = bundle / "response.json"
        request_path.write_text("{}")
        pending = _make_pending_gate(
            "gate0001", bundle, request_path=request_path, response_path=response_path
        )

        assert find_externally_handled(pending) == []
        response_path.write_text("{}")
        assert find_externally_handled(pending) == [("gate0001", 42, "12345")]

    def test_gate_cancellation_detected(self, tmp_path: Path) -> None:
        bundle = tmp_path / "gate"
        bundle.mkdir()
        request_path = bundle / "request.json"
        request_path.write_text("{}")
        pending = _make_pending_gate("gate0002", bundle, request_path=request_path)

        (bundle / "cancellation.json").write_text("{}")
        assert find_externally_handled(pending) == [("gate0002", 42, "12345")]

    def test_removed_gate_request_detected(self, tmp_path: Path) -> None:
        bundle = tmp_path / "gate"
        bundle.mkdir()
        pending = _make_pending_gate(
            "gate0003", bundle, request_path=bundle / "request.json"
        )

        assert find_externally_handled(pending) == [("gate0003", 42, "12345")]

    def test_question_response_detected(self, tmp_path: Path) -> None:
        (tmp_path / "question_request.json").write_text("{}")
        pending = _make_pending_question("ques0001", str(tmp_path))
        assert find_externally_handled(pending) == []

        (tmp_path / "question_response.json").write_text("{}")
        assert find_externally_handled(pending) == [("ques0001", 42, "12345")]

    def test_non_actionable_and_malformed_entries_are_skipped(self) -> None:
        pending = {
            "kill-agent1": {"action": "kill"},
            "bad-gate": {"action": "CustomGate", "action_data": "invalid"},
        }
        assert find_externally_handled(pending) == []


def _shared_entry(
    *,
    state: str = "available",
    transport: str = "telegram",
    chat_id: str | None = "12345",
    message_id: int | None = 42,
    stale_deadline_unix: float = 10_000.0,
) -> dict[str, object]:
    record: dict[str, object] = {}
    if chat_id is not None:
        record["chat_id"] = chat_id
    if message_id is not None:
        record["message_id"] = message_id
    return {
        "action": "PlanApproval",
        "state": state,
        "stale_deadline_unix": stale_deadline_unix,
        "transports": [
            {"transport": "notification_store", "record": {}},
            {"transport": transport, "record": record},
        ],
    }


class TestFindSharedHandledTransports:
    """Tests for shared-store transport cleanup detection."""

    def test_already_handled_entry_returned(self) -> None:
        store = {"actions": {"plan0001": _shared_entry(state="already_handled")}}
        assert find_shared_handled_transports(store, now=0.0) == [
            ("plan0001", 42, "12345")
        ]

    def test_available_entry_skipped(self) -> None:
        store = {"actions": {"plan0001": _shared_entry(state="available")}}
        assert find_shared_handled_transports(store, now=0.0) == []

    def test_stale_state_returned(self) -> None:
        store = {"actions": {"plan0001": _shared_entry(state="stale")}}
        assert find_shared_handled_transports(store, now=0.0) == [
            ("plan0001", 42, "12345")
        ]

    def test_passed_deadline_returned(self) -> None:
        store = {
            "actions": {
                "plan0001": _shared_entry(state="available", stale_deadline_unix=5.0)
            }
        }
        assert find_shared_handled_transports(store, now=9.0) == [
            ("plan0001", 42, "12345")
        ]

    def test_legacy_transport_returned(self) -> None:
        store = {
            "actions": {
                "plan0001": _shared_entry(
                    state="already_handled", transport="telegram_legacy"
                )
            }
        }
        assert find_shared_handled_transports(store, now=0.0) == [
            ("plan0001", 42, "12345")
        ]

    def test_entry_without_telegram_transport_skipped(self) -> None:
        store = {
            "actions": {
                "plan0001": {
                    "action": "PlanApproval",
                    "state": "already_handled",
                    "stale_deadline_unix": 10_000.0,
                    "transports": [{"transport": "notification_store", "record": {}}],
                }
            }
        }
        assert find_shared_handled_transports(store, now=0.0) == []

    def test_record_missing_ids_skipped(self) -> None:
        store = {
            "actions": {
                "plan0001": _shared_entry(state="already_handled", message_id=None)
            }
        }
        assert find_shared_handled_transports(store, now=0.0) == []


def _custom_command(
    *,
    name: str = "tasks",
    output: str = "message",
    description: str = "Tasks dashboard",
    timeout_seconds: int = 90,
):
    from sase_telegram.custom_commands import CustomCommand

    return CustomCommand(
        name=name,
        description=description,
        argv=("tg_cmd_tasks",),
        output=output,
        timeout_seconds=timeout_seconds,
    )


class TestCustomCommandDispatch:
    def test_dispatches_configured_command_with_raw_remainder(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_command

        command = _custom_command()
        with patch(
            "sase_telegram.scripts.sase_tg_inbound._handle_custom_command"
        ) as handler:
            _handle_command(
                "/tasks first  second; $(literal) ",
                custom_commands={"tasks": command},
            )

        handler.assert_called_once_with(command, "first  second; $(literal) ")

    def test_builtin_command_wins_over_custom_mapping(self) -> None:
        from sase_telegram.scripts.sase_tg_inbound import _handle_command

        command = _custom_command(name="list")
        with (
            patch(
                "sase_telegram.scripts.sase_tg_inbound._handle_list_command"
            ) as builtin,
            patch(
                "sase_telegram.scripts.sase_tg_inbound._handle_custom_command"
            ) as custom,
        ):
            _handle_command("/list", custom_commands={"list": command})

        builtin.assert_called_once_with("")
        custom.assert_not_called()

    def test_registration_puts_sorted_custom_commands_before_builtins(
        self, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        cache_path = tmp_path / "commands_registered_ts"
        custom_commands = {
            "zeta": _custom_command(name="zeta", description="Last"),
            "alpha": _custom_command(name="alpha", description="First"),
        }
        expected = [
            ("alpha", "First"),
            ("zeta", "Last"),
        ] + inbound._SLASH_COMMANDS
        with (
            patch.object(inbound, "_COMMANDS_REGISTERED_PATH", cache_path),
            patch.object(inbound.time, "time", return_value=1001.0),
            patch.object(
                inbound.telegram_client, "set_my_commands", return_value=True
            ) as register,
        ):
            inbound._register_commands_if_needed(custom_commands)

        register.assert_called_once_with(expected)
        payload = json.loads(cache_path.read_text())
        assert payload["timestamp"] == 1001.0
        assert payload["fingerprint"] == inbound._slash_commands_fingerprint(expected)

    def test_configured_first_order_invalidates_recent_old_fingerprint(
        self, tmp_path: Path
    ) -> None:
        from sase_telegram.scripts import sase_tg_inbound as inbound

        cache_path = tmp_path / "commands_registered_ts"
        custom_commands = {"tasks": _custom_command()}
        custom_payload = [("tasks", "Tasks dashboard")]
        old_order = inbound._SLASH_COMMANDS + custom_payload
        new_order = custom_payload + inbound._SLASH_COMMANDS
        cache_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "timestamp": 1000.0,
                    "fingerprint": inbound._slash_commands_fingerprint(old_order),
                }
            )
        )

        with (
            patch.object(inbound, "_COMMANDS_REGISTERED_PATH", cache_path),
            patch.object(inbound.time, "time", return_value=1001.0),
            patch.object(
                inbound.telegram_client, "set_my_commands", return_value=True
            ) as register,
        ):
            inbound._register_commands_if_needed(custom_commands)

        register.assert_called_once_with(new_order)
        payload = json.loads(cache_path.read_text())
        assert payload["timestamp"] == 1001.0
        assert payload["fingerprint"] == inbound._slash_commands_fingerprint(new_order)


class TestCustomCommandDelivery:
    def test_message_output_is_formatted_and_sent(self) -> None:
        from sase_telegram.custom_commands import CommandResult
        from sase_telegram.scripts import sase_tg_inbound as inbound

        with (
            patch.object(inbound.credentials, "get_chat_id", return_value="12345"),
            patch.object(
                inbound,
                "run_custom_command",
                return_value=CommandResult("# Tasks\n\n- one\n", "", 0),
            ) as run,
            patch.object(inbound.telegram_client, "send_message") as send,
        ):
            inbound._handle_custom_command(_custom_command(), "next only")

        run.assert_called_once_with(_custom_command(), "next only")
        send.assert_called_once()
        assert "Tasks" in send.call_args.args[1]
        assert send.call_args.kwargs["parse_mode"] == "MarkdownV2"

    def test_nonzero_exit_sends_bounded_expandable_stderr(self) -> None:
        from sase_telegram.custom_commands import CommandResult
        from sase_telegram.scripts import sase_tg_inbound as inbound

        stderr = "old\n" + "x" * 1200 + "<latest>"
        with (
            patch.object(inbound.credentials, "get_chat_id", return_value="12345"),
            patch.object(
                inbound,
                "run_custom_command",
                return_value=CommandResult("", stderr, 7),
            ),
            patch.object(inbound.telegram_client, "send_message") as send,
        ):
            inbound._handle_custom_command(_custom_command(), "")

        text = send.call_args.args[1]
        assert "/tasks" in text
        assert "exit 7" in text
        assert "<blockquote expandable>" in text
        assert "&lt;latest&gt;" in text
        assert "old" not in text
        assert send.call_args.kwargs["parse_mode"] == "HTML"

    def test_timeout_reports_configured_duration(self) -> None:
        from sase_telegram.custom_commands import CommandResult
        from sase_telegram.scripts import sase_tg_inbound as inbound

        with (
            patch.object(inbound.credentials, "get_chat_id", return_value="12345"),
            patch.object(
                inbound,
                "run_custom_command",
                return_value=CommandResult("", "", None, timed_out=True),
            ),
            patch.object(inbound.telegram_client, "send_message") as send,
        ):
            inbound._handle_custom_command(_custom_command(), "")

        assert "timed out after 90s" in send.call_args.args[1]

    def test_empty_stdout_reports_empty_result(self) -> None:
        from sase_telegram.custom_commands import CommandResult
        from sase_telegram.scripts import sase_tg_inbound as inbound

        with (
            patch.object(inbound.credentials, "get_chat_id", return_value="12345"),
            patch.object(
                inbound,
                "run_custom_command",
                return_value=CommandResult(" \n", "", 0),
            ),
            patch.object(inbound.telegram_client, "send_message") as send,
        ):
            inbound._handle_custom_command(_custom_command(), "")

        assert "produced no output" in send.call_args.args[1]

    def test_pdf_conversion_failure_falls_back_to_markdown(self) -> None:
        from sase_telegram.custom_commands import CommandResult
        from sase_telegram.scripts import sase_tg_inbound as inbound

        stdout = "---\ncaption: Tasks dashboard\nfilename: tasks.pdf\n---\n\n# Tasks\n"
        with (
            patch.object(inbound.credentials, "get_chat_id", return_value="12345"),
            patch.object(
                inbound,
                "run_custom_command",
                return_value=CommandResult(stdout, "", 0),
            ),
            patch.object(inbound.pdf_convert, "md_to_pdf", return_value=None),
            patch.object(inbound.telegram_client, "send_message") as send,
            patch.object(inbound.telegram_client, "send_document") as document,
        ):
            inbound._handle_custom_command(_custom_command(output="pdf"), "")

        assert send.call_count == 2
        assert "Running" in send.call_args_list[0].args[1]
        assert "PDF conversion failed" in send.call_args_list[1].args[1]
        assert "Tasks" in send.call_args_list[1].args[1]
        document.assert_not_called()

    def test_pdf_output_uses_safe_filename_and_truncated_caption(self) -> None:
        from sase_telegram.custom_commands import CommandResult
        from sase_telegram.scripts import sase_tg_inbound as inbound

        stdout = (
            "---\n"
            f"caption: {'x' * 1400}\n"
            "filename: ../../daily report.txt\n"
            "---\n\n"
            "# Tasks\n"
        )

        def render(markdown_path: str) -> str:
            pdf_path = Path(markdown_path).with_suffix(".pdf")
            pdf_path.write_bytes(b"%PDF-fake")
            return str(pdf_path)

        with (
            patch.object(inbound.credentials, "get_chat_id", return_value="12345"),
            patch.object(
                inbound,
                "run_custom_command",
                return_value=CommandResult(stdout, "", 0),
            ),
            patch.object(inbound.pdf_convert, "md_to_pdf", side_effect=render),
            patch.object(inbound.telegram_client, "send_message") as send,
            patch.object(inbound.telegram_client, "send_document") as document,
        ):
            inbound._handle_custom_command(_custom_command(output="pdf"), "")

        send.assert_called_once()  # immediate acknowledgement
        document.assert_called_once()
        assert document.call_args.kwargs["filename"] == "daily report.pdf"
        assert document.call_args.kwargs["parse_mode"] == "MarkdownV2"
        caption = document.call_args.kwargs["caption"]
        assert len(caption) <= 1024
        assert caption.endswith("…")
        assert not Path(document.call_args.args[1]).exists()
