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
2. **Load unsent** — `get_unsent_notifications()` loads notifications newer than the high-water mark in `last_sent_ts`,
   filtered to `read == False` and `silent == False`. Dismissed notifications are **not** filtered out because TUI
   dismissal is a UI cleanup action, not a notification-read signal.
3. **Stale cleanup** — `cleanup_stale()` removes pending actions older than 24 hours.
4. **Format and send** — Each notification is formatted by `format_notification()` into MarkdownV2 text with an inline
   keyboard, then sent via `telegram_client.py`. Rate limiting is checked before each send.
5. **Save pending** — Actionable notifications (plan approval, HITL, user question) are saved to
   `pending_actions.json` with their Telegram `message_id` so inbound can edit the keyboard later.
6. **Advance HWM** — `mark_sent()` updates the high-water mark after each successfully delivered notification. If
   outbound crashes mid-batch, only notifications after the last successful send are retried.
7. **Release lock** — `release_outbound_lock()` releases the file lock.

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

The header, review note, Properties card, and body share Telegram's 4096-character budget. Property labels are retained
when space is tight; only large displayed values and then the body preview are truncated, each with a pointer to the
attached plan. Missing, unreadable, invalid-UTF-8, malformed, or non-mapping frontmatter falls back to the established
body-only preview without blocking the approval keyboard. An existing plan file remains attached even if its preview
cannot be parsed.

### Notification Types

| Type | Body Content | Buttons |
|---|---|---|
| Plan Approval | Ordered Properties card + plan body + optional model/agent label | Tale, ✅ Approve, Epic, Reject, Feedback |
| HITL Request | Request notes | Accept, Reject, Feedback |
| User Question | Question text + options | One button per option + Custom |
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
  document.
