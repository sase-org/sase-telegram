"""Tests for outbound logic."""

from __future__ import annotations

import argparse
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sase.notifications.models import Notification
from sase_telegram.outbound import (
    get_unsent_notifications,
    mark_sent,
)
from sase_telegram.scripts.sase_tg_outbound import (
    _append_diff_to_markdown,
    _is_animation_file,
    _is_diff_file,
    _is_image_file,
    _is_pdf_file,
    _is_video_file,
    _make_response_only_file,
    _prepend_commit_message_to_markdown,
    _run_outbound,
)

LAST_SENT_TEST_FILE = Path("/tmp/test_last_sent_ts")


@pytest.fixture(autouse=True)
def _patch_last_sent_file():
    """Use a temp file for tests."""
    with patch("sase_telegram.outbound.LAST_SENT_FILE", LAST_SENT_TEST_FILE):
        yield
    if LAST_SENT_TEST_FILE.exists():
        LAST_SENT_TEST_FILE.unlink()


def _make_notification(
    id: str = "abcd1234-0000-0000-0000-000000000000",
    timestamp: str | None = None,
    read: bool = False,
    dismissed: bool = False,
    silent: bool = False,
) -> Notification:
    if timestamp is None:
        timestamp = datetime.now(UTC).isoformat()
    return Notification(
        id=id,
        timestamp=timestamp,
        sender="test",
        notes=["test note"],
        read=read,
        dismissed=dismissed,
        silent=silent,
    )


class TestGetUnsentNotifications:
    @patch("sase_telegram.outbound.load_notifications")
    def test_no_file_returns_empty_and_initializes(self, mock_load):
        """First run: no last_sent file, returns empty and creates file."""
        assert not LAST_SENT_TEST_FILE.exists()
        result = get_unsent_notifications()
        assert result == []
        assert LAST_SENT_TEST_FILE.exists()
        mock_load.assert_not_called()

    @patch("sase_telegram.outbound.load_notifications")
    def test_filters_correctly(self, mock_load):
        """Returns unread notifications newer than last sent (including dismissed)."""
        old_ts = datetime(2024, 1, 1, tzinfo=UTC).isoformat()
        new_ts = datetime(2025, 6, 1, tzinfo=UTC).isoformat()

        # Set last_sent to midpoint
        midpoint = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
        LAST_SENT_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SENT_TEST_FILE.write_text(str(midpoint))

        n_old = _make_notification(
            id="old00000-0000-0000-0000-000000000000", timestamp=old_ts
        )
        n_new = _make_notification(
            id="new00000-0000-0000-0000-000000000000", timestamp=new_ts
        )
        n_read = _make_notification(
            id="read0000-0000-0000-0000-000000000000", timestamp=new_ts, read=True
        )
        # Dismissed notifications should still be sent. TUI dismissal is a UI
        # cleanup action, not a notification-read signal.
        n_dismissed = _make_notification(
            id="dism0000-0000-0000-0000-000000000000", timestamp=new_ts, dismissed=True
        )
        mock_load.return_value = [n_old, n_new, n_read, n_dismissed]

        result = get_unsent_notifications()
        assert len(result) == 2
        result_ids = {r.id for r in result}
        assert "new00000-0000-0000-0000-000000000000" in result_ids
        assert "dism0000-0000-0000-0000-000000000000" in result_ids

    @patch("sase_telegram.outbound.load_notifications")
    def test_filters_silent_notifications(self, mock_load):
        """Silent notifications are excluded even if unread and new."""
        new_ts = datetime(2025, 6, 1, tzinfo=UTC).isoformat()

        midpoint = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
        LAST_SENT_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SENT_TEST_FILE.write_text(str(midpoint))

        n_normal = _make_notification(
            id="norm0000-0000-0000-0000-000000000000", timestamp=new_ts
        )
        n_silent = _make_notification(
            id="sil00000-0000-0000-0000-000000000000",
            timestamp=new_ts,
            silent=True,
        )
        mock_load.return_value = [n_normal, n_silent]

        result = get_unsent_notifications()
        assert len(result) == 1
        assert result[0].id == "norm0000-0000-0000-0000-000000000000"


