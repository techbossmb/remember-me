---
name: remember
description: Long-term memory for this project (remember.me). Use when the user wants to start memory here, map or update the map of past conversations, asks what was decided or where work stopped earlier ("where were we", "what did we decide about X"), or wants to import chat history.
---

# remember.me

Memory lives in plain files under `~/.remember` (verbatim conversation logs + a map of them). The same files work from
any assistant; nothing here is Claude-specific except the hooks that capture sessions automatically.

Run the CLI as:

```
R="python3 ${CLAUDE_PLUGIN_ROOT}/remember/cli.py"
```

(If the `remember_*` MCP tools are available they do the same things.) `$R status` is always a safe first step.

## Using memory

The session-start context already contains the map's overview when memory is on for this folder. To answer from
history, pick the threads that matter **by meaning** from that overview and open them in one call:
`$R zoom T4 T13 --q "<the question>"`; `$R zoom --recent 5` for "where were we". Verbatim text: `$R spans <ref>`;
search: `$R grep <regex> [--from D --to D]`. The log is the record: if it disagrees with the map, say so and go with the log.
Follow the map's rules; when about to do work a rule's trigger covers, open its playbook and copy its steps into your plan.

## Starting memory in this folder (no setup for the user)

Do this when the user asks to start memory / map this project / "remember this", or accepts the offer from the
session-start note. Keep the user informed in one or two lines per step.

1. `$R status` — shows what is already available.
2. **Bring in what is on disk.** If Claude Code session files exist for this folder, run `$R ingest claude-code`.
   It is local and cheap. Report how many sessions and the date range.
3. **If nothing is on disk** (or this session's own file is missing), write what is in your context into the log:
   the conversation so far as `## user` / `## assistant` sections, user messages verbatim, your replies as written
   (summarise long tool output in one line). Pipe it to `$R log --session <short-name> --note "from context"`.
   It is stored as *recalled*, not verbatim.
4. **Say honestly what is missing.** If your context begins with a summary of earlier turns, or `status` reports
   harness summaries, the earlier verbatim turns are not available here. Tell the user, and mention the fuller-history
   option: a claude.ai data export (Settings → Privacy → Export data) can be imported selectively (step 7).
5. **Build the map.** For more than ~60 messages, hand it to the `remember-me:builder` agent with the project name
   (from `status`); for less, follow `$R builder-prompt` yourself. If there is a lot (hundreds of messages), offer to
   map the most recent sessions first, since building costs tokens.
6. **Show the value.** Print `$R overview`, name two or three things the map now knows (open threads, standing
   rules), and give them the map: `$R show` (or the `remember_show_map` tool) starts or reuses a local viewer and prints
   its link; pass the link on as a clickable link.

## Showing the map

"Show me my memory / the map": run `$R show` (or `remember_show_map`) and give the user the link it prints, as a
clickable link. It starts the viewer if needed and replaces one left over from an older version. Never ask the user
to run anything.

- Link doesn't load / "restart my memory map": `$R viewer restart`, then give the link again.
- "Stop the memory viewer": `$R viewer stop` (works wherever it was started). `$R viewer status` says where it runs.
- "Keep it running" / start at login: `$R viewer autostart` (`autostart off` undoes it). Offer this once, after
  memory is first started, in one line.
- If your shell is a sandbox that can't keep processes running after a command (e.g. a cloud session's link to the
  user's computer), the viewer won't stay up from there: say so, and give the user the one-line command
  `remember viewer start` (or the atlas file from `$R atlas`) instead.

## Keeping it current

The user shouldn't have to think about it. Hooks append every turn of this folder's sessions to the log once memory
is on. When messages are not in the map yet, a note at session start (or after a turn) says so: after finishing what
the user asked, hand the update to the `remember-me:builder` agent with the project name, so the work stays out of
this conversation, and don't mention it unless asked. `REMEMBER_AUTOBUILD=background` instead keeps the map current
with a headless job even when no session is open; `=off` updates only when asked (`$R build` or the builder agent).

## Importing other history (optional, selective)

`$R import <export.zip or folder> --list` lists conversations (newest first). Ask the user which ones belong to this
project, then `$R import <path> --pick 3 --pick "lecture slides"` (number, uuid prefix or name substring;
`--pick all` for everything). Import into another project with `--project <name>`. Then update the map.

## Other assistants

`$R init --write` adds a short memory note to this folder's AGENTS.md / CLAUDE.md / GEMINI.md so other assistants
(Codex, Gemini CLI, Copilot, Cursor, …) use the same memory; `integrations/README.md` in the plugin folder has their
MCP setup.
