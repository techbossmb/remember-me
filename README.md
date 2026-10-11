# remember.me

Memory for AI assistants that survives compaction and new sessions.

- **A verbatim log** of your conversations, kept as plain markdown on your machine (`~/.remember`).
- **A map** of that log: threads with their decisions and open questions, standing rules ("never use emoji in UI
  text"), to-do lists, and step-by-step procedures. Each entry points back to the exact messages it came from.
- **One overview at session start.** The assistant reads the map, picks the threads that matter, and opens them,
  instead of starting from zero or rereading everything.

Everything is plain files plus a small Python CLI with no dependencies, so the same memory works in Claude Code,
Gemini CLI, Copilot, Cursor, Codex, or anything that can run a command or talk to an MCP server. A map built in one
works in the others.

> Early prototype. It works end to end in Claude Code; the other assistants are wired up but not yet tested.
> Tell me what breaks.

## Try it in Claude Code (2 minutes)

1. Get this folder (clone or unzip) somewhere permanent, e.g. `~/tools/remember-me`.
2. In Claude Code:
   ```
   /plugin marketplace add ~/tools/remember-me
   /plugin install remember-me@remember-me
   ```
   (or try it for one session: `claude --plugin-dir ~/tools/remember-me`)
3. Open a project you've already been working on with Claude Code and say: **"start memory for this project"**.

That's it. Claude brings in this folder's earlier Claude Code sessions that are still on disk, or, if there are
none, what's in the current conversation. It tells you if it can see that the conversation began earlier than what
it has. Then it builds the map, shows you what it now knows, and gives you a link to see it. From then on you don't
think about it:

- every turn in that folder is saved to the log automatically;
- the map keeps itself current: after finishing what you asked, Claude quietly hands new conversation to a
  sub-agent that updates the map, so none of that work lands in your conversation;
- every new session starts with the map's overview, so "where were we?" just works.

Nothing is captured until you start memory in a folder.

## See your map

Ask: **"show me my memory map"**. Your assistant gives you a link, usually **http://127.0.0.1:8765**: topics as a
graph, each with its decisions, open questions, the messages behind it, and the rules. You can rename, merge, move or
close topics there. Bookmark it.

The page is served by a small viewer that runs quietly on your own computer. It starts by itself whenever a session
starts in a project with memory, and you can have it start when you log in, so the link always works: ask your
assistant to "keep my memory map running", or run `remember viewer autostart`.

**If the link doesn't load** (after a restart, or the viewer stopped for some reason): ask your assistant to
"restart my memory map", or run `remember viewer restart`.

**To stop it**, even if you don't know where it was started: ask your assistant, or run `remember viewer stop`.
It finds the viewer by asking the port, so you don't need the window it was started in.
`remember viewer status` says whether it's running and where; `remember viewer autostart off` stops it starting at
login. Last resort, without remember.me at all:
- macOS / Linux: `lsof -ti tcp:8765 | xargs kill` (repeat with 8766… if it moved because 8765 was busy)
- Windows (Command Prompt): `netstat -ano | findstr :8765`, then `taskkill /PID <the number at the end> /F`

## Want more history?

Claude Code deletes old session files after a while, and claude.ai chats aren't on disk at all. To bring those in,
export your data from claude.ai (Settings → Privacy → Export data), then ask Claude to import it. It lists the
conversations so you can pick the ones that belong to this project, not all of them:

```
remember import ~/Downloads/conversations-000.zip --list
remember import ~/Downloads/conversations-000.zip --pick 3 --pick "lecture slides"
```

## Other assistants

See [`integrations/README.md`](integrations/README.md). In short: run `remember install-cli` to get a `remember`
command, add the MCP server (`python3 …/remember/cli.py mcp`) to your assistant, and run `remember init --write` in
a project so its AGENTS.md / GEMINI.md tells the assistant memory exists. Gemini CLI can install this folder as an
extension. The map, the log and the viewer are the same files whichever assistant you use.

What differs today: only Claude Code saves every turn on its own (it has the hooks and the transcript files this
uses). Elsewhere the assistant saves the conversation with `remember_log` when asked or at the end of a task. Map
updates work everywhere the same way: the assistant hands them to a sub-agent if it has them, or does them itself at
a natural pause.

## Commands

| | |
|---|---|
| `remember status` | what's stored for this project, what isn't mapped yet |
| `remember overview` | the map: threads, rules, open items |
| `remember zoom T3 T7 --q "…"` | open threads together, with verbatim evidence and where each one stopped |
| `remember zoom --recent 5` | the most recently active threads |
| `remember spans <ref>` · `grep <regex>` · `window <from> <to>` | the raw log: by reference, by search, by time |
| `remember ingest claude-code` | this folder's Claude Code sessions on disk |
| `remember log` | add a conversation from text (`## user` / `## assistant`) |
| `remember import <export> --list / --pick` | claude.ai export, selectively |
| `remember show` | start the map viewer and print its link (`atlas` for a standalone file) |
| `remember build` | update the map now (normally automatic) |
| `remember builder-prompt` | how the map is built (any assistant can do it) |

## Requirements

Python 3.9 or newer, nothing else (macOS ships 3.9 with the Xcode command line tools; Linux has it; on Windows install
Python from python.org). Tested on Linux with Python 3.9 and 3.11–3.13; macOS and Windows are expected to work but
haven't been tried yet. On Windows, if `python3` isn't on your PATH, change `python3` to `python` in the MCP configs.

## Privacy and cost

- **Privacy:** everything stays in `~/.remember` on your machine. The only thing that reads it is the assistant you're
  already using, in your own sessions.
- **Cost:** building and updating the map uses your assistant's plan. A couple of weeks of heavy chat (about 280K
  characters) took roughly 225K tokens to map from scratch; updating after a session is usually small.
  `REMEMBER_AUTOBUILD=off` updates only when you ask; `=background` also keeps the map current between sessions with a
  headless job.
- **Removing it:** delete `~/.remember/log/<project>` and `~/.remember/maps/<project>`.

## Feedback I'm after

Did "start memory" work on your project? Did a new session know what it should? Anything it got wrong, missed, or
made up? Screenshots of `remember overview` plus what you asked are the most useful.