class TestMarkSent:
    def test_writes_timestamp(self):
        """Verify high-water mark is written to latest notification timestamp."""
        LAST_SENT_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts1 = datetime(2025, 6, 1, tzinfo=UTC).isoformat()
        ts2 = datetime(2025, 7, 1, tzinfo=UTC).isoformat()
        n1 = _make_notification(
            id="n1000000-0000-0000-0000-000000000000", timestamp=ts1
        )
        n2 = _make_notification(
            id="n2000000-0000-0000-0000-000000000000", timestamp=ts2
        )

        mark_sent([n1, n2])

        written = float(LAST_SENT_TEST_FILE.read_text().strip())
        expected = datetime.fromisoformat(ts2).timestamp()
        assert written == pytest.approx(expected, abs=1.0)

    def test_empty_list_noop(self):
        """mark_sent with empty list doesn't create the file."""
        mark_sent([])
        assert not LAST_SENT_TEST_FILE.exists()


class TestIsDiffFile:
    def test_diff_extension(self):
        assert _is_diff_file("/path/to/changes.diff")

    def test_non_diff_extension(self):
        assert not _is_diff_file("/path/to/file.md")
        assert not _is_diff_file("/path/to/file.pdf")

    def test_case_insensitive(self):
        assert _is_diff_file("/path/to/file.DIFF")


class TestDisplayFilenames:
    def test_make_response_only_file_humanizes_chat_stem(self) -> None:
        with (
            patch(
                "sase_telegram.scripts.sase_tg_outbound.extract_response_from_chat_file",
                return_value="Done.",
            ),
            patch(
                "sase_telegram.scripts.sase_tg_outbound.display_safe_stem",
                return_value="sase-ace_run-260707_011513",
            ) as display_safe_stem,
        ):
            response_file, response = _make_response_only_file(
                "/tmp/chats/gh_sase_org__sase-ace_run-260707_011513.md"
            )

        assert response == "Done."
        assert response_file is not None
        try:
            assert response_file.name.startswith("response-sase-ace_run-260707_011513-")
            display_safe_stem.assert_called_once_with(
                "gh_sase_org__sase-ace_run-260707_011513"
            )
        finally:
            response_file.unlink(missing_ok=True)


class TestIsImageFile:
    def test_known_extensions(self):
        assert _is_image_file("/path/to/file.jpg")
        assert _is_image_file("/path/to/file.jpeg")
        assert _is_image_file("/path/to/file.png")
        assert _is_image_file("/path/to/file.webp")

    def test_non_image_extension(self):
        assert not _is_image_file("/path/to/file.gif")
        assert not _is_image_file("/path/to/file.md")
        assert not _is_image_file("/path/to/file.diff")


class TestIsAnimationFile:
    def test_gif_extension(self):
        assert _is_animation_file("/path/to/file.gif")
        assert _is_animation_file("/path/to/file.GIF")

    def test_non_animation_extension(self):
        assert not _is_animation_file("/path/to/file.png")
        assert not _is_animation_file("/path/to/file.mp4")


class TestIsVideoFile:
    def test_known_extensions(self):
        assert _is_video_file("/path/to/file.mp4")
        assert _is_video_file("/path/to/file.m4v")
        assert _is_video_file("/path/to/file.mov")
        assert _is_video_file("/path/to/file.webm")

    def test_case_insensitive(self):
        assert _is_video_file("/path/to/file.MP4")

    def test_non_video_extension(self):
        assert not _is_video_file("/path/to/file.gif")
        assert not _is_video_file("/path/to/file.pdf")


class TestIsPdfFile:
    def test_pdf_extension(self):
        assert _is_pdf_file("/path/to/report.pdf")

    def test_case_insensitive(self):
        assert _is_pdf_file("/path/to/report.PDF")

    def test_non_pdf_extension(self):
        assert not _is_pdf_file("/path/to/report.md")


