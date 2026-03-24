"""Tests for outbound logic."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
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
