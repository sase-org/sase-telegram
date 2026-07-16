"""Load and execute user-defined Telegram slash commands."""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

import yaml  # type: ignore[import-untyped]
from sase.config import load_merged_config
from sase.config.core import set_include_local_config

log = logging.getLogger(__name__)

_COMMAND_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_TIMEOUT_RE = re.compile(r"^(\d+)(s|m|h)$")
_TIMEOUT_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600}
_COMMAND_FIELDS = frozenset({"description", "run", "output", "timeout"})

# Built-ins and accepted aliases always win over user configuration.
RESERVED_COMMAND_NAMES = frozenset(
    {
        "bead",
        "beads",
        "changes",
        "fork",
        "kill",
        "list",
        "update",
        "xprompts",
    }
)


@dataclass(frozen=True)
class CustomCommand:
    """One validated custom Telegram command."""

    name: str
    description: str
    argv: tuple[str, ...]
    output: str
    timeout_seconds: int


@dataclass(frozen=True)
class CommandResult:
    """Captured result of running a custom command."""

    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False


@dataclass(frozen=True)
class ParsedCommandOutput:
    """Markdown body and optional delivery metadata parsed from stdout."""

    body: str
    caption: str | None = None
    filename: str | None = None


def _duration_seconds(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = _TIMEOUT_RE.fullmatch(value)
    if match is None:
        return None
    return int(match.group(1)) * _TIMEOUT_MULTIPLIERS[match.group(2)]


def _parse_custom_command(name: object, value: object) -> CustomCommand | None:
    if not isinstance(name, str) or _COMMAND_NAME_RE.fullmatch(name) is None:
        log.warning("Skipping custom Telegram command with invalid name: %r", name)
        return None
    if name in RESERVED_COMMAND_NAMES:
        log.warning("Skipping custom Telegram command with reserved name: %s", name)
        return None
    if not isinstance(value, dict):
        log.warning("Skipping custom Telegram command %s: config is not a map", name)
        return None

    unknown_fields = set(value) - _COMMAND_FIELDS
    if unknown_fields:
        log.warning(
            "Skipping custom Telegram command %s: unknown fields %s",
            name,
            ", ".join(sorted(str(field) for field in unknown_fields)),
        )
        return None

    description = value.get("description")
    if (
        not isinstance(description, str)
        or not description.strip()
        or not 1 <= len(description) <= 256
    ):
        log.warning("Skipping custom Telegram command %s: invalid description", name)
        return None

    run = value.get("run")
    if not isinstance(run, str) or not run.strip():
        log.warning("Skipping custom Telegram command %s: invalid run value", name)
        return None
    try:
        argv = shlex.split(run)
    except ValueError:
        log.warning(
            "Skipping custom Telegram command %s: run value cannot be parsed", name
        )
        return None
    if not argv:
        log.warning("Skipping custom Telegram command %s: run value is empty", name)
        return None
    argv[0] = os.path.expanduser(argv[0])

    output = value.get("output", "message")
    if not isinstance(output, str) or output not in {"message", "pdf"}:
        log.warning("Skipping custom Telegram command %s: invalid output mode", name)
        return None

    timeout_seconds = _duration_seconds(value.get("timeout", "60s"))
    if timeout_seconds is None:
        log.warning("Skipping custom Telegram command %s: invalid timeout", name)
        return None

    return CustomCommand(
        name=name,
        description=description.strip(),
        argv=tuple(argv),
        output=output,
        timeout_seconds=timeout_seconds,
    )


def load_custom_commands() -> dict[str, CustomCommand]:
    """Load valid custom commands from merged SASE configuration.

    Project-local configuration is deliberately disabled because the inbound chop
    must behave identically regardless of its process working directory. Invalid
    entries are logged and skipped so one bad command cannot break the poll.
    """
    try:
        set_include_local_config(False)
        config = load_merged_config()
    except Exception:
        log.warning("Failed to load custom Telegram commands", exc_info=True)
        return {}

    if not isinstance(config, dict):
        log.warning("Skipping custom Telegram commands: merged config is invalid")
        return {}
    telegram = config.get("telegram")
    if not isinstance(telegram, dict):
        log.warning("Skipping custom Telegram commands: telegram config is invalid")
        return {}
    values = telegram.get("commands", {})
    if not isinstance(values, dict):
        log.warning("Skipping custom Telegram commands: commands config is invalid")
        return {}

    commands: dict[str, CustomCommand] = {}
    for name, value in values.items():
        command = _parse_custom_command(name, value)
        if command is not None:
            commands[command.name] = command
    return commands


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_custom_command(command: CustomCommand, args_text: str) -> CommandResult:
    """Run *command* without a shell in an isolated temporary directory."""
    argv = list(command.argv)
    if args_text:
        argv.append(args_text)

    env = os.environ.copy()
    env["SASE_TELEGRAM_COMMAND"] = command.name
    env["SASE_TELEGRAM_COMMAND_ARGS"] = args_text

    with tempfile.TemporaryDirectory(prefix=f"sase-tg-{command.name}-") as cwd:
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=command.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                stdout=_timeout_text(exc.stdout),
                stderr=_timeout_text(exc.stderr),
                returncode=None,
                timed_out=True,
            )
        except (OSError, ValueError) as exc:
            return CommandResult(stdout="", stderr=str(exc), returncode=127)

    return CommandResult(
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )


def parse_command_output(stdout: str) -> ParsedCommandOutput:
    """Parse optional YAML frontmatter from a command's Markdown stdout."""
    lines = stdout.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return ParsedCommandOutput(body=stdout)

    closing_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        ),
        None,
    )
    if closing_index is None:
        return ParsedCommandOutput(body=stdout)

    body = "".join(lines[closing_index + 1 :]).lstrip("\r\n")
    try:
        metadata: Any = yaml.safe_load("".join(lines[1:closing_index]))
    except yaml.YAMLError:
        log.warning("Ignoring malformed custom-command frontmatter")
        return ParsedCommandOutput(body=body)
    if not isinstance(metadata, dict):
        return ParsedCommandOutput(body=body)

    caption = metadata.get("caption")
    filename = metadata.get("filename")
    return ParsedCommandOutput(
        body=body,
        caption=caption.strip()
        if isinstance(caption, str) and caption.strip()
        else None,
        filename=(
            filename.strip() if isinstance(filename, str) and filename.strip() else None
        ),
    )
