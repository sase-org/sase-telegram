"""Slash-command router and bot command registration."""

from __future__ import annotations

from datetime import datetime
import hashlib
import html
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from sase_telegram import credentials, pdf_convert, telegram_client
from sase_telegram.custom_commands import (
    CommandResult,
    CustomCommand,
    parse_command_output,
    run_custom_command,
)
from sase_telegram.formatting import markdown_to_telegram_v2
from sase_telegram.inbound_handlers.update_command import _handle_update_command
from sase_telegram.inbound_handlers.xprompt_commands import (
    _handle_changes_command,
    _handle_xprompts_command,
)
from sase_telegram.inbound_handlers.beads import _handle_bead_command
from sase_telegram.inbound_handlers.agent_actions import (
    _handle_kill_command,
    _handle_fork_command,
)
from sase_telegram.inbound_handlers.agent_list import _handle_list_command
from sase_telegram.inbound_handlers.agent_show import _handle_show_command

import logging

log = logging.getLogger(__name__)


# File-based cache for set_my_commands to avoid Telegram rate limits.
_COMMANDS_REGISTERED_PATH = (
    Path.home() / ".sase" / "telegram" / "commands_registered_ts"
)


_COMMANDS_REGISTER_INTERVAL = 3600  # re-register once per hour


_CUSTOM_COMMAND_CAPTION_LIMIT = 1024


_CUSTOM_COMMAND_STDERR_LIMIT = 1000


def _custom_command_caption(caption: str) -> str:
    """Convert a caption to MarkdownV2 within Telegram's document limit."""
    converted = markdown_to_telegram_v2(caption)
    if len(converted) <= _CUSTOM_COMMAND_CAPTION_LIMIT:
        return converted

    raw_prefix = caption[: _CUSTOM_COMMAND_CAPTION_LIMIT - 1]
    while raw_prefix:
        converted = markdown_to_telegram_v2(raw_prefix.rstrip())
        overflow = len(converted) + 1 - _CUSTOM_COMMAND_CAPTION_LIMIT
        if overflow <= 0:
            return converted + "…"
        raw_prefix = raw_prefix[: -max(1, overflow)]
    return "…"


def _custom_command_pdf_filename(command: CustomCommand, requested: str | None) -> str:
    default = f"{command.name}_{datetime.now().date().isoformat()}.pdf"
    if requested is None:
        return default

    candidate = Path(requested.replace("\x00", "")).name.strip(" .")
    stem = Path(candidate).stem if candidate else ""
    stem = re.sub(r"[^\w .-]+", "_", stem).strip(" .")
    if not stem:
        return default
    return f"{stem[:120]}.pdf"


def _send_custom_command_error(command: CustomCommand, result: CommandResult) -> None:
    chat_id = credentials.get_chat_id()
    stderr = result.stderr.strip()
    if len(stderr) > _CUSTOM_COMMAND_STDERR_LIMIT:
        stderr = "…" + stderr[-(_CUSTOM_COMMAND_STDERR_LIMIT - 1) :]

    exit_code = result.returncode if result.returncode is not None else "unknown"
    text = (
        f"⚠️ <code>/{html.escape(command.name)}</code> failed "
        f"(exit {html.escape(str(exit_code))})"
    )
    if stderr:
        text += f"\n<blockquote expandable>{html.escape(stderr)}</blockquote>"
    telegram_client.send_message(chat_id, text, parse_mode="HTML")


def _send_custom_markdown(chat_id: str, markdown: str) -> None:
    telegram_client.send_message(
        chat_id,
        markdown_to_telegram_v2(markdown),
        parse_mode="MarkdownV2",
    )


def _handle_custom_command(command: CustomCommand, args_text: str) -> None:
    """Run one custom command and deliver its Markdown stdout."""
    chat_id = credentials.get_chat_id()
    if command.output == "pdf":
        _send_custom_markdown(chat_id, f"⏳ Running `/{command.name}`…")

    result = run_custom_command(command, args_text)
    if result.timed_out:
        _send_custom_markdown(
            chat_id,
            f"⏱ `/{command.name}` timed out after {command.timeout_seconds}s",
        )
        return
    if result.returncode != 0:
        _send_custom_command_error(command, result)
        return

    output = parse_command_output(result.stdout)
    if not output.body.strip():
        _send_custom_markdown(
            chat_id,
            f"🫙 `/{command.name}` produced no output.",
        )
        return

    if command.output == "message":
        _send_custom_markdown(chat_id, output.body)
        return

    filename = _custom_command_pdf_filename(command, output.filename)
    caption = _custom_command_caption(output.caption or f"📄 {command.description}")
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"sase-tg-{command.name}-pdf-"
        ) as tmpdir:
            markdown_path = Path(tmpdir) / f"{Path(filename).stem}.md"
            markdown_path.write_text(output.body, encoding="utf-8")
            rendered_path = pdf_convert.md_to_pdf(str(markdown_path))
            if rendered_path is None or not Path(rendered_path).is_file():
                raise RuntimeError("PDF renderer did not produce a file")
            telegram_client.send_document(
                chat_id,
                rendered_path,
                caption=caption,
                parse_mode="MarkdownV2",
                filename=filename,
            )
    except Exception:
        log.warning(
            "Failed to convert custom Telegram command /%s output to PDF",
            command.name,
            exc_info=True,
        )
        _send_custom_markdown(
            chat_id,
            "⚠️ PDF conversion failed; sending Markdown instead.\n\n" + output.body,
        )


