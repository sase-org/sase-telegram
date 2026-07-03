# Architecture

sase-telegram is structured as two CLI entry points (outbound and inbound) backed by a set of pure-logic modules for
formatting, state management, and Telegram API interaction.

## Module Overview

```
telegram_client.py ─── Sync wrapper around python-telegram-bot (retry/backoff, message splitting)
     ▲                     ▲
     │                     │
outbound.py            inbound.py
  Lock + HWM              Callback decode + two-step flows
     ▲                     ▲
     │                     │
formatting.py          pending_actions.py
  Notification → TG        Action persistence
     │
bead_format.py
  sase bead output → Markdown
     │
rate_limit.py
  Sliding-window throttle
     │
credentials.py
  Bot token (pass) + env vars
     │
callback_data.py
  64-byte encoded callback payloads
     │
pdf_convert.py
  Markdown → PDF via SASE renderer
```

## Data Flow

### Outbound

1. `sase_chop_tg_outbound` acquires an exclusive file lock (`outbound.lock`)
2. Loads unsent notifications using a high-water mark timestamp (`last_sent_ts`)
3. Formats each notification as MarkdownV2 with inline keyboards (`formatting.py`)
4. Sends via `telegram_client.py` (with rate limiting, retry/backoff, message splitting)
5. Saves actionable notifications (plan/HITL/question) to `pending_actions.json`
6. Advances the high-water mark only after successful delivery

### Inbound

1. `sase_chop_tg_inbound` fetches the currently pending Telegram updates
2. Saves the next Telegram offset before processing, so overlapping invocations use at-most-once delivery
3. Dispatches each update by type:
   - **Callback query** → decodes button press, handles notification responses or agent/bead callbacks
   - **Text message** → completes a matching two-step feedback flow, dispatches a slash command, or launches an agent
   - **Photo/image document** → downloads file, builds agent prompt with image path

## Key Design Decisions

- **Machine enable gate**: Both console-script wrappers (`scripts/__init__.py`) check `~/.sase/telegram_is_enabled`
  via `enabled.py` before doing anything else. If the flag is absent, the wrapper returns `0` immediately — before the
  lazy import of the entry-point module — so a disabled machine skips all heavy imports, network, and locks and stays
  silent. This lets the telegram lumberjack be configured globally while only flagged machines talk to Telegram.
- **Pure logic separation**: `inbound.py` contains no API calls — all logic is independently testable. The entry point
  script handles I/O and wiring.
- **High-water mark**: The outbound process tracks the timestamp of the last sent notification rather than individual
  notification IDs. It is initialized to "now" on first run to avoid dumping historical backlog, then advanced after
  each successful send.
- **Exclusive locking**: A file lock prevents concurrent outbound runs from sending the same notifications twice. This
  is important because outbound runs are triggered by a chop (periodic scheduler).
- **Rate limiting**: A sliding-window limiter (default 8 messages / 15 seconds) prevents hitting Telegram's flood
  limits. Timestamps are persisted to `rate_limit.json` so the window survives process restarts.
- **Two-step feedback isolation**: Feedback/custom flows are keyed by the originating Telegram message ID. A user can
  reply to the relevant message without overwriting other active feedback flows.
- **Parse mode fallback**: If MarkdownV2 rendering fails (malformed escaping), the client retries with plain text to
  ensure the message is delivered.
- **Shared PDF rendering**: `pdf_convert.py` delegates Markdown-to-PDF rendering to SASE's shared attachment renderer
  and applies the plugin stylesheet.