class TestAppendDiffToMarkdown:
    def test_appends_diff_content(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as resp:
            resp.write("# Response\n\nSome content.")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".diff", delete=False
        ) as diff:
            diff.write("diff --git a/foo.py b/foo.py\n-old\n+new\n")

        _append_diff_to_markdown(Path(resp.name), [diff.name])

        result = Path(resp.name).read_text()
        assert "## Changes" in result
        assert "```diff" in result
        assert "-old" in result
        assert "+new" in result

        Path(resp.name).unlink()
        Path(diff.name).unlink()

    def test_skips_empty_diff(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as resp:
            resp.write("Response content.")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".diff", delete=False
        ) as diff:
            diff.write("")

        _append_diff_to_markdown(Path(resp.name), [diff.name])

        result = Path(resp.name).read_text()
        assert "Diff" not in result

        Path(resp.name).unlink()
        Path(diff.name).unlink()

    def test_skips_nonexistent_diff(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as resp:
            resp.write("Response content.")

        _append_diff_to_markdown(Path(resp.name), ["/nonexistent/file.diff"])

        result = Path(resp.name).read_text()
        assert result == "Response content."

        Path(resp.name).unlink()


class TestPrependCommitMessageToMarkdown:
    def test_appends_full_multiline_message(self, tmp_path: Path):
        response = tmp_path / "response.md"
        response.write_text("# Response\n\nDone.", encoding="utf-8")
        commit_message = "feat: add report\n\nBody line one.\n\nBody line two.\n"

        _prepend_commit_message_to_markdown(response, commit_message)

        result = response.read_text(encoding="utf-8")
        assert "## Commit Message" in result
        assert "```text\n" in result
        assert commit_message in result

    def test_uses_fence_longer_than_commit_message_backticks(self, tmp_path: Path):
        response = tmp_path / "response.md"
        response.write_text("# Response\n\nDone.", encoding="utf-8")
        commit_message = "feat: add fenced body\n\n```python\nprint('x')\n```\n````"

        _prepend_commit_message_to_markdown(response, commit_message)

        result = response.read_text(encoding="utf-8")
        lines = result.splitlines()
        assert "`````text" in lines
        assert lines[-1] == "`````"
        assert commit_message in result


class TestRunOutboundAttachments:
    def test_workflow_complete_pdf_sends_document_without_conversion(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as pdf:
            pdf.write(b"%PDF-1.4\n")
            pdf_path = pdf.name

        notification = Notification(
            id="pdf00000-0000-0000-0000-000000000000",
            timestamp=datetime.now(UTC).isoformat(),
            sender="user-agent",
            notes=["Agent completed: pdf-update"],
            files=[pdf_path],
        )

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
            patch("sase_telegram.scripts.sase_tg_outbound.mark_sent"),
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_message",
                return_value=SimpleNamespace(message_id=123),
            ),
            patch("sase_telegram.scripts.sase_tg_outbound.send_photo") as send_photo,
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_document"
            ) as send_document,
            patch("sase_telegram.scripts.sase_tg_outbound.md_to_pdf") as md_to_pdf,
        ):
            result = _run_outbound(argparse.Namespace(dry_run=False))

        assert result == 0
        send_document.assert_called_once_with("chat-1", pdf_path)
        send_photo.assert_not_called()
        md_to_pdf.assert_not_called()

        Path(pdf_path).unlink()

    @pytest.mark.parametrize(
        "original_plan_file",
        [None, "", "   ", "/", ".", "bad\x00plan.md"],
        ids=["missing", "empty", "blank", "root", "dot", "control-character"],
    )
    def test_plan_pdf_with_unusable_original_name_uses_generated_filename(
        self,
        tmp_path: Path,
        original_plan_file: str | None,
    ) -> None:
        plan_path = tmp_path / "plan.md"
        plan_path.write_text("# Plan\n", encoding="utf-8")
        pdf_path = tmp_path / "plan.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n")
        action_data = {}
        if original_plan_file is not None:
            action_data["original_plan_file"] = original_plan_file
        notification = Notification(
            id="plan0000-0000-0000-0000-000000000000",
            timestamp=datetime.now(UTC).isoformat(),
            sender="plan",
            notes=["Plan ready for review"],
            files=[str(plan_path)],
            action="PlanApproval",
            action_data=action_data,
        )

        with (
            patch(
                "sase_telegram.scripts.sase_tg_outbound.get_unsent_notifications",
                return_value=[notification],
            ),
            patch(
                "sase_telegram.scripts.sase_tg_outbound.format_notification",
                return_value=("Plan review", None, [str(plan_path)]),
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
            patch("sase_telegram.scripts.sase_tg_outbound.mark_sent"),
            patch("sase_telegram.scripts.sase_tg_outbound.pending_actions.add"),
            patch("sase_telegram.scripts.sase_tg_outbound._register_shared_transport"),
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_message",
                return_value=SimpleNamespace(message_id=123),
            ),
            patch(
                "sase_telegram.scripts.sase_tg_outbound.md_to_pdf",
                return_value=str(pdf_path),
            ) as md_to_pdf,
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_document"
            ) as send_document,
        ):
            result = _run_outbound(argparse.Namespace(dry_run=False))

        assert result == 0
        md_to_pdf.assert_called_once_with(str(plan_path))
        send_document.assert_called_once_with("chat-1", str(pdf_path))

    def test_workflow_complete_image_sends_photo_not_document(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image:
            image.write(b"\x89PNG\r\n\x1a\n")
            image_path = image.name

        notification = Notification(
            id="img00000-0000-0000-0000-000000000000",
            timestamp=datetime.now(UTC).isoformat(),
            sender="user-agent",
            notes=["Agent completed: image-update"],
            files=[image_path],
        )

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
            patch("sase_telegram.scripts.sase_tg_outbound.mark_sent"),
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_message",
                return_value=SimpleNamespace(message_id=123),
            ),
            patch("sase_telegram.scripts.sase_tg_outbound.send_photo") as send_photo,
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_document"
            ) as send_document,
            patch("sase_telegram.scripts.sase_tg_outbound.md_to_pdf") as md_to_pdf,
        ):
            result = _run_outbound(argparse.Namespace(dry_run=False))

        assert result == 0
        send_photo.assert_called_once_with("chat-1", image_path)
        send_document.assert_not_called()
        md_to_pdf.assert_not_called()

        Path(image_path).unlink()

    def test_workflow_complete_gif_sends_animation_not_photo(self):
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as gif:
            gif.write(b"GIF89a")
            gif_path = gif.name

        notification = Notification(
            id="gif00000-0000-0000-0000-000000000000",
            timestamp=datetime.now(UTC).isoformat(),
            sender="user-agent",
            notes=["Agent completed: gif-update"],
            files=[gif_path],
        )

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
            patch("sase_telegram.scripts.sase_tg_outbound.mark_sent"),
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_message",
                return_value=SimpleNamespace(message_id=123),
            ),
            patch("sase_telegram.scripts.sase_tg_outbound.send_photo") as send_photo,
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_animation"
            ) as send_animation,
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_document"
            ) as send_document,
            patch("sase_telegram.scripts.sase_tg_outbound.md_to_pdf") as md_to_pdf,
        ):
            result = _run_outbound(argparse.Namespace(dry_run=False))

        assert result == 0
        send_animation.assert_called_once_with("chat-1", gif_path)
        send_photo.assert_not_called()
        send_document.assert_not_called()
        md_to_pdf.assert_not_called()

        Path(gif_path).unlink()

    def test_workflow_complete_sends_one_animation_for_each_media_pair(
        self, tmp_path: Path
    ):
        animation_paths = [tmp_path / f"demo-{index}.gif" for index in range(5)]
        video_paths = [tmp_path / f"demo-{index}.mp4" for index in range(5)]
        for path in animation_paths + video_paths:
            path.touch()

        notification = Notification(
            id="pair0000-0000-0000-0000-000000000000",
            timestamp=datetime.now(UTC).isoformat(),
            sender="user-agent",
            notes=["Agent completed: media-pairs"],
            files=[str(p) for p in animation_paths + video_paths],
        )

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
            patch("sase_telegram.scripts.sase_tg_outbound.mark_sent"),
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_message",
                return_value=SimpleNamespace(message_id=123),
            ),
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_animation"
            ) as send_animation,
            patch("sase_telegram.scripts.sase_tg_outbound.send_video") as send_video,
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_document"
            ) as send_document,
            patch("sase_telegram.scripts.sase_tg_outbound.md_to_pdf") as md_to_pdf,
        ):
            result = _run_outbound(argparse.Namespace(dry_run=False))

        assert result == 0
        assert [call.args for call in send_animation.call_args_list] == [
            ("chat-1", str(path)) for path in animation_paths
        ]
        send_video.assert_not_called()
        send_document.assert_not_called()
        md_to_pdf.assert_not_called()

    def test_workflow_complete_video_sends_video_not_document(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as video:
            video.write(b"\x00\x00\x00\x18ftypmp42")
            video_path = video.name

        notification = Notification(
            id="vid00000-0000-0000-0000-000000000000",
            timestamp=datetime.now(UTC).isoformat(),
            sender="user-agent",
            notes=["Agent completed: video-update"],
            files=[video_path],
        )

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
            patch("sase_telegram.scripts.sase_tg_outbound.mark_sent"),
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_message",
                return_value=SimpleNamespace(message_id=123),
            ),
            patch("sase_telegram.scripts.sase_tg_outbound.send_video") as send_video,
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_document"
            ) as send_document,
            patch("sase_telegram.scripts.sase_tg_outbound.md_to_pdf") as md_to_pdf,
        ):
            result = _run_outbound(argparse.Namespace(dry_run=False))

        assert result == 0
        send_video.assert_called_once_with("chat-1", video_path)
        send_document.assert_not_called()
        md_to_pdf.assert_not_called()

        Path(video_path).unlink()

    def test_selected_media_failure_falls_back_to_document(self, tmp_path: Path):
        video_path = tmp_path / "fallback-update.webm"
        animation_path = tmp_path / "fallback-update.gif"
        video_path.touch()
        animation_path.touch()

        notification = Notification(
            id="fbk00000-0000-0000-0000-000000000000",
            timestamp=datetime.now(UTC).isoformat(),
            sender="user-agent",
            notes=["Agent completed: fallback-update"],
            files=[str(video_path), str(animation_path)],
        )

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
            patch("sase_telegram.scripts.sase_tg_outbound.mark_sent"),
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_message",
                return_value=SimpleNamespace(message_id=123),
            ),
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_animation",
                side_effect=RuntimeError("unsupported codec"),
            ) as send_animation,
            patch("sase_telegram.scripts.sase_tg_outbound.send_video") as send_video,
            patch(
                "sase_telegram.scripts.sase_tg_outbound.send_document"
            ) as send_document,
            patch("sase_telegram.scripts.sase_tg_outbound.md_to_pdf") as md_to_pdf,
        ):
            result = _run_outbound(argparse.Namespace(dry_run=False))

        assert result == 0
        send_animation.assert_called_once_with("chat-1", str(animation_path))
        send_video.assert_not_called()
        send_document.assert_called_once_with("chat-1", str(animation_path))
        md_to_pdf.assert_not_called()

    def test_mixed_chat_diff_pdf_and_image_sends_expected_attachments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            chat_dir = tmp / "chats"
            chat_dir.mkdir()
            chat_file = chat_dir / "chat.md"
            chat_file.write_text("full chat", encoding="utf-8")
            response_file = tmp / "response.md"
            response_file.write_text("# Response\n\nDone.", encoding="utf-8")
            response_pdf = tmp / "response.pdf"
            response_pdf.write_bytes(b"%PDF-1.4\n")
            diff_file = tmp / "changes.diff"
            diff_file.write_text("diff --git a/foo.py b/foo.py\n-old\n+new\n")
            generated_pdf = tmp / "notes.pdf"
            generated_pdf.write_bytes(b"%PDF-1.4\n")
            image_file = tmp / "screenshot.jpg"
            image_file.write_bytes(b"\xff\xd8\xff")

            notification = Notification(
                id="mix00000-0000-0000-0000-000000000000",
                timestamp=datetime.now(UTC).isoformat(),
                sender="user-agent",
                notes=["Agent completed: mixed-update"],
                files=[
                    str(chat_file),
                    str(diff_file),
                    str(generated_pdf),
                    str(image_file),
                ],
            )

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
                patch("sase_telegram.scripts.sase_tg_outbound.mark_sent"),
                patch(
                    "sase_telegram.scripts.sase_tg_outbound.send_message",
                    return_value=SimpleNamespace(message_id=123),
                ),
                patch(
                    "sase_telegram.scripts.sase_tg_outbound._make_response_only_file",
                    return_value=(response_file, "Done."),
                ),
                patch(
                    "sase_telegram.scripts.sase_tg_outbound._get_chats_dir",
                    return_value=str(chat_dir),
                ),
                patch(
                    "sase_telegram.scripts.sase_tg_outbound.md_to_pdf",
                    return_value=str(response_pdf),
                ) as md_to_pdf,
                patch(
                    "sase_telegram.scripts.sase_tg_outbound.send_photo"
                ) as send_photo,
                patch(
                    "sase_telegram.scripts.sase_tg_outbound.send_document"
                ) as send_document,
            ):
                result = _run_outbound(argparse.Namespace(dry_run=False))

            assert result == 0
            md_to_pdf.assert_called_once_with(str(response_file))
            send_document.assert_any_call("chat-1", str(response_pdf))
            send_document.assert_any_call("chat-1", str(generated_pdf))
            assert send_document.call_count == 2
            send_photo.assert_called_once_with("chat-1", str(image_file))

    def test_unembedded_diff_uses_humanized_document_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            diff_file = tmp / "gh_sase_org__sase_fix-260707.diff"
            diff_file.write_text("diff --git a/foo.py b/foo.py\n-old\n+new\n")

            notification = Notification(
                id="dif00000-0000-0000-0000-000000000000",
                timestamp=datetime.now(UTC).isoformat(),
                sender="user-agent",
                notes=["Agent completed: diff-update"],
                files=[str(diff_file)],
            )

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
                patch("sase_telegram.scripts.sase_tg_outbound.mark_sent"),
                patch(
                    "sase_telegram.scripts.sase_tg_outbound.send_message",
                    return_value=SimpleNamespace(message_id=123),
                ),
                patch(
                    "sase_telegram.scripts.sase_tg_outbound.display_safe_stem",
                    side_effect=lambda stem: (
                        "sase_fix-260707"
                        if stem == "gh_sase_org__sase_fix-260707"
                        else stem
                    ),
                ),
                patch(
                    "sase_telegram.scripts.sase_tg_outbound.send_document"
                ) as send_document,
            ):
                result = _run_outbound(argparse.Namespace(dry_run=False))

            assert result == 0
            send_document.assert_called_once_with(
                "chat-1",
                str(diff_file),
                filename="sase_fix-260707.diff",
            )

    def test_chat_pdf_embeds_full_commit_message_before_conversion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            chat_dir = tmp / "chats"
            chat_dir.mkdir()
            chat_file = chat_dir / "chat.md"
            chat_file.write_text("full chat", encoding="utf-8")
            response_file = tmp / "response.md"
            response_file.write_text("# Response\n\nDone.", encoding="utf-8")
            response_pdf = tmp / "response.pdf"
            full_message = "feat: add report\n\nInclude all body lines.\n\nMore detail."
            captured_markdown: list[str] = []

            notification = Notification(
                id="msg00000-0000-0000-0000-000000000000",
                timestamp=datetime.now(UTC).isoformat(),
                sender="user-agent",
                notes=["Agent completed: commit-update"],
                files=[str(chat_file)],
                action="JumpToAgent",
                action_data={"commit_message": full_message},
            )

            def fake_md_to_pdf(path: str) -> str:
                captured_markdown.append(Path(path).read_text(encoding="utf-8"))
                response_pdf.write_bytes(b"%PDF-1.4\n")
                return str(response_pdf)

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
                patch("sase_telegram.scripts.sase_tg_outbound.mark_sent"),
                patch(
                    "sase_telegram.scripts.sase_tg_outbound.send_message",
                    return_value=SimpleNamespace(message_id=123),
                ),
                patch(
                    "sase_telegram.scripts.sase_tg_outbound._make_response_only_file",
                    return_value=(response_file, "Done."),
                ),
                patch(
                    "sase_telegram.scripts.sase_tg_outbound._get_chats_dir",
                    return_value=str(chat_dir),
                ),
                patch(
                    "sase_telegram.scripts.sase_tg_outbound.md_to_pdf",
                    side_effect=fake_md_to_pdf,
                ),
                patch("sase_telegram.scripts.sase_tg_outbound.send_photo"),
                patch(
                    "sase_telegram.scripts.sase_tg_outbound.send_document"
                ) as send_document,
            ):
                result = _run_outbound(argparse.Namespace(dry_run=False))

            assert result == 0
            assert len(captured_markdown) == 1
            assert "## Commit Message" in captured_markdown[0]
            assert full_message in captured_markdown[0]
            send_document.assert_called_once_with("chat-1", str(response_pdf))

    def test_dry_run_lists_pdf_attachment_without_research_section(self, capsys):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            diff_file = tmp / "changes.diff"
            diff_file.write_text(
                "\n".join(
                    [
                        "diff --git a/research/example.md b/research/example.md",
                        "new file mode 100644",
                        "index 0000000..1111111",
                        "--- /dev/null",
                        "+++ b/research/example.md",
                        "@@ -0,0 +1 @@",
                        "+# Research",
                    ]
                ),
                encoding="utf-8",
            )
            generated_pdf = tmp / "example.pdf"
            generated_pdf.write_bytes(b"%PDF-1.4\n")

            notification = Notification(
                id="dry00000-0000-0000-0000-000000000000",
                timestamp=datetime.now(UTC).isoformat(),
                sender="user-agent",
                notes=["Agent completed: research-update"],
                files=[str(diff_file), str(generated_pdf)],
            )

            with (
                patch(
                    "sase_telegram.scripts.sase_tg_outbound.get_unsent_notifications",
                    return_value=[notification],
                ),
                patch("sase_telegram.scripts.sase_tg_outbound.mark_sent"),
            ):
                result = _run_outbound(argparse.Namespace(dry_run=True))

        assert result == 0
        captured = capsys.readouterr()
        assert str(generated_pdf) in captured.out
        assert "Research files:" not in captured.out
