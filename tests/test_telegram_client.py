"""Tests for the Telegram sync wrapper around python-telegram-bot.

The wrapper is exercised without making real network calls by patching
``_get_bot`` to return a mock Bot whose async methods are AsyncMock
instances (because ``_run_async`` runs them through ``asyncio.run``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_mock import MockerFixture
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from sase_telegram import telegram_client


@pytest.fixture(autouse=True)
def _no_sleep(mocker: MockerFixture) -> None:
    """Make retry backoff sleeps instant."""
    mocker.patch.object(telegram_client.time, "sleep", lambda _s: None)


@pytest.fixture
def mock_bot(mocker: MockerFixture) -> MagicMock:
    """Patch ``_get_bot`` to return a fresh MagicMock with AsyncMock methods."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_document = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_animation = AsyncMock()
    bot.send_video = AsyncMock()
    bot.get_updates = AsyncMock(return_value=[])
    bot.answer_callback_query = AsyncMock(return_value=True)
    bot.edit_message_reply_markup = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.set_my_commands = AsyncMock(return_value=True)
    bot.get_file = AsyncMock()
    mocker.patch.object(telegram_client, "_get_bot", return_value=bot)
    return bot


def _markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("ok", callback_data="x")]])


class TestSplitMessage:
    def test_short_message_one_chunk(self) -> None:
        assert telegram_client._split_message("hi") == ["hi"]

    def test_splits_on_newline(self) -> None:
        text = "a" * 4000 + "\n" + "b" * 200
        chunks = telegram_client._split_message(text)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 4000
        assert chunks[1] == "b" * 200

    def test_splits_on_space_when_no_newline(self) -> None:
        text = "x" * 4090 + " " + "y" * 100
        chunks = telegram_client._split_message(text)
        assert len(chunks) == 2
        assert chunks[0] == "x" * 4090
        assert chunks[1].lstrip() == "y" * 100

    def test_hard_split_when_no_break_point(self) -> None:
        text = "z" * 5000
        chunks = telegram_client._split_message(text)
        assert len(chunks) == 2
        assert chunks[0] == "z" * 4096
        assert chunks[1] == "z" * (5000 - 4096)

    def test_respects_custom_limit(self) -> None:
        chunks = telegram_client._split_message("aaaa\nbbbb\ncccc", limit=5)
        assert all(len(c) <= 5 for c in chunks)
        assert "".join(c.strip() for c in chunks) == "aaaabbbbcccc"

    def test_strips_leading_newlines_after_split(self) -> None:
        text = "a" * 4096 + "\nrest"
        chunks = telegram_client._split_message(text)
        assert chunks[1] == "rest"


class TestWithRetry:
    def test_retries_on_retry_after(self) -> None:
        fn = MagicMock(side_effect=[RetryAfter(1), RetryAfter(1), "ok"])
        wrapped = telegram_client._with_retry(fn)
        assert wrapped() == "ok"
        assert fn.call_count == 3

    def test_retries_on_timed_out(self) -> None:
        fn = MagicMock(side_effect=[TimedOut(), "ok"])
        wrapped = telegram_client._with_retry(fn)
        assert wrapped() == "ok"
        assert fn.call_count == 2

    def test_retries_on_network_error(self) -> None:
        fn = MagicMock(side_effect=[NetworkError("boom"), "ok"])
        wrapped = telegram_client._with_retry(fn)
        assert wrapped() == "ok"
        assert fn.call_count == 2

    def test_does_not_retry_on_bad_request(self) -> None:
        fn = MagicMock(side_effect=BadRequest("nope"))
        wrapped = telegram_client._with_retry(fn)
        with pytest.raises(BadRequest):
            wrapped()
        assert fn.call_count == 1

    def test_gives_up_after_max_retries(self) -> None:
        fn = MagicMock(side_effect=TimedOut())
        wrapped = telegram_client._with_retry(fn)
        with pytest.raises(TimedOut):
            wrapped()
        # initial attempt + _MAX_RETRIES
        assert fn.call_count == telegram_client._MAX_RETRIES + 1

    def test_retry_after_propagates_when_max_exceeded(self) -> None:
        fn = MagicMock(side_effect=RetryAfter(1))
        wrapped = telegram_client._with_retry(fn)
        with pytest.raises(RetryAfter):
            wrapped()
        assert fn.call_count == telegram_client._MAX_RETRIES + 1


