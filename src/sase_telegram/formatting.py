"""Message formatting and MarkdownV2 escaping for Telegram notifications."""

from __future__ import annotations

import json
import re
from pathlib import Path

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from sase.notifications.models import Notification

from sase_telegram import callback_data

# Telegram message limit
MAX_MESSAGE_LENGTH = 4096

# Truncation threshold for notes/plan content (hard cap before blockquote wrapping)
NOTES_TRUNCATION_THRESHOLD = 3500

# Content longer than this is wrapped in an expandable blockquote (Bot API 7.4+)
EXPANDABLE_THRESHOLD = 500

# Max chars of prompt text to display in workflow-complete messages
PROMPT_DISPLAY_MAX = 1000

# Characters that must be escaped in MarkdownV2
_MARKDOWN_V2_SPECIAL = r"_*[]()~`>#+-=|{}.!"

# Matches ``` code blocks in MarkdownV2 output (language specifier optional)
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)\n```", re.DOTALL)

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
            parts.append(escape_markdown_v2(text[pos : match.start()]))

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


def _code_blocks_to_inline(text: str) -> str:
    """Convert ``` code blocks to per-line inline code for blockquote compat.

    Telegram's MarkdownV2 parser splits expandable blockquotes when they
    contain ``` code blocks.  This replaces each code block with per-line
    inline code (single backticks) which renders correctly inside blockquotes.
    """

    def _replace(m: re.Match[str]) -> str:
        lines = m.group(1).split("\n")
        return "\n".join(f"`{line}`" if line.strip() else line for line in lines)

    return _CODE_BLOCK_RE.sub(_replace, text)


def _wrap_expandable_blockquote(text: str) -> str:
    """Wrap text in a Telegram MarkdownV2 expandable blockquote (Bot API 7.4+).

    First line starts with ``**>``, subsequent lines with ``>``,
    and the closing ``||`` is appended to the last line.

    Empty lines use a zero-width space (``\\u200B``) so Telegram does not
    split the content into multiple separate blockquotes.
    """
    if not text:
        return text
    # Strip leading/trailing blank lines, collapse consecutive blanks, and
    # replace remaining blank lines with a zero-width space to keep the
    # blockquote continuous.
    raw_lines = text.strip("\n").split("\n")
    lines: list[str] = []
    prev_blank = False
    for line in raw_lines:
        if line.strip():
            lines.append(line)
            prev_blank = False
        elif not prev_blank:
            lines.append("\u200b")
            prev_blank = True
    # Drop trailing blank placeholder
    while lines and lines[-1] == "\u200b":
        lines.pop()
    if not lines:
        return text

    result = [f"**>{lines[0]}"]
    for line in lines[1:]:
        result.append(f">{line}")
    # If the last line could interfere with || (e.g. code block closing ```),
    # put the closing marker on its own blockquote line.
    if result[-1].rstrip().endswith("```"):
        result.append(">||")
    else:
        result[-1] += "||"
    return "\n".join(result)


def _format_notes_text(
    notes: list[str],
    max_length: int = NOTES_TRUNCATION_THRESHOLD,
) -> str:
    """Format notes for Telegram, using expandable blockquote for long content.

    Short notes are returned as escaped MarkdownV2 text.
    Long notes (> EXPANDABLE_THRESHOLD) are wrapped in an expandable blockquote.
    Very long notes (> max_length) are truncated before wrapping.
    """
    text = "\n".join(notes)
    use_blockquote = len(text) > EXPANDABLE_THRESHOLD
    if len(text) > max_length:
        text = text[:max_length] + "\n\n... (see TUI for full output)"
    escaped = escape_markdown_v2(text)
    if use_blockquote:
        return _wrap_expandable_blockquote(escaped)
    return escaped


