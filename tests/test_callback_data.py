"""Tests for callback_data encode/decode."""

import pytest

from sase_telegram.callback_data import CallbackData, decode, encode


class TestEncode:
    def test_basic_encode(self) -> None:
        result = encode("snooze", "abc123", "1h")
        assert result == "snooze:abc123:1h"

    def test_exceeds_64_bytes(self) -> None:
        with pytest.raises(ValueError, match="exceeds 64 bytes"):
            encode("a" * 30, "b" * 30, "c" * 10)

    def test_exactly_64_bytes(self) -> None:
        # 62 chars of content + 2 colons = 64 bytes
        result = encode("a" * 20, "b" * 20, "c" * 22)
        assert len(result.encode("utf-8")) == 64


class TestDecode:
    def test_basic_decode(self) -> None:
        result = decode("snooze:abc123:1h")
        assert result == CallbackData(
            action_type="snooze", notif_id_prefix="abc123", choice="1h"
        )

    def test_invalid_format_too_few_parts(self) -> None:
        with pytest.raises(ValueError, match="Expected 3"):
            decode("only_one")

    def test_choice_with_colons(self) -> None:
        # maxsplit=2 means colons in the choice field are preserved
        result = decode("act:id:choice:with:colons")
        assert result.choice == "choice:with:colons"


class TestRoundtrip:
    def test_encode_decode_roundtrip(self) -> None:
        original = ("dismiss", "notif42", "ok")
        encoded = encode(*original)
        decoded = decode(encoded)
        assert decoded == CallbackData(*original)
