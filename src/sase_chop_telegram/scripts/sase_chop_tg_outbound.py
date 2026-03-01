"""Outbound chop entry point: send sase notifications to Telegram."""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from pathlib import Path

from sase.ace.tui_activity import is_idle
from sase.chat_history import extract_response_from_chat_file
from sase.sase_utils import get_sase_directory
from sase_chop_telegram import pending_actions, rate_limit
from sase_chop_telegram.credentials import get_chat_id
from sase_chop_telegram.formatting import format_notification, format_response_spoiler
from sase_chop_telegram.outbound import get_unsent_notifications, mark_sent
from sase_chop_telegram.pdf_convert import md_to_pdf
from sase_chop_telegram.telegram_client import send_document, send_message

log = logging.getLogger(__name__)

# Actions that should be tracked as pending (user needs to respond)
_ACTIONABLE_ACTIONS = {"PlanApproval", "HITL", "UserQuestion"}

# Lazily resolved path to ~/.sase/chats/
_chats_dir: str | None = None


def _get_chats_dir() -> str:
    """Return the chats directory path, caching on first call."""
    global _chats_dir  # noqa: PLW0603
    if _chats_dir is None:
        _chats_dir = get_sase_directory("chats")
    return _chats_dir


def _is_chat_file(file_path: str) -> bool:
    """Check if a file path points to a chat history file."""
    resolved = str(Path(file_path).expanduser().resolve())
    return resolved.startswith(_get_chats_dir())


def _make_response_only_file(chat_path: str) -> tuple[Path | None, str | None]:
    """Extract just the response from a chat file and write to a temp file.

    Returns (temp_file_path, response_text), or (None, None) if extraction
    fails.
    """
    response = extract_response_from_chat_file(chat_path)
    if not response:
        return None, None
    original_name = Path(chat_path).stem
    tmp = tempfile.NamedTemporaryFile(
        prefix=f"response-{original_name}-",
        suffix=".md",
        delete=False,
        mode="w",
        encoding="utf-8",
    )
    tmp.write(response)
    tmp.close()
    return Path(tmp.name), response


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sase_chop_tg_outbound",
        description="Send sase notifications to Telegram",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent without actually sending",
    )
    parser.add_argument(
        "--context",
        default=None,
        help="Optional context string for lumberjack compatibility",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Outbound Telegram chop entry point."""
    args = _parse_args(argv)

    # Clean up stale pending actions
    pending_actions.cleanup_stale()

    if not is_idle():
        return 0

    notifications = get_unsent_notifications()
    if not notifications:
        return 0

    chat_id = get_chat_id() if not args.dry_run else "DRY_RUN"

    for n in notifications:
        # Check rate limit before sending
        if not args.dry_run and not rate_limit.check_rate_limit():
            wait = rate_limit.wait_time()
            time.sleep(wait)

        text, keyboard, attachments = format_notification(n)

        if args.dry_run:
            print(f"--- Notification {n.id} ---")
            print(f"Text: {text}")
            if keyboard:
                print(f"Keyboard: {keyboard.inline_keyboard}")
            if attachments:
                print(f"Attachments: {attachments}")
                for fp in attachments:
                    if _is_chat_file(fp):
                        _, resp = _make_response_only_file(fp)
                        if resp:
                            print(f"Spoiler: {format_response_spoiler(resp)}")
            print()
            # Advance high-water mark after each notification to prevent
            # re-sending if a later notification fails.
            mark_sent([n])
            continue

        msg = send_message(
            chat_id, text, reply_markup=keyboard, parse_mode="MarkdownV2"
        )
        rate_limit.record_send()

        # Advance high-water mark immediately after the text message is
        # sent so that a failure in attachment processing won't cause the
        # text message to be re-sent on the next tick.
        mark_sent([n])

        pdf_temps: list[Path] = []
        response_temps: list[Path] = []
        for file_path in attachments:
            try:
                # For chat files, extract just the response instead of
                # attaching the entire chat history.
                actual_path = file_path
                if _is_chat_file(file_path):
                    response_file, response_text = _make_response_only_file(
                        file_path
                    )
                    if response_file:
                        response_temps.append(response_file)
                        actual_path = str(response_file)

                    # Send the response as a spoiler message before the PDF
                    if response_text:
                        spoiler_text = format_response_spoiler(response_text)
                        send_message(
                            chat_id,
                            spoiler_text,
                            parse_mode="MarkdownV2",
                        )
                        rate_limit.record_send()

                pdf_path = md_to_pdf(actual_path)
                if pdf_path:
                    pdf_temps.append(Path(pdf_path))
                    send_document(chat_id, pdf_path)
                else:
                    send_document(chat_id, actual_path)
                rate_limit.record_send()
            except Exception:
                log.warning(
                    "Failed to send attachment %s for notification %s",
                    file_path,
                    n.id[:8],
                    exc_info=True,
                )

        for p in pdf_temps + response_temps:
            p.unlink(missing_ok=True)

        # Save pending action for actionable notifications
        if n.action in _ACTIONABLE_ACTIONS:
            pending_actions.add(
                n.id[:8],
                {
                    "notification_id": n.id,
                    "action": n.action,
                    "action_data": n.action_data,
                    "message_id": msg.message_id,
                    "chat_id": chat_id,
                },
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
