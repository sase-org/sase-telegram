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
     │                     │
bead_format.py         receiver.py
  sase bead output         Idempotent ensure of the long-poll proc
     │                     │
rate_limit.py          receiver_runtime.py
  Sliding-window throttle  Installed SASE/plugin/native generation fingerprint
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

1. `sase_job_tg_outbound` acquires an exclusive file lock (`outbound.lock`)
2. Reads current notification state and selects unsent rows past a versioned `(activity_at, id)` high-water cursor
   (`last_sent_ts`), oldest-first
3. Formats each notification as MarkdownV2 with inline keyboards (`formatting.py`)
4. Sends via `telegram_client.py` (with rate limiting, retry/backoff, message splitting)
5. Saves actionable notifications (plan/HITL/question) to `pending_actions.json`
6. Advances the high-water cursor only after successful delivery, and never past a send that failed

### Inbound

1. `sase_job_tg_inbound --receiver` is a long-lived process. Before each `getUpdates`
   long poll, and again after the poll returns, it compares the installed SASE /
   `sase-telegram` / `sase_core_rs` generation to the fingerprint captured at start.
   A changed or unsettled generation re-execs the canonical
   `sase_job_tg_inbound --receiver` argv in the same process slot instead of
   dispatching with mixed imports.
2. `getUpdates` fetches the currently pending Telegram updates from the stored offset
3. Each update is dispatched by type, and the offset in `update_offset.txt` is saved
   only after that update finishes (successfully or with a caught, logged handler
   error). An update fetched across a runtime refresh is not saved, so the fresh
   interpreter fetches it again rather than acknowledging it as processed:
   - **Callback query** → decodes button press, handles notification responses or agent/bead callbacks
   - **Text message** → completes a matching two-step feedback flow, dispatches a slash command, or launches an agent
   - **Photo/image document** → downloads file, builds agent prompt with image path

## Key Design Decisions

- **Machine enable gate**: Both console-script wrappers (`scripts/__init__.py`) check `~/.sase/telegram_is_enabled`
  via `enabled.py` before doing anything else. If the flag is absent, the wrapper returns `0` immediately — before the
  lazy import of the entry-point module — so a disabled machine skips all heavy imports, network, and locks and stays
  silent. This lets the telegram routine be configured globally while only flagged machines talk to Telegram.
- **Generation-aware receiver refresh**: The long-poll process fingerprints its installed
  runtime and re-execs in place when that fingerprint changes, so editable or managed
  updates cannot mix old extension bindings with new Python. Re-exec preserves the
  single `getUpdates` consumer; offset handoff stays lossless because the offset
  advances only after a successful or explicitly skipped dispatch.
- **Pure logic separation**: `inbound.py` contains no API calls — all logic is independently testable. The entry point
  script handles I/O and wiring.
- **High-water mark**: The outbound process tracks the timestamp of the last sent notification rather than individual
  notification IDs. It is initialized to "now" on first run to avoid dumping historical backlog, then advanced after
  each successful send.
- **Exclusive locking**: A file lock prevents concurrent outbound runs from sending the same notifications twice. This
  is important because outbound runs are triggered by an AXE job (periodic scheduler).
- **Rate limiting**: A sliding-window limiter (default 8 messages / 15 seconds) prevents hitting Telegram's flood
  limits. Timestamps are persisted to `rate_limit.json` so the window survives process restarts.
- **Two-step feedback isolation**: Feedback/custom flows are keyed by the originating Telegram message ID. A user can
  reply to the relevant message without overwriting other active feedback flows.
- **Parse mode fallback**: If MarkdownV2 rendering fails (malformed escaping), the client retries with plain text to
  ensure the message is delivered.
- **Shared PDF rendering**: `pdf_convert.py` delegates Markdown-to-PDF rendering to SASE's shared attachment renderer
  and applies the plugin stylesheet.
