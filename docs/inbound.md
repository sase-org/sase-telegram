# Inbound

The inbound script (`sase_chop_tg_inbound`) polls Telegram for user responses and dispatches them back to sase.

## CLI Usage

```bash
sase_chop_tg_inbound              # Process pending updates and exit
sase_chop_tg_inbound --once       # Compatibility flag; also processes pending updates and exits
sase_chop_tg_inbound --context X  # Pass context string for logging
```

## Update Processing

The inbound script fetches updates from Telegram starting from the stored offset in `update_offset.txt`, saves the next
offset before handling them, then dispatches each update by type.

### Callback Queries (Button Presses)

When a user presses an inline keyboard button, Telegram sends a callback query with encoded data. The callback data
format is:

```
{action_type}:{notif_id_prefix}:{choice}
```

- **action_type**: `plan`, `hitl`, `question`, `kill`, `retry`, `bead`
- **notif_id_prefix**: First N characters of the notification ID (enough to match uniquely)
- **choice**: `approve`, `run`, `reject`, `epic`, `feedback`, `accept`, `0`/`1`/`2`/... (question options), etc.

The 64-byte total limit (Telegram API constraint) is enforced at encoding time.

#### Direct Actions

For most choices (`approve`, `run`, `reject`, `accept`, question option numbers), the callback is matched against
`pending_actions.json`, the response is written to the notification's response file, and the pending action is removed.
The inline keyboard is also edited to show the selected action.

Plan `run` writes an approval response with `commit_plan: false` and `run_coder: true`.

#### Two-Step Actions

For `feedback` and `custom` choices, the flow is:

1. The callback is saved to `awaiting_feedback.json` with the pending action context
2. The entry is keyed by the originating Telegram `message_id`, so multiple feedback flows can be active at once
3. Telegram answers the button tap with a prompt to send feedback and removes the original inline keyboard
4. The next reply to that Telegram message completes the action; if only one feedback flow exists, a plain text
   message can complete it as a compatibility fallback:
   - The feedback/custom text is written to the response file
   - `awaiting_feedback.json` is cleared
   - The pending action is removed

### Text Messages

Text messages are dispatched in priority order:

1. **Two-step completion** — If `awaiting_feedback.json` has an active flow, the text completes it (see above)
2. **Slash commands** — Messages starting with `/` are agent management commands (registered with `set_my_commands` so they appear in the chat input UI):
   - `/list` — Lists running agents with provider/model and a prompt snippet
   - `/kill` — Shows an inline keyboard of running agents with rich descriptions
   - `/kill <name>` — Terminates the named agent (sends a 🔄 Retry button on success)
   - `/fork` — Shows fork copy buttons for named running agents
   - `/changes [project]` — Shows copy buttons for active ChangeSpec workflow tags, optionally filtered by exact project name
   - `/xprompts` — Builds the xprompts catalog PDF and reports its path
   - `/bead [<id>]` — Shows active beads as picker buttons, or renders `sase bead show <id>` output in chat
   - `/update` — Starts the detached SASE update worker and replies with its log path
3. **Other slash commands** — Unknown commands (e.g. `/start`) are silently ignored
4. **Agent launch** — Everything else launches a new sase agent with the message as the prompt

If `SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED` is present in the environment, step 4 is skipped. Two-step completions and
slash commands still run normally, but free-form text that would launch an agent is logged and ignored without sending a
Telegram acknowledgement. The check is presence-based, so an empty value still disables launches.

### Photos and Image Documents

Photos or image documents sent to the bot are:
1. Downloaded to `~/.sase/telegram/images/` with a timestamped filename
2. Used to build an agent prompt that references the downloaded image path
3. A new sase agent is launched with the visual context

When `SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED` is present, photos and image documents are ignored before file download, so
disabled hosts do not call Telegram's file API or create local image files for launch prompts.

## Agent Launching

When a text message or photo triggers an agent launch:

- **XPrompt expansion**: References like `#mentor` or `#gh(...)` in the message are expanded
- **VCS shorthand**: Telegram launch prompts can use `#gh@sase`; it is normalized to `#gh:sase` before launch
- **Multi-model directives**: `%m(opus,sonnet)` launches the prompt across multiple models
- **Auto-naming**: Agents launched from Telegram are automatically assigned names
- **Code reconstruction**: Telegram strips backtick formatting from messages; `reconstruct_code_markers()` re-inserts
  them using Telegram's entity metadata
- **Project context**: If the launch prompt contains a VCS project tag like `#gh:sase`, the bot remembers that
  chat-scoped project in `~/.sase/telegram/project_context.json` for later `/bead` commands
- **Launch confirmation**: A message is sent back with Fork and Wait copy-text buttons, plus Kill and Retry controls

## ChangeSpec Tags

`/changes` lists active ChangeSpecs, excluding Submitted, Archived, and Reverted entries. `/changes <project>` filters by
exact project name. Each listed ChangeSpec gets a copy-text button containing only the workflow tag, for example
`#hg:foobar`.

If workflow detection fails for some entries, the command still shows the entries it can resolve and includes a skipped
count. Large result sets are split across multiple Telegram messages without dropping entries.

## Beads

`/bead` runs `sase bead list` across known workspaces from `~/.sase/projects/*/<project>.sase` (legacy `.gp` files are
also read as a fallback), parses active beads
(`open` and `in_progress`), and shows up to 80 picker buttons. `/bead <id>` runs `sase bead show <id>`, converts the
plain-text output to Markdown, then escapes it for Telegram MarkdownV2.

If `SASE_TELEGRAM_BEAD_PROJECT` is set, bead commands are narrowed to that project workspace. Without the override,
picker callbacks carry the source project when possible, and manual detail lookup searches known projects after trying
the remembered Telegram chat context first.

## Update

`/update` calls the shared SASE chat-install launcher. The inbound handler does not stop axe or run the update inline;
it only starts a detached worker, then posts an acknowledgement that distinguishes missing configuration, workspace
resolution failure, an already-running worker, or a launched worker with a log path. The worker restarts axe in its
cleanup path even if sync or the configured install command fails. When the detached worker writes its final completion
record, the next inbound run sends a second message with either the success result or the failure exit code, including
the same worker log path. Pending completion deliveries live under `~/.sase/telegram/update_completions/` and are
retried until Telegram accepts the message.

Command registration is cached in `~/.sase/telegram/commands_registered_ts`; the cache includes a command-list
fingerprint so deploys with command changes re-register immediately instead of waiting for the hourly refresh.
