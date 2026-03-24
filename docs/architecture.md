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
  Markdown → PDF via pandoc
```

## Data Flow

### Outbound

1. `sase_chop_tg_outbound` acquires an exclusive file lock (`outbound.lock`)
2. Checks TUI activity state — skips if user is active
3. Loads unsent notifications using a high-water mark timestamp (`last_sent_ts`)
4. Formats each notification as MarkdownV2 with inline keyboards (`formatting.py`)
5. Sends via `telegram_client.py` (with rate limiting, retry/backoff, message splitting)
6. Saves actionable notifications (plan/HITL/question) to `pending_actions.json`
7. Advances the high-water mark only after successful delivery

### Inbound

1. `sase_chop_tg_inbound` polls Telegram for updates (long-polling or `--once`)
2. Dispatches each update by type:
   - **Callback query** → decodes button press, matches against pending actions, writes response file
   - **Text message** → completes two-step feedback flow, or dispatches as dot command / agent launch
   - **Photo/document** → downloads file, builds agent prompt with image path
3. Updates the Telegram offset (`update_offset.txt`) to avoid reprocessing

## Key Design Decisions

- **Pure logic separation**: `inbound.py` contains no API calls — all logic is independently testable. The entry point
  script handles I/O and wiring.
- **High-water mark**: The outbound process tracks the timestamp of the last sent notification rather than individual
  notification IDs. This prevents both duplicates (re-sending) and loss (skipping unsent notifications if outbound was
  offline).
- **Exclusive locking**: A file lock prevents concurrent outbound runs from sending the same notifications twice. This
  is important because outbound runs are triggered by a chop (periodic scheduler).
- **Rate limiting**: A sliding-window limiter (default 8 messages / 15 seconds) prevents hitting Telegram's flood
  limits. Timestamps are persisted to `rate_limit.json` so the window survives process restarts.
- **Parse mode fallback**: If MarkdownV2 rendering fails (malformed escaping), the client retries with plain text to
  ensure the message is delivered.
- **PDF fallback chain**: For long plans, `pdf_convert.py` tries wkhtmltopdf first (best Unicode/emoji support), then
  falls back to xelatex, then pdflatex.