class TestSendMessage:
    def test_single_chunk_passes_all_kwargs(self, mock_bot: MagicMock) -> None:
        sentinel = MagicMock(message_id=42)
        mock_bot.send_message.return_value = sentinel
        markup = _markup()

        msg = telegram_client.send_message(
            "chat-1",
            "hello",
            reply_markup=markup,
            parse_mode="MarkdownV2",
            reply_to_message_id=99,
        )

        assert msg is sentinel
        mock_bot.send_message.assert_awaited_once_with(
            chat_id="chat-1",
            text="hello",
            reply_markup=markup,
            parse_mode="MarkdownV2",
            reply_to_message_id=99,
        )

    def test_long_message_splits_and_attaches_markup_only_to_last(
        self, mock_bot: MagicMock
    ) -> None:
        last = MagicMock(message_id=2)
        mock_bot.send_message.side_effect = [MagicMock(message_id=1), last]
        markup = _markup()
        text = "a" * 4000 + "\n" + "b" * 200

        msg = telegram_client.send_message("chat-1", text, reply_markup=markup)

        assert msg is last
        assert mock_bot.send_message.await_count == 2
        first_call, second_call = mock_bot.send_message.await_args_list
        assert first_call.kwargs["reply_markup"] is None
        assert second_call.kwargs["reply_markup"] is markup

    def test_parse_mode_fallback_on_failure(self, mock_bot: MagicMock) -> None:
        ok = MagicMock(message_id=7)
        mock_bot.send_message.side_effect = [BadRequest("bad markdown"), ok]

        msg = telegram_client.send_message("chat-1", "hi", parse_mode="MarkdownV2")

        assert msg is ok
        assert mock_bot.send_message.await_count == 2
        # Second call must omit parse_mode
        retry_kwargs = mock_bot.send_message.await_args_list[1].kwargs
        assert "parse_mode" not in retry_kwargs

    def test_no_fallback_when_parse_mode_unset(self, mock_bot: MagicMock) -> None:
        mock_bot.send_message.side_effect = BadRequest("bad chat id")
        with pytest.raises(BadRequest):
            telegram_client.send_message("chat-1", "hi")
        assert mock_bot.send_message.await_count == 1


class TestSendDocument:
    def test_delegates_to_bot(self, mock_bot: MagicMock) -> None:
        mock_bot.send_document.return_value = MagicMock(message_id=1)
        telegram_client.send_document("chat-1", "/tmp/x.pdf", caption="c")
        mock_bot.send_document.assert_awaited_once_with(
            chat_id="chat-1",
            document="/tmp/x.pdf",
            caption="c",
            parse_mode=None,
            filename=None,
        )

    def test_delegates_filename_to_bot(self, mock_bot: MagicMock) -> None:
        mock_bot.send_document.return_value = MagicMock(message_id=1)
        telegram_client.send_document("chat-1", "/tmp/x.pdf", filename="report.pdf")
        mock_bot.send_document.assert_awaited_once_with(
            chat_id="chat-1",
            document="/tmp/x.pdf",
            caption=None,
            parse_mode=None,
            filename="report.pdf",
        )


class TestSendPhoto:
    def test_delegates_to_bot(self, mock_bot: MagicMock) -> None:
        mock_bot.send_photo.return_value = MagicMock(message_id=1)
        telegram_client.send_photo("chat-1", "/tmp/x.png", caption="hi")
        mock_bot.send_photo.assert_awaited_once_with(
            chat_id="chat-1", photo="/tmp/x.png", caption="hi"
        )


class TestSendAnimation:
    def test_delegates_to_bot(self, mock_bot: MagicMock) -> None:
        mock_bot.send_animation.return_value = MagicMock(message_id=1)
        telegram_client.send_animation("chat-1", "/tmp/x.gif", caption="hi")
        mock_bot.send_animation.assert_awaited_once_with(
            chat_id="chat-1", animation="/tmp/x.gif", caption="hi"
        )


