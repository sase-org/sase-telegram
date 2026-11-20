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
  Bot token (pass, 60 s negative cache) + env vars
     │
degraded.py
  Persisted credential/network outage states + notices
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
4. Sends via `telegram_client.py` (with rate limiting, per-chunk retry/backoff, message splitting), resuming a
   notification that an earlier run delivered only in part at its first undelivered chunk (`partial_delivery.json`)
5. Saves actionable notifications (plan/HITL/question) to `pending_actions.json`
6. Advances the high-water cursor only after successful delivery, and never past a send that failed

### Inbound

1. `sase_job_tg_inbound --receiver` is a long-lived process. It registers the
   bot command menu and runs inbound housekeeping (completion delivery,
   `/usage` refresh finishing, media-group flushing, stale/handled button
   cleanup) under a shared lock before each `getUpdates` long poll and again
   after dispatch, so the AXE `tg_inbound` tick is optional under the service
   host. Before each poll, and again after the poll returns, it compares the
   installed SASE / `sase-telegram` / `sase_core_rs` generation to the
   fingerprint captured at start. A changed or unsettled generation re-execs
   the canonical `sase_job_tg_inbound --receiver` argv in the same process
   slot instead of dispatching with mixed imports.
2. `getUpdates` fetches the currently pending Telegram updates from the stored offset
3. Each update is dispatched by type, and the offset in `update_offset.txt` is saved
   only after that update finishes (successfully or with a caught, logged handler
   error). An update fetched across a runtime refresh is not saved, so the fresh
   interpreter fetches it again rather than acknowledging it as processed:
   - **Callback query** → decodes button press, handles notification responses or agent/bead/usage callbacks
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
- **Pure logic separation**: `inbound.py` contains no API calls — all logic is independently testable. The Telegram
  update handlers live in `inbound_handlers/` and the entry point script only wires the run modes.
- **High-water mark**: The outbound process tracks the timestamp of the last sent notification rather than individual
  notification IDs. It is initialized to "now" on first run to avoid dumping historical backlog, then advanced after
  each successful send.
- **Exclusive locking**: A file lock prevents concurrent outbound runs from sending the same notifications twice. This
  is important because outbound runs are triggered by an AXE job (periodic scheduler).
- **Rate limiting**: A sliding-window limiter (default 8 messages / 15 seconds) prevents hitting Telegram's flood
  limits. Timestamps are persisted to `rate_limit.json` so the window survives process restarts.
- **Two-step feedback isolation**: Feedback/custom flows are keyed by the originating Telegram message ID. A user can
  reply to the relevant message without overwriting other active feedback flows.
- **Parse mode fallback**: Only when Telegram rejects the message's entities (a `BadRequest` such as "Can't parse
  entities"), the client resends that message or edit as plain text. Timeouts, network errors and Bad Gateway, rate
  limits, `Forbidden`, `InvalidToken`, and credential errors never trigger the fallback; they reach the retry wrapper
  and the caller unchanged.
- **Per-chunk retries**: A message longer than 4096 characters is split, and each chunk is retried on its own (up to 4
  attempts with 2, 4, and 6 s backoff for timeouts and network errors). A delivered chunk is never resent, and only the
  last chunk carries the inline keyboard.
- **Degraded states**: `degraded.py` classifies credential failures as `credential_unavailable` and timeouts and
  network errors as `network_degraded`, and persists one state per component (`degraded_outbound.json`,
  `degraded_receiver.json`) because outbound is a fresh process every tick and the receiver restarts after a
  credential exit. Each transition logs one line and raises or settles one SASE notification; repeats are summarized
  every 15 minutes. `credentials.py` remembers a failed `pass` lookup for 60 seconds in
  `bot_token_unavailable.json`.
- **Shared PDF rendering**: `pdf_convert.py` delegates Markdown-to-PDF rendering to SASE's shared attachment renderer
  and applies the plugin stylesheet.
