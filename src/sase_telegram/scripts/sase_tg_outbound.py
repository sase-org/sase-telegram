"""Outbound chop entry point: send sase notifications to Telegram."""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from sase.ace.tui_activity import is_idle
from sase.chat_history import extract_response_from_chat_file
from sase.sase_utils import EASTERN_TZ, get_sase_directory
from sase_telegram import pending_actions, rate_limit
from sase_telegram.credentials import get_chat_id
from sase_telegram.formatting import format_notification
from sase_telegram.outbound import get_unsent_notifications, mark_sent
from sase_telegram.pdf_convert import md_to_pdf
from sase_telegram.telegram_client import send_document, send_message, send_photo

log = logging.getLogger(__name__)

_DEBUG_LOG = Path.home() / ".sase" / "telegram" / "outbound_debug.log"

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


def _is_diff_file(file_path: str) -> bool:
    """Check if a file path points to a diff file."""
    return Path(file_path).suffix.lower() == ".diff"


def _is_image_file(file_path: str) -> bool:
    """Check if a file path points to a common image format."""
    return Path(file_path).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _append_diff_to_markdown(response_file: Path, diff_paths: list[str]) -> None:
    """Append formatted diff content to a response markdown file.

    Reads each diff file and appends it as a syntax-highlighted code block
    so that the resulting PDF includes a readable diff section.
    """
    diff_sections: list[str] = []
    for dp in diff_paths:
        try:
            content = Path(dp).read_text(encoding="utf-8", errors="replace")
            if content.strip():
                diff_sections.append(content)
        except OSError:
            continue

    if not diff_sections:
        return

    with open(response_file, "a", encoding="utf-8") as f:
        f.write("\n\n---\n\n")
        f.write("## Changes\n\n")
        for section in diff_sections:
            f.write("```diff\n")
            f.write(section)
            if not section.endswith("\n"):
                f.write("\n")
            f.write("```\n")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sase_tg_outbound",
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


def _log_send_diagnostics(notifications: list) -> None:
    """Write diagnostic info to a debug log when notifications are about to be sent."""
    from sase.ace.tui_activity import (
        ACTIVITY_FILE,
        IDLE_STATE_FILE,
        LAST_KEYPRESS_FILE,
        PID_FILE,
    )

    try:
        now = time.time()
        now_str = datetime.fromtimestamp(now, tz=EASTERN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"\n=== SEND @ {now_str} ({now:.3f}) ==="]

        # Read raw state files
        for label, path in [
            ("idle_state", IDLE_STATE_FILE),
            ("pid", PID_FILE),
            ("last_activity", ACTIVITY_FILE),
            ("last_keypress", LAST_KEYPRESS_FILE),
        ]:
            try:
                val = path.read_text().strip()
                if label in ("last_activity", "last_keypress"):
                    age = now - float(val)
                    lines.append(f"  {label}: {val} (age={age:.1f}s)")
                else:
                    lines.append(f"  {label}: {val}")
            except (FileNotFoundError, ValueError) as e:
                lines.append(f"  {label}: <{type(e).__name__}>")

        # Notification details
        lines.append(f"  notifications_count: {len(notifications)}")
        for n in notifications[:5]:  # cap at 5 for brevity
            lines.append(f"    {n.id[:8]} sender={n.sender} ts={n.timestamp} action={n.action}")

        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_DEBUG_LOG, "a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass  # never let diagnostics crash the outbound


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

    _log_send_diagnostics(notifications)

    chat_id = get_chat_id() if not args.dry_run else "DRY_RUN"

    for n in notifications:
        # Re-check idle state before each notification — stop sending
        # if the user became active while we were processing the batch.
        if not args.dry_run and not is_idle():
            break

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

        # Separate diff files — they'll be embedded into the response PDF
        diff_paths = [f for f in attachments if _is_diff_file(f)]
        non_diff_paths = [f for f in attachments if not _is_diff_file(f)]
        diff_embedded = False

        for file_path in non_diff_paths:
            try:
                # For chat files, extract just the response instead of
                # attaching the entire chat history.
                actual_path = file_path
                if _is_chat_file(file_path):
                    response_file, _ = _make_response_only_file(
                        file_path
                    )
                    if response_file:
                        response_temps.append(response_file)
                        actual_path = str(response_file)

                        # Embed diff content into the response markdown
                        if diff_paths:
                            _append_diff_to_markdown(
                                response_file, diff_paths
                            )
                            diff_embedded = True

                if _is_image_file(actual_path):
                    send_photo(chat_id, actual_path)
                    rate_limit.record_send()
                    continue

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

        # Fallback: send diff files separately if not embedded
        if not diff_embedded:
            for dp in diff_paths:
                try:
                    send_document(chat_id, dp)
                    rate_limit.record_send()
                except Exception:
                    log.warning(
                        "Failed to send diff %s for notification %s",
                        dp,
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
