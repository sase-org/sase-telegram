"""Outbound chop entry point: send sase notifications to Telegram."""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sase_telegram import pending_actions, rate_limit
from sase_telegram.credentials import get_chat_id
from sase_telegram.formatting import format_notification
from sase_telegram.outbound import get_unsent_notifications, mark_sent
from sase_telegram.telegram_client import send_document, send_message, send_photo

log = logging.getLogger(__name__)

_DEBUG_LOG = Path.home() / ".sase" / "telegram" / "outbound_debug.log"

# Actions that should be tracked as pending (user needs to respond)
_ACTIONABLE_ACTIONS = {"PlanApproval", "HITL", "LaunchApproval", "UserQuestion"}
_SUMMARY_ID_LIMIT = 5

# Lazily resolved path to ~/.sase/chats/
_chats_dir: str | None = None


def _register_shared_transport(n: Any, message_id: int, chat_id: str) -> None:
    """Record this Telegram message in the shared host pending-action store.

    Lets cross-surface cleanup (e.g. auto-approved plans whose keyboard
    outlived the resolution) find and dismiss the inline keyboard. The legacy
    Telegram pending-action file is still written, so a failure here must not
    break the callback path — it is logged and swallowed.
    """
    try:
        from sase.notifications.pending_actions import merge_transport_record

        merge_transport_record(
            n.id,
            "telegram",
            {"chat_id": chat_id, "message_id": message_id},
        )
    except Exception:
        log.warning(
            "Failed to register Telegram transport for notification %s",
            n.id[:8],
            exc_info=True,
        )


def get_sase_directory(name: str) -> str:
    from sase.core.paths import get_sase_directory

    return get_sase_directory(name)


def get_timezone() -> Any:
    from sase.core.time import get_timezone

    return get_timezone()


def extract_response_from_chat_file(chat_path: str) -> str | None:
    from sase.history.chat import extract_response_from_chat_file

    return extract_response_from_chat_file(chat_path)


def md_to_pdf(path: str) -> str | None:
    from sase_telegram.pdf_convert import md_to_pdf

    return md_to_pdf(path)


def _get_chats_dir() -> str:
    """Return the chats directory path, caching on first call."""
    global _chats_dir  # noqa: PLW0603
    if _chats_dir is None:
        _chats_dir = get_sase_directory("chats")
    return _chats_dir


def _is_chat_file(file_path: str) -> bool:
    """Check if a file path points to a chat history file."""
    try:
        resolved = Path(file_path).expanduser().resolve()
        chats_dir = Path(_get_chats_dir()).expanduser().resolve()
        resolved.relative_to(chats_dir)
    except (OSError, ValueError):
        return False
    return True


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


def _is_pdf_file(file_path: str) -> bool:
    """Check if a file path points to a PDF."""
    return Path(file_path).suffix.lower() == ".pdf"


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


def _markdown_fence_for_content(content: str) -> str:
    longest_run = 0
    current_run = 0
    for char in content:
        if char == "`":
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0
    return "`" * max(3, longest_run + 1)


def _prepend_commit_message_to_markdown(
    response_file: Path, commit_message: str
) -> None:
    """Append a commit message section to a response markdown file.

    Called before _append_diff_to_markdown() so the commit message
    appears above the diff in the resulting PDF.
    """
    with open(response_file, "a", encoding="utf-8") as f:
        fence = _markdown_fence_for_content(commit_message)
        f.write("\n\n---\n\n")
        f.write("## Commit Message\n\n")
        f.write(f"{fence}text\n")
        f.write(commit_message)
        if not commit_message.endswith("\n"):
            f.write("\n")
        f.write(f"{fence}\n")


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
    try:
        now = time.time()
        now_str = datetime.fromtimestamp(now, tz=get_timezone()).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        lines = [f"\n=== SEND @ {now_str} ({now:.3f}) ==="]

        # Notification details
        lines.append(f"  notifications_count: {len(notifications)}")
        for n in notifications[:5]:  # cap at 5 for brevity
            lines.append(
                f"    {n.id[:8]} sender={n.sender} ts={n.timestamp} action={n.action}"
            )

        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_DEBUG_LOG, "a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass  # never let diagnostics crash the outbound


def _short_notification_ids(notifications: list, limit: int = _SUMMARY_ID_LIMIT) -> str:
    ids = [getattr(n, "id", "")[:8] for n in notifications[:limit]]
    ids = [i for i in ids if i]
    if len(notifications) > limit:
        ids.append(f"+{len(notifications) - limit}")
    return ",".join(ids) if ids else "-"


def _print_outbound_summary(
    *,
    reason: str | None,
    pending_actions_cleaned: int,
    unsent_count: int = 0,
    sent_count: int = 0,
    dry_run_count: int = 0,
    failed_count: int = 0,
    pending_action_writes: int = 0,
    attachment_failures: int = 0,
    high_water_updates: int = 0,
    notification_ids: str = "-",
) -> None:
    parts = [
        "tg_outbound:",
        f"unsent={unsent_count}",
        f"sent={sent_count}",
        f"dry_run={dry_run_count}",
        f"failed={failed_count}",
        f"pending_action_writes={pending_action_writes}",
        f"attachment_failures={attachment_failures}",
        f"high_water_updates={high_water_updates}",
        f"pending_actions_cleaned={pending_actions_cleaned}",
        f"ids={notification_ids}",
    ]
    if reason:
        parts.append(f"reason={reason}")
    print(" ".join(parts))