def format_notification(
    notification: Notification,
    *,
    has_research: bool = False,
    has_non_research_diff: bool | None = None,
) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    """Format a notification for Telegram.

    Returns (message_text, keyboard_or_None, attachment_file_paths).

    Args:
        has_research: True when the agent created new research/*.md files.
        has_non_research_diff: When set, overrides the diff-file heuristic
            for the pencil icon.  ``None`` means "use default logic".
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
            if notification.sender == "image":
                return _format_image_generated(notification)
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
                return _format_workflow_complete(
                    notification,
                    has_research=has_research,
                    has_non_research_diff=has_non_research_diff,
                )
            return _format_generic(notification)


def _notif_prefix(n: Notification) -> str:
    """First 8 chars of notification ID, used in callback data."""
    return n.id[:8]


def _format_plan_approval(
    n: Notification,
) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    prefix = _notif_prefix(n)
    notes_text = _format_notes_text(n.notes)
    attachments: list[str] = []

    agent_name = n.action_data.get("agent_name")
    if agent_name:
        escaped_name = escape_markdown_v2(agent_name)
        name_line = f"  _@{escaped_name}_"
    else:
        name_line = ""

    plan_content = ""
    if n.files:
        plan_file = n.files[0]
        try:
            plan_content = Path(plan_file).read_text()
        except OSError:
            plan_content = ""

    if plan_content:
        converted = markdown_to_telegram_v2(plan_content)
        header = f"📋 *Plan Review*{name_line}\n\n{notes_text}\n\n"

        if len(converted) > EXPANDABLE_THRESHOLD:
            # Telegram's MarkdownV2 parser splits expandable blockquotes
            # when they contain ``` code blocks.  Convert to inline code.
            converted = _code_blocks_to_inline(converted)
            plan_block = _wrap_expandable_blockquote(converted)
            text = f"{header}{plan_block}"

            if len(text) > MAX_MESSAGE_LENGTH:
                # Too long for one message — truncate and attach full PDF.
                # Blockquote adds '>' per line, so overhead scales with line
                # count.  Use 0.75 factor as a conservative estimate, then
                # refine in a safety loop if still over the limit.
                suffix = f"\n\n{escape_markdown_v2('... (truncated, see attached)')}"
                budget = MAX_MESSAGE_LENGTH - len(header) - len(suffix)
                target = int(budget * 0.75)
                while target > 100:
                    trunc_pos = converted.rfind("\n", 0, target)
                    if trunc_pos == -1:
                        trunc_pos = target
                    plan_block = _wrap_expandable_blockquote(
                        converted[:trunc_pos] + suffix
                    )
                    text = f"{header}{plan_block}"
                    if len(text) <= MAX_MESSAGE_LENGTH:
                        break
                    target = int(target * 0.8)
        else:
            # Short plan — show inline without blockquote
            text = f"{header}{converted}"
        # Always attach plan PDF regardless of message length
        if n.files:
            attachments.append(n.files[0])
    else:
        text = f"📋 *Plan Review*{name_line}\n\n{notes_text}"

    row1 = [
        InlineKeyboardButton(
            "✅ Approve",
            callback_data=callback_data.encode("plan", prefix, "approve"),
        ),
        InlineKeyboardButton(
            "📦 Commit",
            callback_data=callback_data.encode("plan", prefix, "commit"),
        ),
        InlineKeyboardButton(
            "📋 Epic",
            callback_data=callback_data.encode("plan", prefix, "epic"),
        ),
    ]
    row2 = [
        InlineKeyboardButton(
            "❌ Reject",
            callback_data=callback_data.encode("plan", prefix, "reject"),
        ),
        InlineKeyboardButton(
            "💬 Feedback",
            callback_data=callback_data.encode("plan", prefix, "feedback"),
        ),
    ]
    keyboard = InlineKeyboardMarkup([row1, row2])
    return text, keyboard, attachments


def _format_hitl(n: Notification) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    prefix = _notif_prefix(n)
    notes_text = _format_notes_text(n.notes)
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
    notes_text = _format_notes_text(n.notes)
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
    *,
    has_research: bool = False,
    has_non_research_diff: bool | None = None,
) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    from sase.llm_provider.registry import format_provider_model_label

    notes_text = _format_notes_text(n.notes)
    agent_name = n.action_data.get("agent_name")
    has_diff = any(Path(f).suffix.lower() == ".diff" for f in n.files)
    if has_non_research_diff is not None:
        has_diff = has_non_research_diff
    icon_parts = ["✅"]
    if has_diff:
        icon_parts.append("✏️")
    if has_research:
        icon_parts.append("📚")
    icon = "".join(icon_parts)
    label = escape_markdown_v2(
        format_provider_model_label(
            n.action_data.get("llm_provider"),
            n.action_data.get("model"),
        )
    )
    if agent_name:
        escaped_name = escape_markdown_v2(agent_name)
        name_line = f"  _@{escaped_name}_"
    else:
        name_line = ""
    text = f"{icon} *{label} Complete*{name_line}\n\n{notes_text}"

    pr_url = n.action_data.get("pr_url")
    if pr_url:
        escaped_url = escape_markdown_v2(pr_url)
        text += f"\n\n🔗 *PR:* {escaped_url}"

    prompt = n.action_data.get("prompt")
    if prompt:
        truncated = (
            prompt
            if len(prompt) <= PROMPT_DISPLAY_MAX
            else (prompt[:PROMPT_DISPLAY_MAX] + "…")
        )
        text += f"\n\n📝 *Prompt:*\n{escape_markdown_v2(truncated)}"

    attachments = [str(p) for f in n.files if (p := Path(f).expanduser()).exists()]

    keyboard: InlineKeyboardMarkup | None = None
    if agent_name:
        from sase.xprompt import extract_vcs_workflow_tag

        resume_text = f"#resume:{agent_name} "
        raw_prompt = n.action_data.get("prompt", "")
        if raw_prompt:
            vcs_tag = extract_vcs_workflow_tag(raw_prompt)
            if vcs_tag:
                resume_text = f"{vcs_tag}{resume_text}"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "▶️ Resume",
                        copy_text=CopyTextButton(text=resume_text),
                    ),
                ]
            ]
        )

    return text, keyboard, attachments


def _format_error_digest(
    n: Notification,
) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    notes_text = _format_notes_text(n.notes)
    text = f"⚠️ *Error Digest*\n\n{notes_text}"
    attachments = [f for f in n.files if Path(f).exists()]
    return text, None, attachments


def _format_image_generated(
    n: Notification,
) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    notes_text = _format_notes_text(n.notes)
    model = escape_markdown_v2(n.action_data.get("model", "gemini"))
    text = f"🖼️ *Image Generated* \\[{model}\\]\n\n{notes_text}"
    attachments = [f for f in n.files if Path(f).exists()]
    return text, None, attachments


def _format_generic(
    n: Notification,
) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
    sender = escape_markdown_v2(n.sender)
    notes_text = _format_notes_text(n.notes)
    text = f"🔔 *{sender}*\n\n{notes_text}"
    return text, None, []
