"""Outbound chop entry point: send sase notifications to Telegram."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from sase.ace.tui_activity import is_idle
from sase.history.chat import extract_response_from_chat_file
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


_RESEARCH_MD_PATH_RE = re.compile(r"^research/.+\.md$")


def _parse_research_sections(
    diff_content: str,
) -> tuple[list[tuple[str, str]], str]:
    """Parse a unified diff for newly added research/*.md files.

    Returns ``(research_files, filtered_diff)`` where *research_files* is a
    list of ``(filename, content)`` tuples and *filtered_diff* is the original
    diff with the research file sections removed.
    """
    research_files: list[tuple[str, str]] = []
    filtered_parts: list[str] = []

    # Split into per-file sections by "diff --git" header
    sections = re.split(r"(?=^diff --git )", diff_content, flags=re.MULTILINE)

    for section in sections:
        if not section.strip():
            continue

        header_match = re.match(r"^diff --git a/.+ b/(.+)$", section, re.MULTILINE)
        if not header_match:
            filtered_parts.append(section)
            continue

        file_path = header_match.group(1)
        # Only strip *new* research markdown files — detect "new file mode"
        # before the first hunk header.
        preamble = section.split("\n@@")[0] if "\n@@" in section else section
        is_new = "new file mode" in preamble
        is_research_md = bool(_RESEARCH_MD_PATH_RE.match(file_path))

        if is_new and is_research_md:
            content = _extract_new_file_content(section)
            filename = Path(file_path).name
            research_files.append((filename, content))
        else:
            filtered_parts.append(section)

    return research_files, "".join(filtered_parts)


def _extract_new_file_content(diff_section: str) -> str:
    """Extract the full text of a newly added file from its diff section."""
    lines = diff_section.split("\n")
    content_lines: list[str] = []
    in_hunk = False

    for line in lines:
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk:
            if line.startswith("+"):
                content_lines.append(line[1:])
            elif line.startswith("\\"):
                continue  # "\ No newline at end of file"

    return "\n".join(content_lines)


def _extract_research_from_diffs(
    diff_paths: list[str],
) -> tuple[list[tuple[str, str]], list[Path], bool]:
    """Analyze diff files for new research/*.md additions.

    Returns ``(research_entries, filtered_diff_temps, has_non_research)``:

    - *research_entries*: ``[(filename, content), ...]`` for each new
      research markdown file found in the diffs.
    - *filtered_diff_temps*: temp ``.diff`` files with research sections
      stripped (empty list when no non-research changes remain).
    - *has_non_research*: ``True`` if any non-research changes exist.
    """
    research_entries: list[tuple[str, str]] = []
    filtered_diff_temps: list[Path] = []
    has_non_research = False

    for dp in diff_paths:
        try:
            content = Path(dp).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not content.strip():
            continue

        entries, filtered = _parse_research_sections(content)
        research_entries.extend(entries)
        if filtered.strip():
            has_non_research = True
            tmp = tempfile.NamedTemporaryFile(
                prefix="filtered-diff-",
                suffix=".diff",
                delete=False,
                mode="w",
                encoding="utf-8",
            )
            tmp.write(filtered)
            tmp.close()
            filtered_diff_temps.append(Path(tmp.name))

    return research_entries, filtered_diff_temps, has_non_research


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
        now_str = datetime.fromtimestamp(now, tz=EASTERN_TZ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
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
            lines.append(
                f"    {n.id[:8]} sender={n.sender} ts={n.timestamp} action={n.action}"
            )

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

    # Acquire exclusive lock to prevent concurrent outbound runs from
    # sending the same notification multiple times.  Lumberjack fires
    # this chop every few seconds; if a run takes longer than the
    # interval (retries, rate-limit sleeps, PDF conversion), overlapping
    # runs would read the same high-water mark and duplicate sends.
    from sase_telegram.outbound import release_outbound_lock, try_acquire_outbound_lock

    lock_fd = try_acquire_outbound_lock()
    if lock_fd is None:
        return 0  # Another instance is running

    try:
        return _run_outbound(args)
    finally:
        release_outbound_lock(lock_fd)


def _run_outbound(args: argparse.Namespace) -> int:
    """Core outbound logic, called while holding the exclusive lock."""
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

        # --- Analyse diffs for new research/*.md files ---
        raw_diff_paths = [
            f for f in n.files if _is_diff_file(f) and Path(f).expanduser().exists()
        ]
        research_entries: list[tuple[str, str]] = []
        filtered_diff_temps: list[Path] = []
        has_non_research = False
        if raw_diff_paths:
            research_entries, filtered_diff_temps, has_non_research = (
                _extract_research_from_diffs(raw_diff_paths)
            )

        text, keyboard, attachments = format_notification(
            n,
            has_research=bool(research_entries),
            has_non_research_diff=has_non_research if raw_diff_paths else None,
        )

        if args.dry_run:
            print(f"--- Notification {n.id} ---")
            print(f"Text: {text}")
            if keyboard:
                print(f"Keyboard: {keyboard.inline_keyboard}")
            if attachments:
                print(f"Attachments: {attachments}")
            if research_entries:
                print(f"Research files: {[name for name, _ in research_entries]}")
            print()
            # Advance high-water mark after each notification to prevent
            # re-sending if a later notification fails.
            mark_sent([n])
            for p in filtered_diff_temps:
                p.unlink(missing_ok=True)
            continue

        msg = None
        try:
            msg = send_message(
                chat_id, text, reply_markup=keyboard, parse_mode="MarkdownV2"
            )
            rate_limit.record_send()
        except Exception:
            log.warning(
                "Failed to send notification %s to Telegram",
                n.id[:8],
                exc_info=True,
            )

        # Always advance high-water mark — even on send failure — to
        # prevent an infinite resend loop.  Failed notifications are
        # still visible in the TUI.
        mark_sent([n])

        if msg is None:
            for p in filtered_diff_temps:
                p.unlink(missing_ok=True)
            continue

        pdf_temps: list[Path] = []
        response_temps: list[Path] = []
        research_temps: list[Path] = []

        # Use filtered diffs (research sections stripped) when available;
        # otherwise fall back to the raw diff attachments.
        if raw_diff_paths:
            diff_paths = [str(p) for p in filtered_diff_temps]
        else:
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

                        # Embed diff content into the response markdown
                        if diff_paths:
                            _append_diff_to_markdown(response_file, diff_paths)
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

        # Send each new research file as its own PDF attachment
        for filename, content in research_entries:
            try:
                md_tmp = tempfile.NamedTemporaryFile(
                    prefix=f"research-{Path(filename).stem}-",
                    suffix=".md",
                    delete=False,
                    mode="w",
                    encoding="utf-8",
                )
                md_tmp.write(content)
                md_tmp.close()
                md_path = Path(md_tmp.name)
                research_temps.append(md_path)

                pdf = md_to_pdf(str(md_path))
                if pdf:
                    research_temps.append(Path(pdf))
                    send_document(chat_id, pdf)
                else:
                    send_document(chat_id, str(md_path))
                rate_limit.record_send()
            except Exception:
                log.warning(
                    "Failed to send research file %s for notification %s",
                    filename,
                    n.id[:8],
                    exc_info=True,
                )

        for p in pdf_temps + response_temps + filtered_diff_temps + research_temps:
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
