"""Message formatting and MarkdownV2 escaping for Telegram notifications."""

from __future__ import annotations

import json
import re
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from sase.notifications.models import Notification

from sase_chop_telegram import callback_data

# Telegram message limit
MAX_MESSAGE_LENGTH = 4096

# Max chars of converted plan content before truncation
PLAN_CONTENT_MAX = 3500

# Truncation threshold for notes content in non-plan messages
NOTES_TRUNCATION_THRESHOLD = 3500

# Max chars of prompt text to display in workflow-complete messages
PROMPT_DISPLAY_MAX = 1000

# Characters that must be escaped in MarkdownV2
_MARKDOWN_V2_SPECIAL = r"_*[]()~`>#+-=|{}.!"

# Regex for inline markdown formatting (order matters: code > bold > italic > link)
_INLINE_PATTERN = re.compile(
    r"(`[^`]+`)"  # inline code
    r"|(\*\*(.+?)\*\*)"  # bold
    r"|(\*([^*]+?)\*)"  # italic
    r"|(\[([^\]]+)\]\(([^)]+)\))"  # links
)


def escape_markdown_v2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2 format."""
    return re.sub(r"([" + re.escape(_MARKDOWN_V2_SPECIAL) + r"])", r"\\\1", text)


def _escape_code_entity(text: str) -> str:
    """Escape content inside code/pre entities for MarkdownV2.

    Inside pre and code entities, only '\\' and '`' need escaping.
    """
    return text.replace("\\", "\\\\").replace("`", "\\`")


def _escape_link_url(url: str) -> str:
    """Escape URL inside MarkdownV2 link parentheses.

    Inside (...) of inline links, only ')' and '\\' need escaping.
    """
    return url.replace("\\", "\\\\").replace(")", "\\)")


def _convert_inline(text: str) -> str:
    """Convert inline markdown formatting to Telegram MarkdownV2.

    Handles: inline code, **bold**, *italic*, and [text](url) links.
    All other text is escaped for MarkdownV2.
    """
    parts: list[str] = []
    pos = 0

    for match in _INLINE_PATTERN.finditer(text):
        # Escape plain text before this match
        if match.start() > pos:
            parts.append(escape_markdown_v2(text[pos:match.start()]))

        if match.group(1):  # inline code
            code = match.group(1)[1:-1]
            parts.append(f"`{_escape_code_entity(code)}`")
        elif match.group(2):  # bold **...**
            inner = match.group(3)
            parts.append(f"*{_convert_inline(inner)}*")
        elif match.group(4):  # italic *...*
            inner = match.group(5)
            parts.append(f"_{_convert_inline(inner)}_")
        elif match.group(6):  # link [text](url)
            link_text = match.group(7)
            link_url = match.group(8)
            parts.append(
                f"[{escape_markdown_v2(link_text)}]({_escape_link_url(link_url)})"
            )

        pos = match.end()

    # Escape remaining text after last match
    if pos < len(text):
        parts.append(escape_markdown_v2(text[pos:]))

    return "".join(parts)


def markdown_to_telegram_v2(md: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2 format.

    Handles headers, bold/italic, bullet/numbered lists, code blocks,
    horizontal rules, tables, links, and YAML frontmatter stripping.
    """
    lines = md.split("\n")
    result: list[str] = []
    i = 0

    # Strip YAML frontmatter
    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        if i < len(lines):
            i += 1  # skip closing ---
        # Skip blank line after frontmatter
        while i < len(lines) and not lines[i].strip():
            i += 1

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Code blocks
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            code_content = "\n".join(code_lines)
            escaped_code = _escape_code_entity(code_content)
            result.append(f"```{lang}\n{escaped_code}\n```")
            if i < len(lines):
                i += 1  # skip closing ```
            continue

        # Headers
        header_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if header_match:
            header_text = header_match.group(2)
            converted = _convert_inline(header_text)
            result.append(f"\n*{converted}*\n")
            i += 1
            continue

        # Horizontal rules
        if re.match(r"^[-*_]{3,}$", stripped):
            result.append("━━━━━━━━━━━━━━━━━━━━")
            i += 1
            continue

        # Table rows — collect and render as code block
        if stripped.startswith("|") and stripped.endswith("|"):
            table_lines: list[str] = []
            while (
                i < len(lines)
                and lines[i].strip().startswith("|")
                and lines[i].strip().endswith("|")
            ):
                table_lines.append(lines[i])
                i += 1
            table_content = "\n".join(table_lines)
            escaped_table = _escape_code_entity(table_content)
            result.append(f"```\n{escaped_table}\n```")
            continue

        # Bullet lists
        bullet_match = re.match(r"^(\s*)-\s+(.+)$", line)
        if bullet_match:
            indent = len(bullet_match.group(1))
            content = bullet_match.group(2)
            prefix = "  " * (indent // 2) + "•"
            result.append(f"{prefix} {_convert_inline(content)}")
            i += 1
            continue

        # Numbered lists
        num_match = re.match(r"^(\s*)(\d+)\.\s+(.+)$", line)
        if num_match:
            indent = len(num_match.group(1))
            num = num_match.group(2)
            content = num_match.group(3)
            prefix = "  " * (indent // 2) + escape_markdown_v2(f"{num}.")
            result.append(f"{prefix} {_convert_inline(content)}")
            i += 1
            continue

        # Regular text (or blank lines)
        if stripped:
            result.append(_convert_inline(stripped))
        else:
            result.append("")
        i += 1

    return "\n".join(result)


def _truncate_notes(notes: list[str], threshold: int = NOTES_TRUNCATION_THRESHOLD) -> str:
    """Join notes and truncate if exceeding threshold."""
    text = "\n".join(notes)
    if len(text) > threshold:
        text = text[:threshold] + "\n\n... (see TUI for full output)"
    return text


def format_notification(
    notification: Notification,
) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    """Format a notification for Telegram.

    Returns (message_text, keyboard_or_None, attachment_file_paths).
    """
    match notification.action:
        case "PlanApproval":
            return _format_plan_approval(notification)
        case "HITL":
            return _format_hitl(notification)
        case "UserQuestion":
            return _format_user_question(notification)
        case _:
            # Dispatch by sender for non-action notifications
            if notification.sender == "axe" and notification.files:
                return _format_error_digest(notification)
            if notification.sender in (
                "crs",
                "fix-hook",
                "query",
                "run-agent",
                "user-agent",
                "user-workflow",
            ):
                return _format_workflow_complete(notification)
            return _format_generic(notification)


def _notif_prefix(n: Notification) -> str:
    """First 8 chars of notification ID, used in callback data."""
    return n.id[:8]


def _format_plan_approval(
    n: Notification,
) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    prefix = _notif_prefix(n)
    notes_text = escape_markdown_v2(_truncate_notes(n.notes))
    attachments: list[str] = []

    plan_content = ""
    if n.files:
        plan_file = n.files[0]
        try:
            plan_content = Path(plan_file).read_text()
        except OSError:
            plan_content = ""

    if plan_content:
        converted = markdown_to_telegram_v2(plan_content)
        if len(converted) > PLAN_CONTENT_MAX:
            # Truncate at line boundary and attach full plan
            trunc_pos = converted.rfind("\n", 0, PLAN_CONTENT_MAX)
            if trunc_pos == -1:
                trunc_pos = PLAN_CONTENT_MAX
            converted = (
                converted[:trunc_pos]
                + f"\n\n{escape_markdown_v2('... (truncated, see attached)')}"
            )
            if n.files:
                attachments.append(n.files[0])
        text = f"📋 *Plan Review*\n\n{notes_text}\n\n{converted}"
    else:
        text = f"📋 *Plan Review*\n\n{notes_text}"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=callback_data.encode("plan", prefix, "approve"),
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=callback_data.encode("plan", prefix, "reject"),
                ),
            ],
            [
                InlineKeyboardButton(
                    "💬 Feedback",
                    callback_data=callback_data.encode("plan", prefix, "feedback"),
                ),
            ],
        ]
    )
    return text, keyboard, attachments


