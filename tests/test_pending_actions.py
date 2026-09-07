"""Tests for pending actions CRUD and cleanup."""

import time
from pathlib import Path
from unittest.mock import patch

from sase.notifications import pending_actions as core_pending_actions
from sase_telegram import pending_actions


class TestPendingActions:
    def setup_method(self) -> None:
        self.tmp_path = Path("/tmp/test_pending_actions.json")
        self.legacy_path = Path("/tmp/test_pending_actions_legacy.json")
        self.tmp_path.unlink(missing_ok=True)
        self.legacy_path.unlink(missing_ok=True)
        self._path_patcher = patch.object(
            pending_actions, "PENDING_ACTIONS_PATH", self.tmp_path
        )
        self._legacy_path_patcher = patch.object(
            core_pending_actions,
            "LEGACY_TELEGRAM_PENDING_ACTIONS_PATH",
            self.legacy_path,
        )
        self._path_patcher.start()
        self._legacy_path_patcher.start()

    def teardown_method(self) -> None:
        self._legacy_path_patcher.stop()
        self._path_patcher.stop()
        self.tmp_path.unlink(missing_ok=True)
        self.legacy_path.unlink(missing_ok=True)

    def test_add_and_get(self) -> None:
        pending_actions.add("action1", {"type": "snooze", "target": "notif-abc"})
        result = pending_actions.get("action1")
        assert result is not None
        assert result["type"] == "snooze"
        assert "created_at" in result

    def test_get_missing(self) -> None:
        assert pending_actions.get("nonexistent") is None

    def test_remove_existing(self) -> None:
        pending_actions.add("action1", {"type": "dismiss"})
        assert pending_actions.remove("action1") is True
        assert pending_actions.get("action1") is None

    def test_remove_missing(self) -> None:
        assert pending_actions.remove("nonexistent") is False

    def test_list_all(self) -> None:
        pending_actions.add("a1", {"type": "snooze"})
        pending_actions.add("a2", {"type": "dismiss"})
        all_actions = pending_actions.list_all()
        assert len(all_actions) == 2
        assert "a1" in all_actions
        assert "a2" in all_actions

    def test_cleanup_stale(self) -> None:
        # Add an action with a timestamp from 25 hours ago
        with patch.object(
            pending_actions.time, "time", return_value=time.time() - 25 * 60 * 60
        ):
            pending_actions.add("old", {"type": "snooze"})
        pending_actions.add("new", {"type": "dismiss"})

        removed = pending_actions.cleanup_stale()
        assert "old" in removed
        assert pending_actions.get("old") is None
        assert pending_actions.get("new") is not None
