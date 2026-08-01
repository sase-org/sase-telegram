# Outbound

The outbound script (`sase_chop_tg_outbound`) sends sase notifications to Telegram.

## Machine Enable Flag

The chop is a **no-op unless `~/.sase/telegram_is_enabled` exists**. When the flag file is absent, the script exits
immediately with status `0`, prints nothing, and skips all heavy imports, network calls, and locks. Enable a machine
with `touch ~/.sase/telegram_is_enabled`.

## CLI Usage

```bash
sase_chop_tg_outbound              # Normal run
sase_chop_tg_outbound --dry-run    # Print what would be sent and advance the high-water mark
sase_chop_tg_outbound --context X  # Pass context string for logging
```

## Pipeline

1. **Lock acquisition** — `try_acquire_outbound_lock()` takes an exclusive lock on `~/.sase/telegram/outbound.lock`.
   If another outbound process is running, this one exits immediately.
2. **Load unsent** — `get_unsent_notifications()` performs a current-state notification read and returns the rows past
   the high-water cursor in `last_sent_ts`, filtered to `read == False`, `silent == False`, and `muted == False`, sorted
   oldest-first by activity cursor. Dismissed notifications are **not** filtered out because TUI dismissal is a UI
   cleanup action, not a notification-read signal. See [Delivery Cursor](#delivery-cursor) below.
3. **Stale cleanup** — `cleanup_stale()` removes pending actions older than 24 hours.
4. **Format and send** — Each notification is formatted by `format_notification()` into MarkdownV2 text with an inline
   keyboard, then sent via `telegram_client.py`. Rate limiting is checked before each send.
5. **Save pending** — Every action registered in SASE's notification-gate adapter registry is saved to
   `pending_actions.json` with its Telegram `message_id` so inbound can resolve it and edit the keyboard later.
6. **Advance HWM** — `mark_sent()` updates the high-water cursor after each successfully delivered notification. Because
   the batch is processed oldest-first and the cursor is never advanced past an event that failed to send, a crash or a
   failed send mid-batch retries exactly the undelivered remainder.
7. **Release lock** — `release_outbound_lock()` releases the file lock.

## Delivery Cursor

`~/.sase/telegram/last_sent_ts` holds a versioned JSON cursor written atomically through a tempfile and `os.replace`:

```json
{"version":2,"activity_at":"2026-08-01T13:05:00Z","id":"7f3c…"}
```

The cursor is an **activity** cursor, not a creation-time cursor. A notification's activity time is `resurfaced_at` when
a snooze has expired and `timestamp` otherwise, and the `id` component is the tie-breaker that keeps two rows sharing an
activity instant from hiding one another. This is the same `(activity_at, id)` contract the SASE store, CLI, and mobile
gateway use.

Consequences for delivery:

- A notification that was snoozed before it was ever delivered stays muted and undelivered until its deadline, then is
  delivered exactly once when its resurfaced generation crosses the cursor.
- A notification that was already delivered and is later snoozed becomes eligible **again** exactly once, when its
  resurface generation appears. Its original `timestamp` is unchanged, so only the activity cursor makes this possible.
- Explicitly unmuted and dismissed snoozes never create a resurface generation, so they never produce a second delivery.
- On first run, outbound initializes the cursor to "now" and sends nothing, preserving first-run backlog suppression.
- A legacy file containing a bare epoch timestamp is read with a maximal ID component — preserving the old strict
  timestamp comparison so nothing is re-delivered — and is migrated to the versioned form atomically on that read.

The outbound read prefers the host store's current-state API, which atomically expires due snoozes before projecting, so
an offline-then-online chop catches up on the next run rather than losing the reminder. Older `sase` installs fall back
to the equivalent expiring snapshot read.

## Notification Formatting

### Message Structure

Each notification is formatted as a Telegram message with:
- A header line (notification type, agent name, workspace number)
- Content body (plan properties and Markdown, HITL notes, question options, etc.)
- An inline keyboard with action buttons

### Content Handling

| Content Size | Behavior |
|---|---|
| < 500 chars | Inline in the message body |
| 500–3500 chars | Wrapped in an expandable blockquote (Telegram Bot API 7.4+) |
| > 3500 chars | Truncated in the message; the complete attachment remains available as a document |

Plan approvals split the attached file once with SASE's safe frontmatter parser. Every parseable top-level field is
shown in a **Properties** card before the rich Markdown body: identity/lifecycle fields use a predictable semantic
order, and unfamiliar fields follow alphabetically. Lists and mappings render as indented multiline values, while
empty values and containers remain explicit. Short cards stay open; metadata-heavy cards use an expandable blockquote.
Epic approval headings also show the top-level phase-sequence length at a glance, such as
`Epic Review · 3 phases` or `Epic Review · 1 phase`. The suffix is best-effort and is omitted when the plan cannot be
read or parsed, `phases` is absent, or its value is not a sequence; tale review headings remain unchanged.
Successfully validated, nonempty epics add a separate line such as
`Phase sizes: 2 small · 1 medium · 1 large`. Buckets always appear in `small`, `medium`, `large` order and zero buckets
are omitted. This compact line complements the complete nested `phases[].size` values in Properties; legacy missing
sizes normalize to `small` in launch-consumption mode. Validation errors, unavailable validator capabilities, and
preview-time validator failures quietly omit the line without changing the raw phase count, attachment, or controls.

The header, review note, Properties card, and body share Telegram's 4096-character budget. Property labels are retained
when space is tight; only large displayed values and then the body preview are truncated, each with a pointer to the
attached plan. Missing, unreadable, invalid-UTF-8, malformed, or non-mapping frontmatter falls back to the established
body-only preview without blocking the approval keyboard. An existing plan file remains attached even if its preview
cannot be parsed.

### Notification Types

Gate capabilities come from SASE's notification-gate adapter registry. Telegram keeps specialized bodies for plans,
launch approvals, HITL, and user questions; registry actions without a specialized body use the shared envelope-driven
gate formatter. Registering a new branch-actionable kind therefore gives it pending tracking, buttons, attachments,
and callback handling without another Telegram action allowlist.

| Type | Body Content | Buttons |
|---|---|---|
| Plan Approval | Ordered Properties card + plan body + optional model/agent label | Tale, ✅ Approve, Epic, Reject, Feedback |
| HITL Request | Request notes | Accept, Reject, Feedback |
| User Question | Question text + options | One button per option + Custom |
| Generic Gate (including Task Triage and Custom Gate) | Notes + inline Markdown preview | One button per declared branch, plus a "with feedback" button for a branch whose selection declares `feedback: optional` |
| Workflow Complete | Summary, optional PR URL, prompt snippet + attachments | Fork (copy-text) |
| Agent Launched | Provider/model, workspace number, prompt snippet | Fork, Wait, Kill, Retry |
| Agent Killed | Termination confirmation | Redo |
| Error Digest | Error summary | — |
| Image Generated | Model name | Sends image inline |

The visible plan **✅ Approve** button maps to the internal `run` payload for compatibility. It approves the plan with
`commit_plan: false` and `run_coder: true`, which starts coder work without committing the plan first.

### Attachments

- **Plan attachments**: Plan files are attached whenever present, including when preview parsing fails; Markdown files
  are rendered to PDF through SASE's shared Markdown renderer when possible
- **Diff sections**: Diffs from chat files and commit messages are embedded into the response PDF
- **Research files**: Detected research files in diffs are mentioned in the notification
- **Digest files**: Error digest files are sent as document attachments
- **Media and PDFs**: Static images are sent inline as photos, GIFs as animations, videos as videos, and existing PDFs
  as documents without conversion. If Telegram rejects a GIF or video as inline media, outbound retries that file as a
  document. Workflow completions send one motion-media representation for files with the same normalized directory and
  filename stem, preferring GIF, MP4, M4V, MOV, then WEBM. Other artifacts remain separate, and every discovered file
  remains available in the underlying SASE notification and artifact inventory.