def _format_hitl(n: Notification) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    prefix = _notif_prefix(n)
    notes_text = escape_markdown_v2(_truncate_notes(n.notes))
    text = f"🔧 *HITL Request*\n\n{notes_text}"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Accept",
                    callback_data=callback_data.encode("hitl", prefix, "accept"),
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=callback_data.encode("hitl", prefix, "reject"),
                ),
                InlineKeyboardButton(
                    "💬 Feedback",
                    callback_data=callback_data.encode("hitl", prefix, "feedback"),
                ),
            ]
        ]
    )
    return text, keyboard, []


def _format_user_question(
    n: Notification,
) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    prefix = _notif_prefix(n)
    notes_text = escape_markdown_v2(_truncate_notes(n.notes))
    text = f"❓ *Question*\n\n{notes_text}"

    # Try to load question options from request file
    response_dir = n.action_data.get("response_dir", "")
    buttons: list[list[InlineKeyboardButton]] = []
    if response_dir:
        request_file = Path(response_dir) / "question_request.json"
        try:
            request_data = json.loads(request_file.read_text())
            questions = request_data.get("questions", [])
            if questions:
                # Use first question's options
                options = questions[0].get("options", [])
                for i, opt in enumerate(options):
                    label = opt.get("label", f"Option {i + 1}")
                    buttons.append(
                        [
                            InlineKeyboardButton(
                                label,
                                callback_data=callback_data.encode(
                                    "question", prefix, str(i)
                                ),
                            )
                        ]
                    )
        except (OSError, json.JSONDecodeError):
            pass

    # Always add a Custom button
    buttons.append(
        [
            InlineKeyboardButton(
                "💬 Custom",
                callback_data=callback_data.encode("question", prefix, "custom"),
            )
        ]
    )

    keyboard = InlineKeyboardMarkup(buttons)
    return text, keyboard, []


def _format_workflow_complete(
    n: Notification,
) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    notes_text = escape_markdown_v2(_truncate_notes(n.notes))
    agent_name = n.action_data.get("agent_name")
    if agent_name:
        escaped_name = escape_markdown_v2(agent_name)
        text = f"✅ *Workflow Complete* \\[{escaped_name}\\]\n\n{notes_text}"
    else:
        text = f"✅ *Workflow Complete*\n\n{notes_text}"

    prompt = n.action_data.get("prompt")
    if prompt:
        truncated = prompt if len(prompt) <= PROMPT_DISPLAY_MAX else (
            prompt[:PROMPT_DISPLAY_MAX] + "…"
        )
        text += f"\n\n📝 *Prompt:*\n{escape_markdown_v2(truncated)}"

    attachments = [
        str(p) for f in n.files if (p := Path(f).expanduser()).exists()
    ]
    return text, None, attachments


def _format_error_digest(
    n: Notification,
) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    notes_text = escape_markdown_v2(_truncate_notes(n.notes))
    text = f"⚠️ *Error Digest*\n\n{notes_text}"
    attachments = [f for f in n.files if Path(f).exists()]
    return text, None, attachments


def _format_generic(
    n: Notification,
) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    sender = escape_markdown_v2(n.sender)
    notes_text = escape_markdown_v2(_truncate_notes(n.notes))
    text = f"🔔 *{sender}*\n\n{notes_text}"
    return text, None, []