def main(argv: list[str] | None = None) -> int:
    """Outbound Telegram chop entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Suppress noisy httpx request logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = _parse_args(argv)

    # Clean up stale pending actions
    stale_pending = pending_actions.cleanup_stale()

    # Acquire exclusive lock to prevent concurrent outbound runs from
    # sending the same notification multiple times.  Lumberjack fires
    # this chop every few seconds; if a run takes longer than the
    # interval (retries, rate-limit sleeps, PDF conversion), overlapping
    # runs would read the same high-water mark and duplicate sends.
    from sase_telegram.outbound import release_outbound_lock, try_acquire_outbound_lock

    lock_fd = try_acquire_outbound_lock()
    if lock_fd is None:
        _print_outbound_summary(
            reason="lock_held",
            pending_actions_cleaned=len(stale_pending),
        )
        return 0  # Another instance is running

    try:
        return _run_outbound(args, pending_actions_cleaned=len(stale_pending))
    finally:
        release_outbound_lock(lock_fd)


def _run_outbound(args: argparse.Namespace, *, pending_actions_cleaned: int = 0) -> int:
    """Core outbound logic, called while holding the exclusive lock."""
    notifications = get_unsent_notifications()
    if not notifications:
        _print_outbound_summary(
            reason="no_unsent_notifications",
            pending_actions_cleaned=pending_actions_cleaned,
        )
        return 0

    log.info("Sending %d notification(s)", len(notifications))
    _log_send_diagnostics(notifications)

    chat_id = get_chat_id() if not args.dry_run else "DRY_RUN"
    sent_count = 0
    dry_run_count = 0
    failed_count = 0
    pending_action_writes = 0
    attachment_failures = 0
    high_water_updates = 0

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
            print()
            # Advance high-water mark after each notification to prevent
            # re-sending if a later notification fails.
            mark_sent([n])
            high_water_updates += 1
            dry_run_count += 1
            continue

        msg = None
        try:
            msg = send_message(
                chat_id, text, reply_markup=keyboard, parse_mode="MarkdownV2"
            )
            rate_limit.record_send()
            log.debug("Sent notification %s → message_id=%s", n.id[:8], msg.message_id)
            mark_sent([n])
            high_water_updates += 1
            sent_count += 1
        except Exception:
            failed_count += 1
            log.warning(
                "Failed to send notification %s to Telegram",
                n.id[:8],
                exc_info=True,
            )

        if msg is None:
            continue

        # Save pending action IMMEDIATELY after send so the inbound
        # chop can find it when the user taps a button.  Previously
        # this was deferred until after attachment processing, creating
        # a race window where fast button presses arrived before the
        # pending action was persisted — silently losing the callback.
        if n.action in _ACTIONABLE_ACTIONS:
            entry: dict[str, object] = {
                "notification_id": n.id,
                "action": n.action,
                "action_data": n.action_data,
                "message_id": msg.message_id,
                "chat_id": chat_id,
            }
            if n.action == "PlanApproval" and n.files:
                entry["plan_file"] = n.files[0]
            if n.action == "LaunchApproval" and n.files:
                entry["files"] = list(n.files)
                entry["preview_file"] = n.files[0]
            pending_actions.add(n.id[:8], entry)
            pending_action_writes += 1
            _register_shared_transport(n, msg.message_id, chat_id)

        pdf_temps: list[Path] = []
        response_temps: list[Path] = []

        diff_paths = [f for f in attachments if _is_diff_file(f)]
        non_diff_paths = [f for f in attachments if not _is_diff_file(f)]
        diff_embedded = False

        for file_path in non_diff_paths:
            try:
                # For chat files, extract just the response instead of
                # attaching the entire chat history.
                actual_path = file_path
                if _is_chat_file(file_path):
                    response_file, _ = _make_response_only_file(file_path)
                    if response_file:
                        response_temps.append(response_file)
                        actual_path = str(response_file)

                        # Embed commit message into the response markdown
                        commit_message = n.action_data.get("commit_message")
                        if commit_message:
                            _prepend_commit_message_to_markdown(
                                response_file, commit_message
                            )

                        # Embed diff content into the response markdown
                        if diff_paths:
                            _append_diff_to_markdown(response_file, diff_paths)
                            diff_embedded = True

                if _is_image_file(actual_path):
                    send_photo(chat_id, actual_path)
                    rate_limit.record_send()
                    continue

                if _is_pdf_file(actual_path):
                    send_document(chat_id, actual_path)
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
                attachment_failures += 1
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
                    attachment_failures += 1
                    log.warning(
                        "Failed to send diff %s for notification %s",
                        dp,
                        n.id[:8],
                        exc_info=True,
                    )

        for p in pdf_temps + response_temps:
            p.unlink(missing_ok=True)

    _print_outbound_summary(
        reason=None,
        pending_actions_cleaned=pending_actions_cleaned,
        unsent_count=len(notifications),
        sent_count=sent_count,
        dry_run_count=dry_run_count,
        failed_count=failed_count,
        pending_action_writes=pending_action_writes,
        attachment_failures=attachment_failures,
        high_water_updates=high_water_updates,
        notification_ids=_short_notification_ids(notifications),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
