# Inbound

The inbound script (`sase_chop_tg_inbound`) polls Telegram for user responses and dispatches them back to sase.

## Machine Enable Flag

The chop is a **no-op unless `~/.sase/telegram_is_enabled` exists**. When the flag file is absent, the script exits
immediately with status `0`, prints nothing, and skips all heavy imports and network calls. Enable a machine with
`touch ~/.sase/telegram_is_enabled`.

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

- **action_type**: `plan`, `hitl`, `question`, `kill`, `retry`, `bead`, `list`, `show`
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
   - `/list` — Lists running agents with provider/model, status, context, activity, prompt snippet, and overview buttons
   - `/list all` — Includes recently finished and failed agents
   - `/list <name>` — Shows a detail view for one agent with Fork/Wait/Kill/Retry buttons
   - `/list <project>` — Filters the overview to one project (agent names win when a name and project match)
   - `/show` — Shows a mobile-friendly index of known clans, families, and tribes
   - `/show <ref>` — Shows a rich detail view for an agent, clan, family, or tribe
   - `/show @<tribe>` — Forces tribe interpretation when a name is shared with another entity
   - `/kill` — Shows an inline keyboard of running agents with rich descriptions
   - `/kill <name>` — Terminates the named agent (sends a 🔄 Redo button on success)
   - `/fork` — Shows fork copy buttons for named running agents
   - `/changes [project]` — Shows copy buttons for active ChangeSpec workflow tags, optionally filtered by exact project name
   - `/xprompts` — Builds the xprompts catalog PDF and reports its path
   - `/bead [<id>]` — Shows active beads as picker buttons, or renders `sase bead show <id>` output in chat
   - `/update` — Starts the detached SASE update worker and replies with its log path
3. **Configured slash commands** — A command in `telegram.commands` runs its executable and returns Markdown as a message or PDF
4. **Other slash commands** — Unknown commands (e.g. `/start`) are silently ignored
5. **Agent launch** — Everything else launches a new sase agent with the message as the prompt

If `SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED` is present in the environment, step 5 is skipped. Two-step completions and
slash commands still run normally, but free-form text that would launch an agent is logged and ignored without sending a
Telegram acknowledgement. The check is presence-based, so an empty value still disables launches.

### Photos, Albums, and Image Documents

Single photos or image documents sent to the bot are:
1. Downloaded to `~/.sase/telegram/images/` with a timestamped filename
2. Used to build an agent prompt that references the downloaded image path
3. A new sase agent is launched with the visual context

Telegram albums are delivered as multiple updates with the same `media_group_id`. The inbound script stages those
updates in `~/.sase/telegram/media_groups.json`, waits for a small quiet window so split deliveries can join the same
album, then downloads every image and launches one agent prompt with a numbered list of all local image paths. If a
download fails, the bot sends one error message, removes the staged album, and does not launch an agent.

When `SASE_TELEGRAM_LAUNCH_AGENTS_DISABLED` is present, photos, image documents, and albums are ignored before staging
or file download, so disabled hosts do not call Telegram's file API or create local image files for launch prompts.

## Agent Launching

When a text message or photo triggers an agent launch:

- **XPrompt expansion**: References like `#mentor` or `#gh(...)` in the message are expanded
- **VCS shorthand**: Telegram launch prompts can use `#gh@sase`; it is normalized to `#gh:sase` before launch
- **Multi-model directives**: `%{%m:opus | %m:sonnet}` launches the prompt across multiple models
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

`/bead` runs `sase bead list --status=open --status=in_progress` across known workspaces from
`~/.sase/projects/*/<project>.sase` (legacy `.gp` files are also read as a fallback), parses the active results, and
shows up to 80 picker buttons. The explicit filters disable the CLI's interactive closed-bead fallback: a project with
only closed beads contributes no buttons, and Telegram reports `No active beads.` when no project has active work.
`/bead <id>` runs `sase bead show <id>`, converts the plain-text output to Markdown, then escapes it for Telegram
MarkdownV2.

If `SASE_TELEGRAM_BEAD_PROJECT` is set, bead commands are narrowed to that project workspace. Without the override,
picker callbacks carry the source project when possible, and manual detail lookup searches known projects after trying
the remembered Telegram chat context first.

## Update

