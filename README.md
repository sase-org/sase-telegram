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

| Type               | Telegram Behavior                                                                           |
| ------------------ | ------------------------------------------------------------------------------------------- |
| Plan Approval      | Shows plan content with Approve / Reject / Epic / Feedback buttons                          |
| HITL Request       | Shows request notes with Accept / Reject / Feedback buttons                                 |
| User Question      | Shows question with dynamic option buttons + Custom input                                   |
| Workflow Complete   | Sends a summary message with diff/chat attachments and a Resume copy button                 |
| Agent Launched      | Shows provider/model label, workspace number, prompt snippet, and Resume / Wait / Kill buttons |
| Agent Killed        | Confirms termination with a Retry copy button to re-launch with the same prompt             |
| Error Digest       | Sends error summary with digest file attachments                                            |
| Image Generated    | Sends model name and generated image inline                                                 |

### Features

- **Activity-aware sending** — only sends when you've been inactive (configurable threshold)
- **Rate limiting** — sliding-window rate limiter prevents message flooding
- **Exclusive outbound locking** — prevents concurrent outbound runs from duplicating sends
- **Two-step feedback** — press a Feedback/Custom button, then type your response
- **Agent launching** — send a text message to spawn a new sase agent from Telegram
- **Auto-naming** — agents launched from Telegram automatically get assigned names
- **xprompt expansion** — agent prompts expand xprompt references (e.g. `#mentor`)
- **Multi-model directives** — use `%m(opus,sonnet)` to launch the same prompt across multiple models
- **Copy-text buttons** — Resume, Wait, Kill, and Retry buttons copy pre-filled prompts to your clipboard
- **Photo/document handling** — send images to launch agents with visual context
- **Dot commands** — `.kill <name>`, `.list`, `.listx` for agent management from Telegram
- **PDF attachments** — long plans are converted to PDF via pandoc for readability
- **Large content handling** — auto-truncates long plans and notes; uses expandable blockquotes for medium content
- **Message splitting** — messages exceeding Telegram's 4096-character limit are automatically split
- **Parse mode fallback** — falls back to plain text if MarkdownV2 rendering fails

## Configuration

### Credentials

| Source                                | Description                       |
| ------------------------------------- | --------------------------------- |
| `pass show telegram_sase_bot_token`   | Bot token (retrieved from `pass`) |
| `SASE_TELEGRAM_BOT_CHAT_ID`          | Chat ID to send messages to       |
| `SASE_TELEGRAM_BOT_USERNAME`         | Bot username                      |

### Environment Variables

| Variable                         | Default | Description                                |
| -------------------------------- | ------- | ------------------------------------------ |
| `SASE_TELEGRAM_RATE_LIMIT`       | `8/15`  | Rate limit as `max_messages/window_seconds` |

Note: Idle detection is handled by sase's TUI process (which writes an idle state file). The outbound script reads
this state — there is no separate inactivity threshold to configure in sase-telegram.

## How It Works

### Outbound

The outbound script acquires an exclusive file lock (to prevent concurrent runs from duplicating sends), checks if
you're inactive (via sase's TUI activity tracking), loads unsent notifications using a high-water mark timestamp, and
formats them as Telegram MarkdownV2 messages with inline keyboards. Long plans are wrapped in expandable blockquotes
or converted to PDF attachments. Chat file attachments are trimmed to just the response portion, with diffs embedded
into the response PDF. Actionable notifications (plan approvals, HITL requests, user questions) are saved as pending
actions for the inbound script to match against.

### Inbound

The inbound script polls Telegram for button presses, text messages, and photo/document uploads. It processes inline
keyboard callbacks (approve/reject/select/epic), handles two-step feedback flows (Feedback/Custom button followed by a
text message), and writes response files for sase to pick up. Text messages that don't complete a feedback flow are
dispatched as follows:

- **Dot commands** (`.kill <name>`, `.list`, `.listx`) — agent management
- **Bot commands** (`/start`, etc.) — silently ignored
- **Everything else** — launches a new sase agent with the message as the prompt

Agent launches expand xprompt references, support multi-model directives, and auto-assign names. Launch confirmation
messages include Resume, Wait, and Kill copy-text buttons for quick follow-up actions.

### State Files

State files are stored under `~/.sase/telegram/`:

| File                        | Purpose                                      |
| --------------------------- | -------------------------------------------- |
| `pending_actions.json`      | Actionable notifications awaiting response    |
| `rate_limit.json`           | Sliding-window send timestamps                |
| `update_offset.txt`         | Last processed Telegram update ID             |
| `awaiting_feedback.json`    | Active two-step feedback flow state           |
| `last_sent_ts`              | High-water mark for outbound notifications    |
| `outbound.lock`             | Exclusive lock for outbound process           |
| `outbound_debug.log`        | Diagnostic log for outbound sends             |
| `images/`                   | Downloaded photos from Telegram messages       |

## Requirements

- Python 3.12+
- [sase](https://github.com/sase-org/sase) >= 0.1.0
- [python-telegram-bot](https://python-telegram-bot.org/) >= 21.0
- [pass](https://www.passwordstore.org/) (for bot token retrieval)
- [pandoc](https://pandoc.org/) (optional, for PDF generation)
- PDF engine (optional, one of): [wkhtmltopdf](https://wkhtmltopdf.org/) (preferred), xelatex, or pdflatex

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
├── __init__.py              # Package init
├── callback_data.py         # Encode/decode inline keyboard callback data (64-byte limit)
├── credentials.py           # Bot token (via pass), chat ID and username (env vars)
├── formatting.py            # Notification → Telegram MarkdownV2 formatting + inline keyboards
├── inbound.py               # Pure logic: callback decoding, two-step feedback, photo handling
├── outbound.py              # High-water mark tracking, exclusive lock, unsent detection
├── pending_actions.py       # Persist pending actions to JSON (24h stale cleanup)
├── rate_limit.py            # Sliding-window rate limiter (configurable via env var)
├── telegram_client.py       # Sync wrapper with retry/backoff and message splitting
├── pdf_convert.py           # Markdown to PDF via pandoc (wkhtmltopdf → xelatex → pdflatex)
├── pdf_style.css            # CSS styling for wkhtmltopdf PDF output
└── scripts/
    ├── __init__.py           # Re-exports inbound_main and outbound_main
    ├── sase_tg_outbound.py   # Outbound entry point (--dry-run, --context)
    └── sase_tg_inbound.py    # Inbound entry point (--once, --context)
```

## License

MIT