def _handle_command(
    text: str,
    message: Any | None = None,
    custom_commands: dict[str, CustomCommand] | None = None,
) -> None:
    """Dispatch a slash command (e.g. '/kill agent') to the appropriate handler."""
    parts = text.split(None, 1)
    command = parts[0][1:].split("@")[0].lower()  # strip prefix and @bot suffix
    args = parts[1] if len(parts) > 1 else ""

    if command == "kill":
        _handle_kill_command(args)
    elif command == "list":
        _handle_list_command(args)
    elif command == "show":
        _handle_show_command(args, message=message)
    elif command == "fork":
        _handle_fork_command()
    elif command == "changes":
        _handle_changes_command(args)
    elif command == "xprompts":
        _handle_xprompts_command()
    elif command in {"bead", "beads"}:
        _handle_bead_command(args, message=message)
    elif command == "update":
        _handle_update_command()
    elif custom_commands and command in custom_commands:
        _handle_custom_command(custom_commands[command], args)


_SLASH_COMMANDS = [
    ("kill", "Terminate a running agent"),
    ("list", "Show agents; supports all, name, or project"),
    ("show", "Show an agent, clan, session, or tribe"),
    ("fork", "Copy fork text for an agent"),
    ("changes", "Copy Patch workflow tags"),
    ("xprompts", "Export the xprompts catalog as a PDF"),
    ("bead", "Show a bead's details as Markdown"),
    ("update", "Update SASE and restart axe"),
]


def _slash_commands_fingerprint(commands: list[tuple[str, str]] | None = None) -> str:
    """Return a stable fingerprint for the registered Telegram command list."""
    payload = json.dumps(
        commands if commands is not None else _SLASH_COMMANDS,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _registered_slash_commands(
    custom_commands: dict[str, CustomCommand] | None = None,
) -> list[tuple[str, str]]:
    commands: list[tuple[str, str]] = []
    if custom_commands:
        commands.extend(
            (name, command.description)
            for name, command in sorted(custom_commands.items())
        )
    commands.extend(_SLASH_COMMANDS)
    return commands


def _commands_registration_is_current(
    now: float,
    commands: list[tuple[str, str]] | None = None,
) -> bool:
    try:
        payload = json.loads(_COMMANDS_REGISTERED_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return False

    if not isinstance(payload, dict) or payload.get("version") != 1:
        return False
    if payload.get("fingerprint") != _slash_commands_fingerprint(commands):
        return False
    try:
        last_ts = float(payload["timestamp"])
    except (KeyError, TypeError, ValueError):
        return False
    return now - last_ts < _COMMANDS_REGISTER_INTERVAL


def _register_commands_if_needed(
    custom_commands: dict[str, CustomCommand] | None = None,
) -> None:
    """Register slash commands with Telegram, at most once per hour.

    Uses a file-based timestamp and command fingerprint to avoid calling
    ``set_my_commands`` on every tick (every 5 seconds), while still picking up
    command list changes before the normal hourly interval expires.
    """
    now = time.time()
    commands = _registered_slash_commands(custom_commands)
    if _COMMANDS_REGISTERED_PATH.exists() and _commands_registration_is_current(
        now, commands
    ):
        return

    try:
        if not telegram_client.set_my_commands(commands):
            log.warning(
                "Failed to register slash commands: Telegram returned False "
                "(will retry later)"
            )
            return
        _COMMANDS_REGISTERED_PATH.parent.mkdir(parents=True, exist_ok=True)
        _COMMANDS_REGISTERED_PATH.write_text(
            json.dumps(
                {
                    "version": 1,
                    "timestamp": now,
                    "fingerprint": _slash_commands_fingerprint(commands),
                },
                sort_keys=True,
            )
        )
        log.info("Registered Telegram slash commands")
    except Exception:
        log.warning(
            "Failed to register slash commands (will retry later)", exc_info=True
        )
