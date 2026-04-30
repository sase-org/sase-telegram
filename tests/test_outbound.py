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
    _is_diff_file,
    _is_image_file,
    _is_pdf_file,
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
        # Dismissed notifications should still be sent — TUI dismissal is a
        # UI cleanup action that happens while active, but the outbound only
        # runs when idle.
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


class TestIsImageFile:
    def test_known_extensions(self):
        assert _is_image_file("/path/to/file.jpg")
        assert _is_image_file("/path/to/file.jpeg")
        assert _is_image_file("/path/to/file.png")
        assert _is_image_file("/path/to/file.webp")
        assert _is_image_file("/path/to/file.gif")

    def test_non_image_extension(self):
        assert not _is_image_file("/path/to/file.md")
        assert not _is_image_file("/path/to/file.diff")


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
            patch("sase_telegram.scripts.sase_tg_outbound.is_idle", return_value=True),
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
            patch("sase_telegram.scripts.sase_tg_outbound.is_idle", return_value=True),
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
                    "sase_telegram.scripts.sase_tg_outbound.is_idle",
                    return_value=True,
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
