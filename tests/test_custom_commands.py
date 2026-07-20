"""Tests for custom Telegram command loading and execution."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from unittest.mock import patch

from sase_telegram import custom_commands
from sase_telegram.custom_commands import (
    CustomCommand,
    load_custom_commands,
    parse_command_output,
    run_custom_command,
)


def _command(*argv: str, timeout_seconds: int = 5) -> CustomCommand:
    return CustomCommand(
        name="tasks",
        description="Tasks dashboard",
        argv=tuple(argv),
        output="message",
        timeout_seconds=timeout_seconds,
    )


def test_load_custom_commands_normalizes_valid_config() -> None:
    config = {
        "telegram": {
            "commands": {
                "tasks": {
                    "description": " Tasks dashboard ",
                    "run": "~/bin/tg_cmd_tasks --note 'dash note.md'",
                    "output": "pdf",
                    "timeout": "2m",
                },
                "summary": {
                    "description": "Daily summary",
                    "run": "daily-summary",
                },
            }
        }
    }
    with (
        patch.object(custom_commands, "set_include_local_config") as set_local,
        patch.object(custom_commands, "load_merged_config", return_value=config),
    ):
        commands = load_custom_commands()

    set_local.assert_called_once_with(False)
    assert commands["tasks"] == CustomCommand(
        name="tasks",
        description="Tasks dashboard",
        argv=(str(Path("~/bin/tg_cmd_tasks").expanduser()), "--note", "dash note.md"),
        output="pdf",
        timeout_seconds=120,
    )
    assert commands["summary"].output == "message"
    assert commands["summary"].timeout_seconds == 60


def test_load_custom_commands_skips_invalid_and_reserved_entries(caplog) -> None:
    config = {
        "telegram": {
            "commands": {
                "good": {"description": "Good", "run": "true"},
                "list": {"description": "Shadow", "run": "true"},
                "show": {"description": "Shadow show", "run": "true"},
                "Bad": {"description": "Bad name", "run": "true"},
                "missing": {"description": "No run"},
                "quote": {"description": "Bad argv", "run": "'"},
                "mode": {
                    "description": "Bad mode",
                    "run": "true",
                    "output": [],
                },
                "slow": {
                    "description": "Bad timeout",
                    "run": "true",
                    "timeout": "1d",
                },
                "extra": {
                    "description": "Extra",
                    "run": "true",
                    "surprise": True,
                },
            }
        }
    }
    with (
        patch.object(custom_commands, "set_include_local_config"),
        patch.object(custom_commands, "load_merged_config", return_value=config),
    ):
        commands = load_custom_commands()

    assert set(commands) == {"good"}
    assert "reserved name" in caplog.text
    assert "invalid name" in caplog.text
    assert "invalid timeout" in caplog.text


def test_load_custom_commands_degrades_on_config_failure() -> None:
    with (
        patch.object(custom_commands, "set_include_local_config"),
        patch.object(
            custom_commands,
            "load_merged_config",
            side_effect=RuntimeError("bad config"),
        ),
    ):
        assert load_custom_commands() == {}


def test_run_custom_command_passes_raw_args_env_and_temp_cwd() -> None:
    script = (
        "import json, os, sys; "
        "print(json.dumps({'argv': sys.argv[1:], 'command': "
        "os.environ['SASE_TELEGRAM_COMMAND'], 'args': "
        "os.environ['SASE_TELEGRAM_COMMAND_ARGS'], 'cwd': os.getcwd()})); "
        "print('diagnostic', file=sys.stderr)"
    )
    args_text = "two words; $(not-a-shell)"
    result = run_custom_command(
        _command(sys.executable, "-c", script, "fixed argument"),
        args_text,
    )

    assert result.returncode == 0
    assert not result.timed_out
    assert result.stderr == "diagnostic\n"
    payload = json.loads(result.stdout)
    assert payload["argv"] == ["fixed argument", args_text]
    assert payload["command"] == "tasks"
    assert payload["args"] == args_text
    assert Path(payload["cwd"]).name.startswith("sase-tg-tasks-")
    assert not Path(payload["cwd"]).exists()


def test_run_custom_command_captures_failure() -> None:
    script = "import sys; print('bad news', file=sys.stderr); raise SystemExit(3)"
    result = run_custom_command(_command(sys.executable, "-c", script), "")

    assert result.returncode == 3
    assert result.stderr == "bad news\n"
    assert not result.timed_out


def test_run_custom_command_reports_timeout() -> None:
    result = run_custom_command(
        _command(
            sys.executable,
            "-c",
            "import time; time.sleep(10)",
            timeout_seconds=0,
        ),
        "",
    )

    assert result.timed_out
    assert result.returncode is None


def test_run_custom_command_reports_missing_executable() -> None:
    result = run_custom_command(_command("/definitely/not/a/command"), "")

    assert result.returncode == 127
    assert result.stderr


def test_parse_command_output_with_frontmatter() -> None:
    parsed = parse_command_output(
        "---\n"
        'caption: "📋 *Tasks* — 2 READY"\n'
        "filename: tasks_dashboard.pdf\n"
        "---\n\n"
        "# Tasks\n\n- one\n"
    )

    assert parsed.caption == "📋 *Tasks* — 2 READY"
    assert parsed.filename == "tasks_dashboard.pdf"
    assert parsed.body == "# Tasks\n\n- one\n"


def test_parse_command_output_without_frontmatter() -> None:
    parsed = parse_command_output("# Tasks\n")

    assert parsed.body == "# Tasks\n"
    assert parsed.caption is None
    assert parsed.filename is None


def test_parse_command_output_tolerates_malformed_frontmatter() -> None:
    parsed = parse_command_output("---\ncaption: [broken\n---\n\n# Tasks\n")

    assert parsed.body == "# Tasks\n"
    assert parsed.caption is None


def test_parse_command_output_keeps_unclosed_frontmatter_as_body() -> None:
    stdout = "---\ncaption: nope\n# Still content\n"
    assert parse_command_output(stdout).body == stdout
