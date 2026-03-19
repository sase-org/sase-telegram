# sase-telegram — Telegram Integration Chop for sase

[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy](https://img.shields.io/badge/type_checker-mypy-blue.svg)](https://mypy-lang.org/)
[![pytest](https://img.shields.io/badge/tests-pytest-blue.svg)](https://docs.pytest.org/)

## Overview

**sase-telegram** is a plugin for [sase](https://github.com/sase-org/sase) that provides two-way Telegram
integration. It sends notifications to Telegram when you're away from the TUI, and lets you respond to plan approvals,
HITL requests, user questions, and even launch new agents — all from Telegram.

## Installation

```bash
pip install sase-telegram
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv pip install sase-telegram
```

Requires `sase>=0.1.0` as a dependency (installed automatically).

## What's Included

### CLI Scripts

Installing sase-telegram adds the following commands:

| Command                    | Description                                                   |
| -------------------------- | ------------------------------------------------------------- |
| `sase_chop_tg_outbound`   | Send pending notifications to Telegram (supports `--dry-run`) |
| `sase_chop_tg_inbound`    | Poll Telegram for user responses and process them             |

### Supported Notification Types

| Type               | Telegram Behavior                                                             |
| ------------------ | ----------------------------------------------------------------------------- |
| Plan Approval      | Shows plan content with Approve / Reject / Epic / Feedback buttons            |
| HITL Request       | Shows request notes with Accept / Reject / Feedback buttons                   |
| User Question      | Shows question with dynamic option buttons + Custom input                     |
| Workflow Complete   | Sends a summary message with diff/chat attachments (crs, query, run-agent, etc.) |
| Error Digest       | Sends error summary with digest file attachments                              |
| Image Generated    | Sends model name and generated image inline                                   |

### Features

- **Activity-aware sending** — only sends when you've been inactive (configurable threshold)
- **Rate limiting** — sliding-window rate limiter prevents message flooding
- **Two-step feedback** — press a Feedback/Custom button, then type your response
- **Agent launching** — send a text message to spawn a new sase agent from Telegram
- **Photo/document handling** — send images to launch agents with visual context
- **Dot commands** — `.kill <name>`, `.list`, `.listx` for agent management from Telegram
- **PDF attachments** — long plans are converted to PDF via pandoc for readability
- **Large content handling** — auto-truncates long plans and notes; uses expandable blockquotes for medium content

## Configuration

### Credentials

| Source                                | Description                       |
| ------------------------------------- | --------------------------------- |
| `pass show telegram_sase_bot_token`   | Bot token (retrieved from `pass`) |
| `SASE_TELEGRAM_BOT_CHAT_ID`          | Chat ID to send messages to       |
| `SASE_TELEGRAM_BOT_USERNAME`         | Bot username                      |

### Environment Variables

| Variable                             | Default | Description                                       |
| ------------------------------------ | ------- | ------------------------------------------------- |
| `SASE_TELEGRAM_INACTIVE_SECONDS`     | `600`   | Seconds of inactivity before sending notifications |
| `SASE_TELEGRAM_RATE_LIMIT`           | `8/15`  | Rate limit as `max_messages/window_seconds`        |

## How It Works

The outbound script checks if you're inactive (via sase's TUI activity tracking), loads unsent notifications, formats
them as Telegram MarkdownV2 messages with inline keyboards, and sends them. Actionable notifications are saved as
pending actions. Long plans are converted to PDF attachments.

The inbound script polls Telegram for button presses, text messages, and photo/document uploads. It processes callbacks
(approve/reject/select), handles two-step feedback flows, writes response files for sase to pick up, and can launch
agents from arbitrary text messages or images. Dot commands (`.kill`, `.list`, `.listx`) provide agent management.

State files are stored under `~/.sase/telegram/` (pending actions, rate limit timestamps, update offsets, feedback
state).

## Requirements

- Python 3.12+
- [sase](https://github.com/sase-org/sase) >= 0.1.0
- [python-telegram-bot](https://python-telegram-bot.org/) >= 21.0
- [pass](https://www.passwordstore.org/) (for bot token retrieval)
- [pandoc](https://pandoc.org/) (optional, for PDF generation)

## Development

```bash
just install    # Install in editable mode with dev deps
just fmt        # Auto-format code
just lint       # Run ruff + mypy
just test       # Run tests
just check      # All checks (lint + test)
just build      # Build distribution packages
just clean      # Remove build artifacts
```

## Project Structure

```
src/sase_telegram/
├── __init__.py              # Package exports
├── callback_data.py         # Encode/decode inline keyboard callback data
├── credentials.py           # Bot token, chat ID, username retrieval
├── formatting.py            # Notification → Telegram MarkdownV2 formatting
├── inbound.py               # Inbound message/callback processing logic
├── outbound.py              # Activity detection and notification loading
├── pending_actions.py       # Persist pending actions to JSON
├── rate_limit.py            # Sliding-window rate limiter
├── telegram_client.py       # Sync wrapper around async python-telegram-bot
├── pdf_convert.py           # Markdown to PDF conversion via pandoc
├── pdf_style.css            # CSS styling for PDF output
└── scripts/
    ├── __init__.py                  # Script dispatch
    ├── sase_tg_outbound.py          # Outbound entry point
    └── sase_tg_inbound.py           # Inbound entry point
```

## License

MIT
