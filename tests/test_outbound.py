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

    @patch("sase_telegram.outbound.get_tui_last_activity", return_value=None)
    @patch("sase_telegram.outbound.load_notifications")
    def test_filters_correctly(self, mock_load, _mock_activity):
        """Only returns unread, undismissed notifications newer than last sent."""
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
        n_dismissed = _make_notification(
            id="dism0000-0000-0000-0000-000000000000", timestamp=new_ts, dismissed=True
        )
        mock_load.return_value = [n_old, n_new, n_read, n_dismissed]

        result = get_unsent_notifications()
        assert len(result) == 1
        assert result[0].id == "new00000-0000-0000-0000-000000000000"

    @patch("sase_telegram.outbound.get_tui_last_activity")
    @patch("sase_telegram.outbound.load_notifications")
    def test_advances_hwm_to_last_activity_time(self, mock_load, mock_activity):
        """Advance high-water mark to last TUI activity time.

        Notifications received before the last activity should not be
        re-sent, regardless of whether the TUI is still running.
        """
        activity_time = datetime(2025, 6, 1, tzinfo=UTC).timestamp()
        mock_activity.return_value = activity_time

        # High-water mark is older than activity time
        old_hwm = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
        LAST_SENT_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SENT_TEST_FILE.write_text(str(old_hwm))

        # Notification received before last activity — should NOT be returned
        before_ts = datetime(2025, 3, 1, tzinfo=UTC).isoformat()
        # Notification received after last activity — should be returned
        after_ts = datetime(2025, 7, 1, tzinfo=UTC).isoformat()

        n_before = _make_notification(
            id="before00-0000-0000-0000-000000000000", timestamp=before_ts
        )
        n_after = _make_notification(
            id="after000-0000-0000-0000-000000000000", timestamp=after_ts
        )
        mock_load.return_value = [n_before, n_after]

        result = get_unsent_notifications()
        assert len(result) == 1
        assert result[0].id == "after000-0000-0000-0000-000000000000"

        # High-water mark should have been advanced to activity time
        written_hwm = float(LAST_SENT_TEST_FILE.read_text().strip())
        assert written_hwm == pytest.approx(activity_time, abs=1.0)

    @patch("sase_telegram.outbound.get_tui_last_activity", return_value=0)
    @patch("sase_telegram.outbound.load_notifications")
    def test_manual_idle_does_not_advance_hwm(self, mock_load, _mock_activity):
        """epoch=0 (manual idle via I key) should NOT advance the HWM.

        When the user manually marks idle, accumulated notifications should
        still be delivered via Telegram.
        """
        old_hwm = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
        LAST_SENT_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_SENT_TEST_FILE.write_text(str(old_hwm))

        new_ts = datetime(2025, 6, 1, tzinfo=UTC).isoformat()
        n = _make_notification(
            id="notif000-0000-0000-0000-000000000000", timestamp=new_ts
        )
        mock_load.return_value = [n]

        result = get_unsent_notifications()
        assert len(result) == 1

        # HWM should NOT have been advanced
        written_hwm = float(LAST_SENT_TEST_FILE.read_text().strip())
        assert written_hwm == pytest.approx(old_hwm, abs=1.0)


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
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False
        ) as resp:
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
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False
        ) as resp:
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
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False
        ) as resp:
            resp.write("Response content.")

        _append_diff_to_markdown(Path(resp.name), ["/nonexistent/file.diff"])

        result = Path(resp.name).read_text()
        assert result == "Response content."

        Path(resp.name).unlink()
