# Inbound

The inbound script (`sase_chop_tg_inbound`) polls Telegram for user responses and dispatches them back to sase.

## CLI Usage

```bash
sase_chop_tg_inbound              # Long-polling mode (runs continuously)
sase_chop_tg_inbound --once       # Process pending updates and exit
sase_chop_tg_inbound --context X  # Pass context string for logging
```

## Update Processing

The inbound script fetches updates from Telegram starting from the stored offset in `update_offset.txt`, then
dispatches each update by type.

### Callback Queries (Button Presses)

When a user presses an inline keyboard button, Telegram sends a callback query with encoded data. The callback data
format is:

```
{action_type}:{notif_id_prefix}:{choice}
```

- **action_type**: `plan`, `hitl`, `question`, `kill`, `retry`
- **notif_id_prefix**: First N characters of the notification ID (enough to match uniquely)
- **choice**: `approve`, `reject`, `epic`, `feedback`, `accept`, `0`/`1`/`2`/... (question options), etc.

The 64-byte total limit (Telegram API constraint) is enforced at encoding time.

#### Direct Actions

For most choices (`approve`, `reject`, `accept`, question option numbers), the callback is matched against
`pending_actions.json`, the response is written to the notification's response file, and the pending action is removed.
The inline keyboard is also edited to show the selected action.

#### Two-Step Actions

For `feedback` and `custom` choices, the flow is:

1. The callback is saved to `awaiting_feedback.json` with the pending action context
2. Telegram sends a confirmation message asking the user to type their response
3. The next text message from the user completes the action:
   - The feedback/custom text is written to the response file
   - `awaiting_feedback.json` is cleared
   - The pending action is removed

### Text Messages

Text messages are dispatched in priority order:

1. **Two-step completion** — If `awaiting_feedback.json` has an active flow, the text completes it (see above)
2. **Dot commands** — Messages starting with `.` are agent management commands:
   - `.kill <name>` — Kills a running agent by name
   - `.list` — Lists active agents
   - `.listx` — Lists active agents with extended details
3. **Bot commands** — Messages starting with `/` (like `/start`) are silently ignored
4. **Agent launch** — Everything else launches a new sase agent with the message as the prompt

### Photos and Documents

Photos sent to the bot are:
1. Downloaded to `~/.sase/telegram/images/` as `{timestamp}_{file_id_prefix}.jpg`
2. Used to build an agent prompt that references the downloaded image path
3. A new sase agent is launched with the visual context

## Agent Launching

When a text message or photo triggers an agent launch:

- **XPrompt expansion**: References like `#mentor` or `#gh(...)` in the message are expanded
- **Multi-model directives**: `%m(opus,sonnet)` launches the prompt across multiple models
- **Auto-naming**: Agents launched from Telegram are automatically assigned names
- **Code reconstruction**: Telegram strips backtick formatting from messages; `reconstruct_code_markers()` re-inserts
  them using Telegram's entity metadata
- **Launch confirmation**: A message is sent back with Resume, Wait, and Kill copy-text buttons
