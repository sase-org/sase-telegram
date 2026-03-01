"""Tests for message formatting."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from sase.notifications.models import Notification
from sase_chop_telegram.formatting import (
    NOTES_TRUNCATION_THRESHOLD,
    PLAN_CONTENT_MAX,
    _convert_inline,
    _escape_code_entity,
    escape_markdown_v2,
    format_notification,
    markdown_to_telegram_v2,
)


def _make_notification(
    action: str | None = None,
    sender: str = "test",
    notes: list[str] | None = None,
    files: list[str] | None = None,
    action_data: dict[str, str] | None = None,
) -> Notification:
    return Notification(
        id="abcd1234-0000-0000-0000-000000000000",
        timestamp="2025-06-01T12:00:00+00:00",
        sender=sender,
        notes=notes or ["Test notification"],
        files=files or [],
        action=action,
        action_data=action_data or {},
    )


class TestEscapeMarkdownV2:
    def test_escapes_all_special_chars(self):
        text = "Hello_World *bold* [link](url) ~strike~ `code` >quote #h +p -m =e |p {b} .d !e"
        result = escape_markdown_v2(text)
        for char in r"_*[]()~`>#+-=|{}.!":
            assert f"\\{char}" in result

    def test_plain_text_unchanged(self):
        assert escape_markdown_v2("hello world") == "hello world"

    def test_empty_string(self):
        assert escape_markdown_v2("") == ""


class TestEscapeCodeEntity:
    def test_escapes_backtick_and_backslash(self):
        assert _escape_code_entity("foo\\bar`baz") == "foo\\\\bar\\`baz"

    def test_plain_text_unchanged(self):
        assert _escape_code_entity("hello world") == "hello world"

    def test_preserves_other_special_chars(self):
        # Inside code entities, only \ and ` need escaping
        assert _escape_code_entity("a_b*c.d!e") == "a_b*c.d!e"


class TestConvertInline:
    def test_bold(self):
        assert _convert_inline("**hello**") == "*hello*"

    def test_italic(self):
        assert _convert_inline("*hello*") == "_hello_"

    def test_inline_code(self):
        result = _convert_inline("`foo.bar`")
        assert result == "`foo.bar`"

    def test_link(self):
        result = _convert_inline("[click](http://example.com)")
        assert result == "[click](http://example.com)"

    def test_mixed_formatting(self):
        result = _convert_inline("**File:** `src/app.py`")
        assert "*File:*" in result
        assert "`src/app.py`" in result

    def test_plain_text_escaped(self):
        result = _convert_inline("version 1.0")
        assert result == "version 1\\.0"

    def test_bold_with_special_chars(self):
        result = _convert_inline("**File:** path.txt")
        assert "*File:*" in result
        assert "path\\.txt" in result


class TestMarkdownToTelegramV2:
    def test_headers(self):
        result = markdown_to_telegram_v2("# Main Header\n\n## Sub Header")
        assert "*Main Header*" in result
        assert "*Sub Header*" in result

    def test_bullet_list(self):
        result = markdown_to_telegram_v2("- item one\n- item two")
        assert "• item one" in result
        assert "• item two" in result

    def test_numbered_list(self):
        result = markdown_to_telegram_v2("1. first\n2. second")
        assert "1\\." in result
        assert "first" in result
        assert "2\\." in result

    def test_code_block(self):
        md = "```python\ndef foo():\n    pass\n```"
        result = markdown_to_telegram_v2(md)
        assert "```python" in result
        assert "def foo():" in result
        assert "    pass" in result

    def test_code_block_escaping(self):
        md = "```\nfoo\\bar `baz`\n```"
        result = markdown_to_telegram_v2(md)
        assert "foo\\\\bar \\`baz\\`" in result

    def test_horizontal_rule(self):
        result = markdown_to_telegram_v2("text\n\n---\n\nmore text")
        assert "━━━━━━━━━━━━━━━━━━━━" in result

    def test_table_as_code_block(self):
        md = "| Col1 | Col2 |\n| --- | --- |\n| a | b |"
        result = markdown_to_telegram_v2(md)
        assert "```\n" in result
        assert "Col1" in result

    def test_yaml_frontmatter_stripped(self):
        md = "---\nbead_id: sase-0dw\n---\n\n# Plan Title"
        result = markdown_to_telegram_v2(md)
        assert "bead_id" not in result
        assert "*Plan Title*" in result

    def test_bold_in_bullet(self):
        result = markdown_to_telegram_v2("- **PLANNING** — agent sent a plan")
        assert "• *PLANNING* — agent sent a plan" in result

    def test_full_plan(self):
        md = (
            "---\nbead_id: sase-0dw\n---\n\n"
            "# Plan: Add feature X\n\n"
            "## Context\n\n"
            "Some context text.\n\n"
            "- **Item A** — description\n"
            "- **Item B** — description\n\n"
            "---\n\n"
            "## Phase 1\n\n"
            "### 1. Do thing\n\n"
            "**File:** `src/app.py`\n\n"
            "```python\ndef foo():\n    pass\n```\n"
        )
        result = markdown_to_telegram_v2(md)

        # Frontmatter stripped
        assert "bead_id" not in result
        # Headers converted to bold
        assert "*Plan: Add feature X*" in result
        assert "*Context*" in result
        assert "*Phase 1*" in result
        # Bullets with bold
        assert "• *Item A* — description" in result
        # Horizontal rule
        assert "━━━━━━━━━━━━━━━━━━━━" in result
        # Code block preserved
        assert "```python" in result
        assert "def foo():" in result

    def test_empty_lines_preserved(self):
        result = markdown_to_telegram_v2("line one\n\nline two")
        assert "\n\n" in result


class TestFormatPlanApproval:
    def test_with_short_plan(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Short Plan\n\nSome content here.")
            plan_file = f.name

        n = _make_notification(
            action="PlanApproval",
            sender="plan",
            notes=["Plan ready for review: test.md"],
            files=[plan_file],
            action_data={"response_dir": "/tmp/test", "session_id": "s1"},
        )
        text, keyboard, attachments = format_notification(n)

        assert "Plan Review" in text
        # Plan content is now richly formatted (not in a raw code block)
        assert "*Short Plan*" in text
        assert "Some content here" in text
        assert keyboard is not None
        assert len(keyboard.inline_keyboard) == 2
        assert len(keyboard.inline_keyboard[0]) == 2  # Approve + Reject
        assert "Approve" in keyboard.inline_keyboard[0][0].text
        assert "Reject" in keyboard.inline_keyboard[0][1].text
        assert len(keyboard.inline_keyboard[1]) == 1  # Feedback
        assert "Feedback" in keyboard.inline_keyboard[1][0].text
        assert attachments == []

        Path(plan_file).unlink()

    def test_with_large_plan(self):
        # Generate content that exceeds PLAN_CONTENT_MAX after conversion
        large_content = "\n".join(f"## Section {i}\n\nContent line {i}." for i in range(200))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(large_content)
            plan_file = f.name

        n = _make_notification(
            action="PlanApproval",
            sender="plan",
            notes=["Plan ready for review: big.md"],
            files=[plan_file],
        )
        text, keyboard, attachments = format_notification(n)

        assert "truncated" in text
        assert plan_file in attachments
        assert keyboard is not None

        Path(plan_file).unlink()

    def test_missing_plan_file(self):
        n = _make_notification(
            action="PlanApproval",
            sender="plan",
            notes=["Plan ready for review"],
            files=["/nonexistent/plan.md"],
        )
        text, keyboard, _ = format_notification(n)
        assert "Plan Review" in text
        assert keyboard is not None


class TestFormatHITL:
    def test_format_and_keyboard(self):
        n = _make_notification(
            action="HITL",
            sender="hitl",
            notes=["HITL waiting: step 'review' in my-workflow"],
        )
        text, keyboard, attachments = format_notification(n)

        assert "HITL Request" in text
        assert "review" in text
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        assert len(buttons) == 1
        assert len(buttons[0]) == 3  # Accept + Reject + Feedback
        assert "Accept" in buttons[0][0].text
        assert "Reject" in buttons[0][1].text
        assert "Feedback" in buttons[0][2].text
        assert attachments == []


class TestFormatUserQuestion:
    def test_with_options(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            request_file = Path(tmpdir) / "question_request.json"
            request_data = {
                "questions": [
                    {
                        "question": "Which DB?",
                        "options": [
                            {"label": "PostgreSQL"},
                            {"label": "SQLite"},
                        ],
                    }
                ]
            }
            request_file.write_text(json.dumps(request_data))

            n = _make_notification(
                action="UserQuestion",
                sender="question",
                notes=["Claude is asking a question"],
                action_data={"response_dir": tmpdir, "session_id": "s1"},
            )
            text, keyboard, attachments = format_notification(n)

        assert "Question" in text
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        # 2 option buttons + 1 Custom button
        assert len(buttons) == 3
        assert "PostgreSQL" in buttons[0][0].text
        assert "SQLite" in buttons[1][0].text
        assert "Custom" in buttons[2][0].text
        assert attachments == []

    def test_without_request_file(self):
        n = _make_notification(
            action="UserQuestion",
            sender="question",
            notes=["Claude is asking a question"],
            action_data={"response_dir": "/nonexistent", "session_id": "s1"},
        )
        text, keyboard, _ = format_notification(n)
        assert "Question" in text
        assert keyboard is not None
        # Only Custom button when request file is missing
        assert len(keyboard.inline_keyboard) == 1
        assert "Custom" in keyboard.inline_keyboard[0][0].text


class TestFormatWorkflowComplete:
    def test_no_keyboard(self):
        n = _make_notification(
            sender="crs",
            notes=["Workflow completed successfully"],
        )
        text, keyboard, attachments = format_notification(n)

        assert "Workflow Complete" in text
        assert keyboard is None
        assert attachments == []

    def test_includes_agent_name(self):
        n = _make_notification(
            sender="user-agent",
            notes=["Agent completed: my-workflow"],
            action_data={"agent_name": "c"},
        )
        text, keyboard, attachments = format_notification(n)

        assert "Workflow Complete" in text
        assert "\\[c\\]" in text
        assert keyboard is None


class TestFormatErrorDigest:
    def test_with_digest_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Error details here")
            digest_file = f.name

        n = _make_notification(
            sender="axe",
            notes=["3 error(s) in the last hour"],
            files=[digest_file],
        )
        text, keyboard, attachments = format_notification(n)

        assert "Error Digest" in text
        assert keyboard is None
        assert digest_file in attachments

        Path(digest_file).unlink()

    def test_missing_digest_file(self):
        n = _make_notification(
            sender="axe",
            notes=["2 error(s) in the last hour"],
            files=["/nonexistent/digest.txt"],
        )
        text, keyboard, attachments = format_notification(n)

        assert "Error Digest" in text
        assert attachments == []


class TestFormatGeneric:
    def test_fallback_format(self):
        n = _make_notification(
            sender="unknown-sender",
            notes=["Something happened"],
        )
        text, keyboard, attachments = format_notification(n)

        assert "unknown\\-sender" in text
        assert "Something happened" in text
        assert keyboard is None
        assert attachments == []


class TestNoteTruncation:
    def test_short_notes_not_truncated(self):
        n = _make_notification(
            action="HITL",
            sender="hitl",
            notes=["Short HITL output"],
        )
        text, _, _ = format_notification(n)
        assert "see TUI for full output" not in text

    def test_long_hitl_notes_truncated(self):
        long_note = "x" * (NOTES_TRUNCATION_THRESHOLD + 500)
        n = _make_notification(
            action="HITL",
            sender="hitl",
            notes=[long_note],
        )
        text, _, _ = format_notification(n)
        assert "see TUI for full output" in text

    def test_long_generic_notes_truncated(self):
        long_note = "y" * (NOTES_TRUNCATION_THRESHOLD + 100)
        n = _make_notification(
            sender="unknown",
            notes=[long_note],
        )
        text, _, _ = format_notification(n)
        assert "see TUI for full output" in text

    def test_long_error_digest_notes_truncated(self):
        long_note = "z" * (NOTES_TRUNCATION_THRESHOLD + 100)
        n = _make_notification(
            sender="axe",
            notes=[long_note],
            files=["/nonexistent/digest.txt"],
        )
        text, _, _ = format_notification(n)
        assert "see TUI for full output" in text
