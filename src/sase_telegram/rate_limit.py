"""Sliding window rate limiter for Telegram message sending."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

RATE_LIMIT_PATH = Path.home() / ".sase" / "telegram" / "rate_limit.json"
DEFAULT_MAX_MESSAGES = 8
DEFAULT_WINDOW_SECONDS = 15.0


def _get_config() -> tuple[int, float]:
    """Get rate limit config from env var or defaults.

    ``SASE_TELEGRAM_RATE_LIMIT`` format: ``max_messages/window_seconds``
    (e.g. ``"5/10"``).
    """
    env = os.environ.get("SASE_TELEGRAM_RATE_LIMIT")
    if env:
        parts = env.split("/")
        if len(parts) == 2:
            return int(parts[0]), float(parts[1])
    return DEFAULT_MAX_MESSAGES, DEFAULT_WINDOW_SECONDS


def _load_timestamps() -> list[float]:
    """Load send timestamps from disk."""
    if not RATE_LIMIT_PATH.exists():
        return []
    with open(RATE_LIMIT_PATH) as f:
        return json.load(f)


def _save_timestamps(timestamps: list[float]) -> None:
    """Atomically write send timestamps to disk."""
    RATE_LIMIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=RATE_LIMIT_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(timestamps, f)
        os.replace(tmp_path, RATE_LIMIT_PATH)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def check_rate_limit() -> bool:
    """Return True if sending a message is allowed within the rate limit."""
    max_messages, window_seconds = _get_config()
    now = time.time()
    timestamps = _load_timestamps()
    # Keep only timestamps within the window
    recent = [t for t in timestamps if now - t < window_seconds]
    return len(recent) < max_messages


def record_send() -> None:
    """Record a message send timestamp."""
    max_messages, window_seconds = _get_config()
    now = time.time()
    timestamps = _load_timestamps()
    # Prune old timestamps and add the new one
    recent = [t for t in timestamps if now - t < window_seconds]
    recent.append(now)
    _save_timestamps(recent)


def wait_time() -> float:
    """Return seconds to wait before the next send is allowed, or 0.0."""
    _max_messages, window_seconds = _get_config()
    now = time.time()
    timestamps = _load_timestamps()
    recent = sorted(t for t in timestamps if now - t < window_seconds)
    if len(recent) < _max_messages:
        return 0.0
    # Need to wait until the oldest timestamp in the window expires
    return recent[0] + window_seconds - now