`/update` calls the shared SASE chat-install launcher. The inbound handler does not stop axe or run the update inline;
it only starts a detached worker, then posts an acknowledgement that distinguishes an already-running worker from a
launched worker with a log path. The worker runs the built-in `sase update --json` engine, using SASE's normal
managed-vs-dev update routing, then ensures axe is running afterward. When the detached worker writes its final
completion record, the next inbound run sends a second message with the worker's update summary when present, falling
back to the failure exit code, and includes the same worker log path. Pending completion deliveries live under
`~/.sase/telegram/update_completions/` and are retried until Telegram accepts the message.

Command registration is cached in `~/.sase/telegram/commands_registered_ts`; the cache includes a command-list
fingerprint so deploys with command changes re-register immediately instead of waiting for the hourly refresh.

## Agent Kinship Views

`/show <ref>` resolves references in a deterministic order: an `@name` is always a tribe; otherwise an exact agent
name wins, followed by a clan, a family, and finally a case-insensitive bare tribe name. Agent members such as
`review.worker` and family members such as `migrate--planner` are ordinary exact agent names. If a non-tribe target
shares its name with a tribe, the response includes a `/show @name` hint. Unknown references offer up to six matching
agent, clan, family, or tribe buttons.

Agent views reuse `/list <name>` details and add Clan, Tribe, Family, Parent, and Children rows when available. Clan
views show generation progress, an effective tribe and summary, status rollups, and member drill-downs. Family views
show the sequential launch chain, current phase activity, and the active prompt. Tribe views group effective members by
clan, family, and standalone agent. Bare `/show` provides an index over every group represented by live or recent
entries.

Every open or refresh button stores the full reference in `pending_actions.json` and puts only a short generated key in
Telegram callback data, keeping arbitrary-length names below Telegram's 64-byte callback limit. Selections expire with
the normal 24-hour pending-action cleanup. Refresh edits a one-message view in place; views that need multiple chunks
are sent as fresh messages. Text is HTML-escaped, long views use the same chunking as `/list`, and oversized button sets
are capped with an explicit truncation note.

## Custom Slash Commands

Custom commands are read once at the start of each inbound poll from SASE's merged configuration, with project-local
configuration disabled so behavior does not depend on the chop's working directory. For example:

```yaml
telegram:
  commands:
    tasks:
      description: "📋 Obsidian tasks dashboard as a PDF"
      run: tg_cmd_tasks --note dash.md
      output: pdf
      timeout: 90s
```

Command names must contain 1–32 lowercase letters, digits, or underscores. `description` is required and appears in
Telegram's `/` menu. Configured commands are registered first in deterministic name order, followed by built-ins, so
personalized commands remain near the top of the menu. Telegram clients may present longer command lists in a
scrollable sheet. `run` is parsed with `shlex.split`; its executable may be a bare name on `PATH` or an absolute or `~`
path. No shell is used. `output` defaults to `message`; `timeout` defaults to `60s` and accepts `s`, `m`, or `h`. Built-in
command names, including the accepted `beads` alias, are reserved and cannot be shadowed. Invalid entries are logged and
skipped without preventing other commands or inbound updates from running.

Anything typed after the slash command is appended to the configured argument vector as one trailing argument, without
shell splitting, and is also available as `SASE_TELEGRAM_COMMAND_ARGS`. The command name is exported as
`SASE_TELEGRAM_COMMAND`. Each invocation runs in a new temporary working directory with stdout and stderr captured.

Successful stdout is Markdown. It may begin with YAML frontmatter:

```markdown
---
caption: "📋 *Tasks Dashboard* — 7 WIP · 6 NEXT · 23 READY"
filename: tasks_dashboard_2026-07-16.pdf
---

# 📋 Tasks Dashboard

...
```

For `message` output, the Markdown body is converted to Telegram MarkdownV2 and sent using the normal auto-splitting
and parse-mode fallback. Frontmatter is ignored. For `pdf` output, the bot immediately acknowledges the command,
renders the body with the shared SASE PDF renderer, and sends it as a document. `caption` defaults to the command
description, is converted to MarkdownV2, and is safely limited to 1024 characters. `filename` defaults to
`<command>_<YYYY-MM-DD>.pdf`. If rendering fails, the bot sends the Markdown body as a message instead.

A timed-out command reports its configured limit. A non-zero exit reports the exit code and a bounded stderr tail in
an expandable blockquote. Successful empty stdout gets an explicit empty-output response, so every recognized command
produces visible feedback.
