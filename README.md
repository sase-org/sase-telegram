# sase-telegram — Telegram Integration Chop for sase

[![PyPI](https://img.shields.io/pypi/v/sase-telegram?logo=pypi&logoColor=white)](https://pypi.org/project/sase-telegram/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy](https://img.shields.io/badge/type_checker-mypy-blue.svg)](https://mypy-lang.org/)
[![pytest](https://img.shields.io/badge/tests-pytest-blue.svg)](https://docs.pytest.org/)

## Overview

**sase-telegram** is a plugin for [sase](https://github.com/sase-org/sase) that provides two-way Telegram
integration. It sends SASE notifications to Telegram, and lets you respond to plan approvals, HITL requests, user
questions, and even launch new agents — all from Telegram.

## Installation

For a managed SASE install, install `sase-telegram` into the same `uv tool` environment as `sase` so its CLI scripts
are available to SASE's chop automation.

### Recommended: SASE Admin Center Updates tab

If SASE is already installed with `uv tool install sase`, open `sase ace`, press `#` for the SASE Admin Center, then go
to the **Updates** tab (`5`, or `[` / `]`). Highlight `sase-telegram` in the plugin list (`j` / `k`, or `/` to filter),
press `i` to install, and confirm the preview modal. The preview shows the exact `uv` command and resolved package set;
the install runs as a tracked background task and is discovered on the next `sase` run.

See the core SASE docs for the
[Updates tab](https://github.com/sase-org/sase/blob/master/docs/configuration.md#updates-tab) and
[`sase plugin` commands](https://github.com/sase-org/sase/blob/master/docs/plugins.md).

### Alternative: install SASE and the plugin together

```bash
uv tool install sase --with sase-telegram
```

Repeat `--with` for additional plugins, for example `--with sase-telegram --with sase-github`. Add `--force` to replace
an existing tool install.

### Equivalent CLI for an existing install

```bash
sase plugin install telegram
```

`pip install sase-telegram` is only an escape hatch for non-managed or library-style environments. It is not the normal
path for a `uv tool`-managed SASE command.

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
| Plan Approval      | Shows plan content with Tale / ✅ Approve / Epic / Legend / Reject / Feedback buttons       |
| HITL Request       | Shows request notes with Accept / Reject / Feedback buttons                                 |
| User Question      | Shows question with dynamic option buttons + Custom input                                   |
| Workflow Complete   | Sends a summary message with diff/chat/media attachments and a Fork copy button           |
| Agent Launched      | Shows provider/model label, workspace number, prompt snippet, and Fork / Wait / Kill / Retry buttons |
| Agent Killed        | Confirms termination with a Redo copy button to re-launch with the same prompt              |
| Error Digest       | Sends error summary with digest file attachments                                            |
| Image Generated    | Sends model name and generated image inline                                                 |

### Features

- **Outbound notifications** — sends unread, non-silent SASE notifications to Telegram
- **Rate limiting** — sliding-window rate limiter prevents message flooding
- **Exclusive outbound locking** — prevents concurrent outbound runs from duplicating sends
- **Two-step feedback** — press a Feedback/Custom button, then type your response
- **Agent launching** — send a text message to spawn a new sase agent from Telegram
- **Auto-naming** — agents launched from Telegram automatically get assigned names
- **xprompt expansion** — agent prompts expand xprompt references (e.g. `#mentor`)
- **Multi-model directives** — use `%{%m:opus | %m:sonnet}` to launch the same prompt across multiple models
- **Copy-text buttons** — Fork, Wait, Retry, Redo, plan, and ChangeSpec buttons copy pre-filled text to your clipboard
- **Photo/document handling** — send photos, albums, or image documents to launch agents with visual context
- **Slash commands** — `/list`, `/kill [<name>]`, `/fork`, `/changes [project]`, `/xprompts`, `/bead [<id>]`, `/update` for agent management, ChangeSpec, xprompt, bead, and SASE update workflows from Telegram (registered with `set_my_commands` so they show up in the chat input UI)
- **Media attachments** — workflow completion attachments route static images, GIFs, videos, and PDFs through the
  matching Telegram send method, with GIF/video document fallback
- **PDF attachments** — Markdown attachments are rendered to PDF through the shared SASE renderer when possible
- **Large content handling** — auto-truncates long plans and notes; uses expandable blockquotes for medium content
- **Message splitting** — messages exceeding Telegram's 4096-character limit are automatically split
- **Parse mode fallback** — falls back to plain text if MarkdownV2 rendering fails

## Configuration

### Machine Enable Flag

The chops are **no-ops unless `~/.sase/telegram_is_enabled` exists**. When the flag file is absent,
`sase_chop_tg_outbound` and `sase_chop_tg_inbound` exit immediately with status `0`, print nothing, and skip all heavy
imports, network calls, and locks. This lets the telegram lumberjack be configured globally (on every machine) while
only explicitly-enabled machines actually talk to Telegram.

Enable a machine with:

```bash
touch ~/.sase/telegram_is_enabled
```

### Credentials

Bot token sources are checked in order:

| Source                              | Description                                      |
| ----------------------------------- | ------------------------------------------------ |
| `SASE_TELEGRAM_BOT_TOKEN`           | Bot token from the environment                   |
| `~/.sase/telegram_bot_token`        | Bot token file; must not be group/other-readable |
| `pass show telegram_sase_bot_token` | Bot token from `pass`                            |

The chat target and bot username are required separately:

| Source                       | Description                 |
| ---------------------------- | --------------------------- |
| `SASE_TELEGRAM_BOT_CHAT_ID`  | Chat ID to send messages to |
| `SASE_TELEGRAM_BOT_USERNAME` | Bot username                |

### Environment Variables

| Variable                                 | Default | Description |
| ---------------------------------------- | ------- | ----------- |
| `SASE_TELEGRAM_RATE_LIMIT`               | `8/15`  | Rate limit as `max_messages/window_seconds` |
| `SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED`  | unset   | When present with any value, inbound callbacks, feedback, and slash commands still work, but plain text/photo/image-document messages do not launch agents. |

## How It Works

### Outbound

The outbound script acquires an exclusive file lock (to prevent concurrent runs from duplicating sends), loads unsent
notifications using a high-water mark timestamp, and formats them as Telegram MarkdownV2 messages with inline
keyboards. Long plans are wrapped in expandable blockquotes or truncated and paired with a document attachment. Chat
file attachments are trimmed to just the response portion, with commit messages and diffs embedded into the response
PDF when possible. Static images are sent as photos, GIFs as animations, videos as videos, and PDFs as documents; GIFs
and videos retry as documents if Telegram rejects inline media delivery. Actionable notifications (plan approvals, HITL
requests, user questions) are saved as pending actions for the inbound script to match against.

### Inbound

The inbound script fetches Telegram button presses, text messages, and photo/document uploads. It processes inline
keyboard callbacks (approve/run/reject/select/epic/legend, agent controls, and bead pickers), handles two-step feedback flows
(Feedback/Custom button followed by a reply or single active text response), and writes response files for sase to pick
up. Text messages that don't complete a feedback flow are dispatched as follows:

- **Slash commands** (`/list`, `/kill [<name>]`, `/fork`, `/changes [project]`, `/xprompts`, `/bead [<id>]`, `/update`) — agent management, ChangeSpec workflow tag lookup, xprompt catalog export, bead inspection, and SASE updates
- **Other slash commands** (`/start`, unknown commands, etc.) — silently ignored
- **Everything else** — launches a new sase agent with the message as the prompt

Agent launches expand xprompt references, support multi-model directives, and auto-assign names. Launch confirmation
messages include Fork and Wait copy-text buttons plus Kill and Retry controls for quick follow-up actions.

Photos and image documents launch agents with prompts that reference the downloaded local image path. Telegram albums
are staged briefly and then launched as one prompt containing a numbered list of all downloaded image paths, so prompt
image discovery can surface every file later.

Set `SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED` on hosts that should process Telegram callbacks, feedback, and slash commands
without launching new agents from free-form text, photos, image documents, or albums. The check is presence-based, so an
empty value still disables launches; ignored launch messages are logged without a Telegram acknowledgement.

`/changes` lists active ChangeSpecs, excluding Submitted, Archived, and Reverted entries. Use `/changes <project>` to
filter by exact project name. Each result has a copy-text button for the bare workflow tag, such as `#hg:foobar`.

`/bead` lists active beads across all known SASE projects as picker buttons. `/bead <id>` runs `sase bead show <id>`,
converts the output to Telegram MarkdownV2, and sends the bead details in chat. If `SASE_TELEGRAM_BEAD_PROJECT` is set,
bead commands are narrowed to that project workspace. Without the override, detail lookup searches known projects and
prefers the chat-scoped project remembered from recent Telegram launch context.

`/update` starts the shared SASE chat update worker in a detached process and immediately replies with the worker log
path. The worker runs the built-in `sase update --json` engine, using SASE's normal managed-vs-dev update routing, then
ensures axe is running afterward. After the worker exits, the next inbound run sends a completion message that reports
the worker's update summary or the fallback failure exit code and includes the worker log path.

Slash command registration is cached in `~/.sase/telegram/commands_registered_ts`, but the cache includes a fingerprint
of the command list so renamed commands are registered immediately after deployment.

### State Files

State files are stored under `~/.sase/telegram/`:

| File                        | Purpose                                      |
| --------------------------- | -------------------------------------------- |
| `pending_actions.json`      | Pending notification, kill, retry, and bead callback context |
| `rate_limit.json`           | Sliding-window send timestamps                |
| `update_offset.txt`         | Last processed Telegram update ID             |
| `awaiting_feedback.json`    | Active two-step feedback flow state, keyed by Telegram message |
| `media_groups.json`         | Staged Telegram photo/image-document albums waiting for the quiet window |
| `last_sent_ts`              | High-water mark for outbound notifications    |
| `outbound.lock`             | Exclusive lock for outbound process           |
| `outbound_debug.log`        | Diagnostic log for outbound sends             |
| `commands_registered_ts`    | Cached timestamp and fingerprint for Telegram slash command registration |
| `images/`                   | Downloaded photos and image documents from Telegram messages |

## Requirements

- Python 3.12+
- [sase](https://github.com/sase-org/sase) >= 0.1.0
- [python-telegram-bot](https://python-telegram-bot.org/) >= 21.0
- [pass](https://www.passwordstore.org/) (for bot token retrieval)
- PDF tooling supported by SASE's shared Markdown renderer (optional, for PDF generation)

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
├── credentials.py           # Bot token sources, chat ID, and username
├── formatting.py            # Notification → Telegram MarkdownV2 formatting + inline keyboards
├── inbound.py               # Pure logic: callback decoding, two-step feedback, photo handling
├── bead_format.py           # Convert `sase bead` output to Markdown for Telegram rendering
├── outbound.py              # High-water mark tracking, exclusive lock, unsent detection
├── pending_actions.py       # Persist pending actions to JSON (24h stale cleanup)
├── rate_limit.py            # Sliding-window rate limiter (configurable via env var)
├── telegram_client.py       # Sync wrapper with retry/backoff and message splitting
├── pdf_convert.py           # Markdown to PDF via SASE's shared renderer
├── pdf_style.css            # CSS styling for rendered PDF output
└── scripts/
    ├── __init__.py           # Re-exports inbound_main and outbound_main
    ├── sase_tg_outbound.py   # Outbound entry point (--dry-run, --context)
    └── sase_tg_inbound.py    # Inbound entry point (--once, --context)
```

## License

MIT
