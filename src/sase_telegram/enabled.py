"""Machine-level enable flag for the sase-telegram chops."""

from pathlib import Path


def telegram_enabled_path() -> Path:
    """Return the opt-in flag path (computed at call time for testability)."""
    return Path.home() / ".sase" / "telegram_is_enabled"


def is_telegram_enabled() -> bool:
    return telegram_enabled_path().exists()
