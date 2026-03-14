"""Encode and decode inline keyboard callback data strings.

Format: ``{action_type}:{notif_id_prefix}:{choice}``

The encoded string must not exceed 64 bytes (Telegram API limit).
"""

from __future__ import annotations

from typing import NamedTuple

MAX_CALLBACK_BYTES = 64
SEPARATOR = ":"


class CallbackData(NamedTuple):
    """Parsed callback data from an inline keyboard button."""

    action_type: str
    notif_id_prefix: str
    choice: str


def encode(action_type: str, notif_id_prefix: str, choice: str) -> str:
    """Encode callback data fields into a colon-separated string.

    Raises ``ValueError`` if the result exceeds 64 bytes.
    """
    encoded = SEPARATOR.join([action_type, notif_id_prefix, choice])
    if len(encoded.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError(
            f"Callback data exceeds {MAX_CALLBACK_BYTES} bytes: {encoded!r}"
        )
    return encoded


def decode(data: str) -> CallbackData:
    """Decode a colon-separated callback data string.

    Raises ``ValueError`` if the string does not contain exactly 3 parts.
    """
    parts = data.split(SEPARATOR, maxsplit=2)
    if len(parts) != 3:
        raise ValueError(
            f"Expected 3 colon-separated parts, got {len(parts)}: {data!r}"
        )
    return CallbackData(action_type=parts[0], notif_id_prefix=parts[1], choice=parts[2])
