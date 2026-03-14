"""Tests for rate limiting logic."""

import time
from pathlib import Path
from unittest.mock import patch

from sase_telegram import rate_limit


class TestRateLimit:
    def setup_method(self) -> None:
        self.tmp_path = Path("/tmp/test_rate_limit.json")
        self.tmp_path.unlink(missing_ok=True)
        self._patcher = patch.object(rate_limit, "RATE_LIMIT_PATH", self.tmp_path)
        self._patcher.start()

    def teardown_method(self) -> None:
        self._patcher.stop()
        self.tmp_path.unlink(missing_ok=True)

    def test_allows_under_limit(self) -> None:
        assert rate_limit.check_rate_limit() is True

    def test_blocks_over_limit(self) -> None:
        # Default is 8 messages per 15 seconds
        for _ in range(8):
            rate_limit.record_send()
        assert rate_limit.check_rate_limit() is False

    def test_wait_time_zero_when_under_limit(self) -> None:
        assert rate_limit.wait_time() == 0.0

    def test_wait_time_positive_when_over_limit(self) -> None:
        for _ in range(8):
            rate_limit.record_send()
        wt = rate_limit.wait_time()
        assert wt > 0.0

    def test_old_timestamps_pruned(self) -> None:
        # Simulate sends from 20 seconds ago (outside default 15s window)
        old_time = time.time() - 20
        rate_limit._save_timestamps([old_time] * 8)
        assert rate_limit.check_rate_limit() is True

    def test_custom_config_via_env(self) -> None:
        with patch.dict("os.environ", {"SASE_TELEGRAM_RATE_LIMIT": "2/5"}):
            rate_limit.record_send()
            rate_limit.record_send()
            assert rate_limit.check_rate_limit() is False
