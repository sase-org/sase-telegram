# Outbound

The outbound script (`sase_chop_tg_outbound`) sends sase notifications to Telegram when the user is inactive.

## CLI Usage

```bash
sase_chop_tg_outbound              # Normal run
sase_chop_tg_outbound --dry-run    # Print what would be sent without sending
sase_chop_tg_outbound --context X  # Pass context string for logging
```

## Pipeline

1. **Lock acquisition** — `try_acquire_outbound_lock()` takes an exclusive lock on `~/.sase/telegram/outbound.lock`.
   If another outbound process is running, this one exits immediately.
2. **Idle check** — Reads the TUI activity state. If the user is active, skips sending.
3. **Load unsent** — `get_unsent_notifications()` loads notifications newer than the high-water mark in `last_sent_ts`,
   filtered to `read == False`. Dismissed notifications are **not** filtered out (TUI dismissal is an active-user
   action; outbound only runs when idle).
4. **Stale cleanup** — `cleanup_stale()` removes pending actions older than 24 hours.
5. **Format and send** — Each notification is formatted by `format_notification()` into MarkdownV2 text with an inline
   keyboard, then sent via `telegram_client.py`. Rate limiting is checked before each send.
6. **Save pending** — Actionable notifications (plan approval, HITL, user question) are saved to
   `pending_actions.json` with their Telegram `message_id` so inbound can edit the keyboard later.
7. **Advance HWM** — `mark_sent()` updates the high-water mark to the latest notification's timestamp. This only
   happens after successful delivery, so if outbound crashes mid-batch, the unsent notifications will be retried.
8. **Release lock** — `release_outbound_lock()` releases the file lock.

## Notification Formatting

### Message Structure

Each notification is formatted as a Telegram message with:
- A header line (notification type, agent name, workspace number)
- Content body (plan text, HITL notes, question options, etc.)
- An inline keyboard with action buttons

### Content Handling

| Content Size | Behavior |
|---|---|
| < 500 chars | Inline in the message body |
| 500–3500 chars | Wrapped in an expandable blockquote (Telegram Bot API 7.4+) |
| > 3500 chars | Truncated in the message; full content sent as PDF attachment |

### Notification Types

| Type | Body Content | Buttons |
|---|---|---|
| Plan Approval | Plan text + diff summary | Approve, Reject, Epic, Feedback |
| HITL Request | Request notes | Accept, Reject, Feedback |
| User Question | Question text + options | One button per option + Custom |
| Workflow Complete | Summary + attachments | Resume (copy-text) |
| Agent Launched | Provider/model, prompt snippet | Resume, Wait, Kill (copy-text) |
| Agent Killed | Termination confirmation | Retry (copy-text) |
| Error Digest | Error summary | — |
| Image Generated | Model name | Sends image inline |

### Attachments

- **Plan PDFs**: Long plans are converted to PDF via pandoc and sent as documents
- **Diff sections**: Diffs from chat files and commit messages are embedded into the plan PDF
- **Research files**: Detected research files in diffs are mentioned in the notification
- **Digest files**: Error digest files are sent as document attachments
