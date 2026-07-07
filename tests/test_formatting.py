"""Tests for message formatting."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from sase.notifications.models import Notification
import sase_telegram.formatting as formatting
from sase_telegram.formatting import (
    EXPANDABLE_THRESHOLD,
    MAX_MESSAGE_LENGTH,
    NOTES_TRUNCATION_THRESHOLD,
    _code_blocks_to_inline,
    _convert_inline,
    _escape_code_entity,
    _format_notes_text,
    _wrap_expandable_blockquote,
    escape_markdown_v2,
    format_answered_question,
    format_notification,
    format_questions_complete,
    markdown_to_telegram_v2,
    render_question_message,
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


class TestDisplayHumanizers:
    def test_display_cl_names_in_text_also_humanizes_vcs_refs(
        self, monkeypatch
    ) -> None:
        import sase.project_display_names as pdn

        monkeypatch.setattr(
            pdn,
            "humanize_vcs_refs_in_text",
            lambda text: text.replace("gh_sase-org__sase", "sase"),
        )
        monkeypatch.setattr(
            pdn,
            "humanize_cl_names_in_text",
            lambda text: text.replace("sase_task", "sase-task"),
        )

        assert (
            formatting.display_cl_names_in_text(
                "#gh:gh_sase-org__sase Continue sase_task"
            )
            == "#gh:sase Continue sase-task"
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
        assert len(keyboard.inline_keyboard[0]) == 3  # Tale + Approve + Epic
        assert "Tale" in keyboard.inline_keyboard[0][0].text
        assert keyboard.inline_keyboard[0][0].callback_data.endswith(":approve")
        assert keyboard.inline_keyboard[0][1].text == "✅ Approve"
        assert keyboard.inline_keyboard[0][1].callback_data == "plan:" + (
            keyboard.inline_keyboard[0][0].callback_data.split(":")[1] + ":run"
        )
        assert "Epic" in keyboard.inline_keyboard[0][2].text
        assert len(keyboard.inline_keyboard[1]) == 3  # Legend + Reject + Feedback
        assert "Legend" in keyboard.inline_keyboard[1][0].text
        assert keyboard.inline_keyboard[1][0].callback_data == "plan:" + (
            keyboard.inline_keyboard[0][0].callback_data.split(":")[1] + ":legend"
        )
        assert "Reject" in keyboard.inline_keyboard[1][1].text
        assert "Feedback" in keyboard.inline_keyboard[1][2].text
        assert attachments == [plan_file]

        Path(plan_file).unlink()

    def test_with_large_plan(self):
        # Generate content that exceeds MAX_MESSAGE_LENGTH after conversion
        large_content = "\n".join(
            f"## Section {i}\n\nContent line {i}." for i in range(200)
        )
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

        # Plan should be in expandable blockquote and truncated to fit
        assert text.startswith("📋 *Plan Review*")
        assert "**>" in text  # expandable blockquote marker
        assert "truncated" in text
        assert len(text) <= MAX_MESSAGE_LENGTH
        assert plan_file in attachments
        assert keyboard is not None

        Path(plan_file).unlink()

    def test_includes_runtime_before_notes_and_plan_body(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Short Plan\n\nSome content here.")
            plan_file = f.name

        n = _make_notification(
            action="PlanApproval",
            sender="plan",
            notes=["Plan ready for review: test.md"],
            files=[plan_file],
            action_data={
                "response_dir": "/tmp/test",
                "session_id": "s1",
                "agent_name": "test_agent",
                "llm_provider": "claude",
                "model": "opus",
                "runtime": "4m32s",
            },
        )
        text, _, _ = format_notification(n)

        assert text.startswith(
            "📋 *CLAUDE\\(opus\\) Plan Review*  _@test\\_agent_\n*Runtime:* 4m32s\n\n"
        )
        assert text.index("*Runtime:* 4m32s") < text.index("Plan ready")
        assert text.index("Plan ready") < text.index("*Short Plan*")

        Path(plan_file).unlink()

    def test_plan_with_code_blocks_no_triple_backticks_in_blockquote(self):
        """Code blocks inside expandable blockquotes are converted to inline code."""
        # Content must exceed EXPANDABLE_THRESHOLD (500 chars) after conversion
        padding = "\n".join(f"Step {i}: do thing {i}." for i in range(30))
        medium_content = (
            "# Plan\n\n## Phase 1\n\n" + padding + "\n\n"
            "```python\ndef foo():\n    pass\n```\n\n"
            "## Phase 2\n\nMore text.\n\n"
            "```bash\necho hello\n```\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(medium_content)
            plan_file = f.name

        n = _make_notification(
            action="PlanApproval",
            sender="plan",
            notes=["Plan ready"],
            files=[plan_file],
        )
        text, _, _ = format_notification(n)

        # Blockquote should not contain ``` code blocks (causes splitting)
        blockquote_start = text.index("**>")
        blockquote_content = text[blockquote_start:]
        assert "```" not in blockquote_content
        # But should still have inline code
        assert "`def foo():`" in text
        assert "`echo hello`" in text

        Path(plan_file).unlink()

    def test_medium_plan_in_blockquote_no_truncation(self):
        # Plan longer than EXPANDABLE_THRESHOLD but fits in one message
        medium_content = "\n".join(f"## Step {i}\n\nDo thing {i}." for i in range(20))
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(medium_content)
            plan_file = f.name

        n = _make_notification(
            action="PlanApproval",
            sender="plan",
            notes=["Plan ready"],
            files=[plan_file],
        )
        text, keyboard, attachments = format_notification(n)

        # Should be in expandable blockquote but NOT truncated
        assert "**>" in text
        assert "||" in text
        assert "truncated" not in text
        assert attachments == [plan_file]
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

    def test_humanizes_agent_name_in_header(self, monkeypatch):
        monkeypatch.setattr(
            formatting,
            "display_cl_name",
            lambda name: "SASE Core_plan" if name == "sase_plan" else name,
        )

        n = _make_notification(
            action="PlanApproval",
            sender="plan",
            notes=["Plan ready for review"],
            files=["/nonexistent/plan.md"],
            action_data={"agent_name": "sase_plan"},
        )
        text, _, _ = format_notification(n)

        assert "_@SASE Core\\_plan_" in text


class TestFormatLaunchApproval:
    def test_with_preview_and_keyboard(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Launch Preview\n\n- Start agent A\n- Start agent B")
            preview_file = f.name

        n = _make_notification(
            action="LaunchApproval",
            sender="launch",
            notes=["Launch approval requested: 2 slots", "Source: telegram"],
            files=[preview_file],
            action_data={
                "response_dir": "/tmp/launch",
                "request_id": "req_123",
                "source_surface": "telegram",
                "slot_count": "2",
            },
        )
        text, keyboard, attachments = format_notification(n)

        assert text.startswith("🚀 *Launch Approval*")
        assert "*Slots:* 2 slots" in text
        assert "*Source:* telegram" in text
        assert "*Request:* `req_123`" in text
        assert "**>" in text
        assert "*Launch Preview*" in text
        assert "Start agent A" in text
        assert attachments == [preview_file]
        assert keyboard is not None
        buttons = keyboard.inline_keyboard
        assert len(buttons) == 1
        assert [button.text for button in buttons[0]] == [
            "✅ Approve",
            "❌ Reject",
            "💬 Feedback",
        ]
        assert buttons[0][0].callback_data == "launch:abcd1234:approve"
        assert buttons[0][1].callback_data == "launch:abcd1234:reject"
        assert buttons[0][2].callback_data == "launch:abcd1234:feedback"

        Path(preview_file).unlink()

    def test_missing_preview_file_still_attaches_path(self):
        n = _make_notification(
            action="LaunchApproval",
            sender="launch",
            notes=["Launch approval requested: 1 slot", "Source: cli"],
            files=["/nonexistent/launch_preview.md"],
            action_data={"slot_count": "1", "source_surface": "cli"},
        )
        text, keyboard, attachments = format_notification(n)

        assert "Launch Approval" in text
        assert "*Slots:* 1 slot" in text
        assert keyboard is not None
        assert attachments == ["/nonexistent/launch_preview.md"]


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

    def test_multi_question_numbering(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            request_file = Path(tmpdir) / "question_request.json"
            request_data = {
                "questions": [
                    {"question": "First?", "options": [{"label": "A"}]},
                    {"question": "Second?", "options": [{"label": "B"}]},
                ]
            }
            request_file.write_text(json.dumps(request_data))

            n = _make_notification(
                action="UserQuestion",
                sender="question",
                notes=["Claude is asking a question"],
                action_data={"response_dir": tmpdir, "session_id": "s1"},
            )
            text, keyboard, _ = format_notification(n)

        assert "Question 1 of 2" in text
        assert "First?" in text
        assert keyboard is not None
        assert keyboard.inline_keyboard[0][0].text == "A"

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


class TestQuestionRenderingHelpers:
    def test_multi_select_buttons_include_checks_and_submit(self):
        text, keyboard = render_question_message(
            {
                "question": "Which DB?",
                "options": [{"label": "PostgreSQL"}, {"label": "SQLite"}],
                "multiSelect": True,
            },
            index=1,
            total=3,
            selected=["PostgreSQL"],
            prefix="abcd1234",
        )

        assert "Question 2 of 3" in text
        assert "Which DB?" in text
        buttons = keyboard.inline_keyboard
        assert buttons[0][0].text == "☑️ PostgreSQL"
        assert buttons[1][0].text == "⬜ SQLite"
        assert buttons[2][0].text == "✅ Submit"
        assert buttons[2][1].text == "💬 Custom"
        assert buttons[2][0].callback_data == "question:abcd1234:submit"

    def test_answered_question_escapes_summary_and_question(self):
        text = format_answered_question(
            {"question": "Use v2.0?", "header": "API"},
            index=1,
            total=2,
            selected=["REST"],
            custom_feedback=None,
        )

        assert "Question 2 of 2" in text
        assert "REST" in text
        assert "Use v2\\.0?" in text

    def test_completion_summary_numbers_answers(self):
        text = format_questions_complete(
            [
                {"selected": ["PostgreSQL", "SQLite"], "custom_feedback": None},
                {"selected": ["Other"], "custom_feedback": "Use v2.0"},
            ]
        )

        assert "All 2 questions answered" in text
        assert "1\\. PostgreSQL, SQLite" in text
        assert '2\\. "Use v2\\.0" \\(custom\\)' in text


class TestFormatWorkflowComplete:
    def test_no_keyboard(self):
        n = _make_notification(
            sender="crs",
            notes=["Workflow completed successfully"],
        )
        text, keyboard, attachments = format_notification(n)

        assert "Complete" in text
        assert keyboard is None
        assert attachments == []

    def test_includes_agent_name(self):
        n = _make_notification(
            sender="user-agent",
            notes=["Agent completed: my-workflow"],
            action_data={"agent_name": "c"},
        )
        text, keyboard, attachments = format_notification(n)

        assert "Complete" in text
        assert "_@c_" in text
        assert keyboard is not None
        button = keyboard.inline_keyboard[0][0]
        assert button.text == "🍴 Fork"
        assert button.copy_text is not None
        assert button.copy_text.text == "#fork:c "

    def test_includes_bead_display_and_fork_button(self):
        n = _make_notification(
            sender="user-agent",
            notes=["Agent completed: my-workflow"],
            action_data={
                "agent_name": "sase-x.3",
                "bead_display": "sase-x.3 - Fix the thing",
            },
        )
        text, keyboard, _ = format_notification(n)

        assert "*Bead:* sase\\-x\\.3 \\- Fix the thing" in text
        assert text.index("_@sase\\-x\\.3_") < text.index("*Bead:*")
        assert text.index("*Bead:*") < text.index("Agent completed")
        assert keyboard is not None
        button = keyboard.inline_keyboard[0][0]
        assert button.copy_text is not None
        assert button.copy_text.text == "#fork:sase-x.3 "

    def test_humanizes_visible_text_and_copy_vcs_but_keeps_agent_ref_raw(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            formatting,
            "display_cl_name",
            lambda name: "SASE Core_task" if name == "sase_task" else name,
        )
        monkeypatch.setattr(
            formatting,
            "display_cl_names_in_text",
            lambda text: text.replace("gh_sase-org__sase", "sase").replace(
                "sase_task", "SASE Core_task"
            ),
        )
        monkeypatch.setattr(
            formatting,
            "display_vcs_refs_in_text",
            lambda text: text.replace("gh_sase-org__sase", "sase"),
        )
        n = _make_notification(
            sender="user-agent",
            notes=["Agent completed: sase_task"],
            action_data={
                "agent_name": "sase_task",
                "bead_display": "sase_task - Fix the thing",
                "prompt": "#gh:gh_sase-org__sase Continue sase_task",
                "cl_name": "gh_sase-org__sase_foo",
            },
        )

        from unittest.mock import patch

        with patch(
            "sase.xprompt.extract_vcs_workflow_tag",
            return_value="#gh:gh_sase-org__sase ",
        ):
            text, keyboard, _ = format_notification(n)

        assert "_@SASE Core\\_task_" in text
        assert "*Bead:* SASE Core\\_task \\- Fix the thing" in text
        assert "Agent completed: SASE Core\\_task" in text
        assert "\\#gh:sase Continue SASE Core\\_task" in text
        assert keyboard is not None
        button = keyboard.inline_keyboard[0][0]
        assert button.copy_text is not None
        assert button.copy_text.text == "#gh:sase_foo #fork:sase_task "

    def test_includes_runtime_without_changing_existing_fields(self):
        from unittest.mock import patch

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as png:
            png.write(b"\x89PNG\r\n\x1a\n")
            png_file = png.name

        n = _make_notification(
            sender="user-agent",
            notes=["CLAUDE(opus) completed: my-workflow"],
            files=[png_file],
            action_data={
                "agent_name": "sase-x.3",
                "bead_display": "sase-x.3 - Fix the thing",
                "llm_provider": "claude",
                "model": "opus",
                "prompt": "#gh:sase Fix the bug",
                "cl_name": "sase_foobar_1",
                "runtime": "4m32s",
            },
        )
        with patch(
            "sase.xprompt.extract_vcs_workflow_tag",
            return_value="#gh:sase ",
        ):
            text, keyboard, attachments = format_notification(n)

        assert "CLAUDE\\(opus\\) Complete" in text
        assert "*Bead:* sase\\-x\\.3 \\- Fix the thing" in text
        assert "*Runtime:* 4m32s" in text
        assert "📝 *Prompt:*\n\\#gh:sase Fix the bug" in text
        assert text.index("*Bead:*") < text.index("*Runtime:*")
        assert text.index("*Runtime:*") < text.index("CLAUDE\\(opus\\) completed")
        assert attachments == [png_file]
        assert keyboard is not None
        button = keyboard.inline_keyboard[0][0]
        assert button.text == "🍴 Fork"
        assert button.copy_text is not None
        assert button.copy_text.text == "#gh:sase_foobar_1 #fork:sase-x.3 "

        Path(png_file).unlink()

    def test_omits_bead_display_line_when_absent(self):
        n = _make_notification(
            sender="user-agent",
            notes=["Agent completed: my-workflow"],
            action_data={"agent_name": "sase-x.3"},
        )
        text, _, _ = format_notification(n)

        assert "*Bead:*" not in text

    def test_shows_provider_model_label(self):
        n = _make_notification(
            sender="user-agent",
            notes=["CLAUDE(opus) completed: my-workflow"],
            action_data={
                "agent_name": "c",
                "llm_provider": "claude",
                "model": "opus",
            },
        )
        text, _, _ = format_notification(n)

        assert "CLAUDE\\(opus\\) Complete" in text
        assert "_@c_" in text

    def test_diff_icon_when_diff_present(self):
        with tempfile.NamedTemporaryFile(suffix=".diff", delete=False) as f:
            f.write(b"diff --git a/foo.py b/foo.py\n")
            diff_file = f.name

        n = _make_notification(
            sender="user-agent",
            notes=["Agent completed: my-workflow"],
            files=[diff_file],
        )
        text, _, _ = format_notification(n)
        assert "✅✏️" in text
        assert "Complete" in text

        Path(diff_file).unlink()

    def test_no_diff_icon_without_diff(self):
        n = _make_notification(
            sender="user-agent",
            notes=["Agent completed: my-workflow"],
        )
        text, _, _ = format_notification(n)
        assert text.startswith("✅ ")
        assert "✏️" not in text

    def test_fork_uses_cl_name_over_project(self):
        from unittest.mock import patch

        n = _make_notification(
            sender="user-agent",
            notes=["Agent completed: my-workflow"],
            action_data={
                "agent_name": "c",
                "prompt": "#gh:sase Fix the bug",
                "cl_name": "sase_foobar_1",
            },
        )
        with patch(
            "sase.xprompt.extract_vcs_workflow_tag",
            return_value="#gh:sase ",
        ):
            _, keyboard, _ = format_notification(n)

        assert keyboard is not None
        button = keyboard.inline_keyboard[0][0]
        assert button.copy_text is not None
        assert button.copy_text.text == "#gh:sase_foobar_1 #fork:c "

    def test_fork_without_cl_name_uses_original_tag(self):
        from unittest.mock import patch

        n = _make_notification(
            sender="user-agent",
            notes=["Agent completed: my-workflow"],
            action_data={
                "agent_name": "c",
                "prompt": "#gh:sase Fix the bug",
            },
        )
        with patch(
            "sase.xprompt.extract_vcs_workflow_tag",
            return_value="#gh:sase ",
        ):
            _, keyboard, _ = format_notification(n)

        assert keyboard is not None
        button = keyboard.inline_keyboard[0][0]
        assert button.copy_text is not None
        assert button.copy_text.text == "#gh:sase #fork:c "

    def test_preserves_workflow_complete_media_attachments(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as png:
            png.write(b"\x89PNG\r\n\x1a\n")
            png_file = png.name
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as jpg:
            jpg.write(b"\xff\xd8\xff")
            jpg_file = jpg.name
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as gif:
            gif.write(b"GIF89a")
            gif_file = gif.name
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as mp4:
            mp4.write(b"\x00\x00\x00\x18ftypmp42")
            mp4_file = mp4.name

        n = _make_notification(
            sender="user-agent",
            notes=["Agent completed: media-update"],
            files=[png_file, jpg_file, gif_file, mp4_file, "/missing/not-attached.gif"],
        )
        _text, _keyboard, attachments = format_notification(n)

        assert attachments == [png_file, jpg_file, gif_file, mp4_file]

        Path(png_file).unlink()
        Path(jpg_file).unlink()
        Path(gif_file).unlink()
        Path(mp4_file).unlink()


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


class TestFormatImage:
    def test_with_existing_image_file(self):
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            image_file = f.name

        n = _make_notification(
            sender="image",
            notes=["Generated image with gemini-3-pro-image-preview"],
            files=[image_file],
            action_data={"model": "gemini-3-pro-image-preview"},
        )
        text, keyboard, attachments = format_notification(n)

        assert "Image Generated" in text
        assert "gemini\\-3\\-pro\\-image\\-preview" in text
        assert keyboard is None
        assert image_file in attachments

        Path(image_file).unlink()


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


class TestCodeBlocksToInline:
    def test_simple_code_block(self):
        text = "before\n```python\ndef foo():\n    pass\n```\nafter"
        result = _code_blocks_to_inline(text)
        assert "```" not in result
        assert "`def foo():`" in result
        assert "`    pass`" in result
        assert "before" in result
        assert "after" in result

    def test_code_block_without_language(self):
        text = "```\nsome code\n```"
        result = _code_blocks_to_inline(text)
        assert result == "`some code`"

    def test_no_code_blocks(self):
        text = "plain text with `inline code`"
        assert _code_blocks_to_inline(text) == text

    def test_multiple_code_blocks(self):
        text = "```py\na\n```\ntext\n```js\nb\n```"
        result = _code_blocks_to_inline(text)
        assert "```" not in result
        assert "`a`" in result
        assert "`b`" in result
        assert "text" in result

    def test_blank_lines_in_code(self):
        text = "```\nline1\n\nline2\n```"
        result = _code_blocks_to_inline(text)
        lines = result.split("\n")
        assert lines[0] == "`line1`"
        assert lines[1] == ""  # blank line preserved (not wrapped)
        assert lines[2] == "`line2`"


class TestExpandableBlockquote:
    def test_single_line(self):
        result = _wrap_expandable_blockquote("hello world")
        assert result == "**>hello world||"

    def test_multi_line(self):
        result = _wrap_expandable_blockquote("line one\nline two\nline three")
        assert result == "**>line one\n>line two\n>line three||"

    def test_empty_string(self):
        assert _wrap_expandable_blockquote("") == ""

    def test_code_block_at_end(self):
        # Code block closing ``` at end should put || on its own line
        result = _wrap_expandable_blockquote("some code\n```")
        assert result.endswith("\n>||")
        assert "```||" not in result

    def test_preserves_inner_formatting(self):
        result = _wrap_expandable_blockquote("*bold*\n`code`")
        assert result == "**>*bold*\n>`code`||"

    def test_blank_lines_use_zwsp(self):
        """Blank lines become zero-width spaces so Telegram keeps one blockquote."""
        result = _wrap_expandable_blockquote("header\n\nbody")
        assert result == "**>header\n>\u200b\n>body||"

    def test_consecutive_blank_lines_collapsed(self):
        result = _wrap_expandable_blockquote("a\n\n\n\nb")
        assert result == "**>a\n>\u200b\n>b||"

    def test_leading_trailing_blanks_stripped(self):
        result = _wrap_expandable_blockquote("\n\nfirst\nlast\n\n")
        assert result == "**>first\n>last||"

    def test_content_with_sections_stays_single_blockquote(self):
        """Simulates plan content with headers and code blocks."""
        content = "\n*Design*\n\nSome paragraph\n\n```yaml\ncode\n```\n\n*Decisions*\n"
        result = _wrap_expandable_blockquote(content)
        # Should be one continuous blockquote (single **> at start, single || at end)
        assert result.count("**>") == 1
        assert result.endswith(">*Decisions*||")


class TestFormatNotesText:
    def test_short_notes_plain(self):
        notes = ["Short note"]
        result = _format_notes_text(notes)
        assert "**>" not in result  # no blockquote
        assert "Short note" in result

    def test_long_notes_in_blockquote(self):
        long_note = "x" * (EXPANDABLE_THRESHOLD + 100)
        result = _format_notes_text([long_note])
        assert result.startswith("**>")
        assert "||" in result

    def test_very_long_notes_truncated_in_blockquote(self):
        long_note = "x" * (NOTES_TRUNCATION_THRESHOLD + 500)
        result = _format_notes_text([long_note])
        assert result.startswith("**>")
        assert "see TUI for full output" in result


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

    def test_long_notes_use_expandable_blockquote(self):
        long_note = "x" * (EXPANDABLE_THRESHOLD + 100)
        n = _make_notification(
            action="HITL",
            sender="hitl",
            notes=[long_note],
        )
        text, _, _ = format_notification(n)
        assert "**>" in text  # expandable blockquote marker

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