class TestSendVideo:
    def test_delegates_to_bot(self, mock_bot: MagicMock) -> None:
        mock_bot.send_video.return_value = MagicMock(message_id=1)
        telegram_client.send_video("chat-1", "/tmp/x.mp4", caption="hi")
        mock_bot.send_video.assert_awaited_once_with(
            chat_id="chat-1", video="/tmp/x.mp4", caption="hi"
        )


class TestGetUpdates:
    def test_delegates_to_bot(self, mock_bot: MagicMock) -> None:
        mock_bot.get_updates.return_value = ["u1", "u2"]
        result = telegram_client.get_updates(offset=5, timeout=10)
        assert result == ["u1", "u2"]
        mock_bot.get_updates.assert_awaited_once_with(offset=5, timeout=10)


class TestAnswerCallbackQuery:
    def test_delegates_to_bot(self, mock_bot: MagicMock) -> None:
        result = telegram_client.answer_callback_query("cbq-1", text="ack")
        assert result is True
        mock_bot.answer_callback_query.assert_awaited_once_with(
            callback_query_id="cbq-1", text="ack"
        )


class TestEditMessageReplyMarkup:
    def test_delegates_to_bot(self, mock_bot: MagicMock) -> None:
        markup = _markup()
        telegram_client.edit_message_reply_markup("chat-1", 42, reply_markup=markup)
        mock_bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id="chat-1", message_id=42, reply_markup=markup
        )

    def test_can_clear_markup(self, mock_bot: MagicMock) -> None:
        telegram_client.edit_message_reply_markup("chat-1", 42)
        kwargs = mock_bot.edit_message_reply_markup.await_args.kwargs
        assert kwargs["reply_markup"] is None


class TestEditMessageText:
    def test_delegates_to_bot(self, mock_bot: MagicMock) -> None:
        markup = _markup()
        telegram_client.edit_message_text(
            "chat-1",
            42,
            "done",
            reply_markup=markup,
            parse_mode="MarkdownV2",
        )

        mock_bot.edit_message_text.assert_awaited_once_with(
            chat_id="chat-1",
            message_id=42,
            text="done",
            reply_markup=markup,
            parse_mode="MarkdownV2",
        )

    def test_parse_mode_fallback_on_failure(self, mock_bot: MagicMock) -> None:
        ok = MagicMock(message_id=7)
        mock_bot.edit_message_text.side_effect = [BadRequest("bad markdown"), ok]

        msg = telegram_client.edit_message_text(
            "chat-1",
            42,
            "done",
            parse_mode="MarkdownV2",
        )

        assert msg is ok
        assert mock_bot.edit_message_text.await_count == 2
        retry_kwargs = mock_bot.edit_message_text.await_args_list[1].kwargs
        assert "parse_mode" not in retry_kwargs


class TestSetMyCommands:
    def test_registers_bot_commands(self, mock_bot: MagicMock) -> None:
        telegram_client.set_my_commands([("list", "List agents"), ("kill", "Kill")])
        mock_bot.set_my_commands.assert_awaited_once()
        sent = mock_bot.set_my_commands.await_args.args[0]
        assert all(isinstance(c, BotCommand) for c in sent)
        assert [(c.command, c.description) for c in sent] == [
            ("list", "List agents"),
            ("kill", "Kill"),
        ]

    def test_empty_list(self, mock_bot: MagicMock) -> None:
        telegram_client.set_my_commands([])
        sent = mock_bot.set_my_commands.await_args.args[0]
        assert sent == []


class TestDownloadFile:
    def test_downloads_to_destination(
        self, mock_bot: MagicMock, tmp_path: Path
    ) -> None:
        dest = tmp_path / "out.bin"
        file_obj = MagicMock()
        file_obj.download_to_drive = AsyncMock(return_value=dest)
        mock_bot.get_file.return_value = file_obj

        result = telegram_client.download_file("file-id-1", dest)

        assert result == dest
        mock_bot.get_file.assert_awaited_once_with("file-id-1")
        file_obj.download_to_drive.assert_awaited_once_with(custom_path=dest)
