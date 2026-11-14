# remember.me

Memory for AI assistants that survives compaction and new sessions: a verbatim log of your conversations, plus a map
of it (threads with their decisions and open questions, standing rules, to-do lists, procedures), each entry pointing
back to the exact messages it came from. Plain files on your own computer (`~/.remember`), shared by every assistant
you use.

**To set it up, tell your assistant:** "Set up remember.me from github.com/techbossmb/remember-me". It does the rest
and tells you if it needs a click from you.

> Early prototype. Works end to end in the Claude desktop app (macOS) and Claude Code. Gemini CLI, Copilot, Cursor and
> Codex are wired up but not yet tested.

---

## Setup: instructions for the AI assistant

You are setting this up for the user. Do every step yourself with your own tools. Ask the user only for what you
can't do (a click in an app's settings), and then say exactly what to click. Don't hand them commands to run.

### 1. Find which environment you're in, and follow that part

**Claude desktop app (Cowork / chat with plugins).** You can't add a plugin from GitHub yourself; it's a settings step.
Tell the user, in these words or close to them:
> In the Claude desktop app: Customize → Plugins → Add marketplace → enter `techbossmb/remember-me` → Sync, then
> install **remember-me**. Then fully quit and reopen the Claude app (closing the window isn't enough), and say
> "start memory for this project" in a new chat.

Memory and the map viewer run on the user's computer through the desktop app, so the app must be open for memory to
work, including from their phone. The first time, the app asks permission for each memory tool; tell the user to
approve them.

**Claude Code.** Run:
```
claude plugin marketplace add techbossmb/remember-me
claude plugin install remember-me@remember-me
```
Then tell the user to start a new session and say "start memory for this
project". Every turn is saved automatically from then on.

**Gemini CLI.** Run `gemini extensions install https://github.com/techbossmb/remember-me`, then have the user start
a new session.

**Cursor, GitHub Copilot (VS Code), OpenAI Codex, or any MCP client.** Clone the repo to a permanent place
(`git clone https://github.com/techbossmb/remember-me ~/.remember-me`), then add an MCP server named `remember` with
command `python3` and args `["<home>/.remember-me/remember/cli.py", "mcp"]`, in that client's config format:
[`integrations/README.md`](integrations/README.md) has each one. If you can run shell commands, also run
`python3 ~/.remember-me/remember/cli.py install-cli` (gives a `remember` command) and, in the user's project,
`remember init --write` (adds a short memory note to AGENTS.md / GEMINI.md / Copilot instructions). Restart the client.

### 2. Python

remember.me needs Python 3.9+ and nothing else. If you can run commands, check `python3 --version` (Windows:
`python --version`).
- macOS without it: running `python3` pops up an offer to install Apple's developer tools; tell the user to accept.
- Windows without it: the user installs Python from python.org (tick "Add to PATH"). If only `python` exists, use
  `python` instead of `python3` in MCP configs.

### 3. Start memory (when the user asks, in a project)

Read the `remember_guide` tool once and follow it. In short: name the project (in apps that don't run in a project
folder, suggest the name of what the user is working on and confirm once), save what's in your context with
`remember_log`, say plainly if the conversation began earlier than what you have, build the map out of the user's way
(a sub-agent if you have one), then give them the map link from `remember_show_map`.

---

## For the user

**See your map:** ask "show me my memory map". You get a link like http://127.0.0.1:8765 that opens on the computer
where memory lives: topics as a graph, each with its decisions, open questions, the messages behind it, and the rules.
You can rename, merge, move or close topics there. If the link stops loading, ask "restart my memory map"; to have it
always running, ask "keep my memory map running"; to stop it, ask "stop my memory map".

**Older conversations:** starting memory only captures the chat you're in (plus, in Claude Code, earlier sessions
still on disk). For older claude.ai chats, export your data (claude.ai → Settings → Privacy → Export data) and ask
your assistant to import it. It lists the conversations so you pick only the ones for this project.

**Updates** come from GitHub. In the Claude app plugins sync automatically (or tap Sync under Customize → Plugins).
A new chat gets an update right away; an open chat gets it after you fully quit and reopen the app. Your memory is
the same files either way.

**What differs by assistant:** Claude Code saves every turn on its own. Elsewhere (the Claude app included) the
assistant saves at natural pauses; "save this to memory" always works. Map updates happen the same way everywhere.

**Privacy and cost:** everything stays in `~/.remember` on your computer; only the assistant you're using reads it.
Building the map uses your assistant's plan: a couple of weeks of heavy chat took roughly 225K tokens to map from
scratch; updates after a session are small. To remove a project's memory, delete `~/.remember/log/<project>` and
`~/.remember/maps/<project>`.

**Feedback wanted:** did "start memory" work? Did a new chat a few days later know where you left off? Anything it
got wrong, missed, or made up?

---

## Reference (for assistants and developers)

The same operations are available as MCP tools (`remember_*`) and as a CLI (`remember <command>`, or
`python3 remember/cli.py <command>`). Every tool takes `project`.

| | |
|---|---|
| `status` | what's stored for this project, what isn't mapped yet |
| `overview` | the map: threads, rules, open items |
| `zoom T3 T7 --q "…"` · `zoom --recent 5` | open threads together with verbatim evidence; the most recently active |
| `spans <ref>` · `grep <regex>` · `window <from> <to>` | the raw log: by reference, by search, by time |
| `log` | save conversation messages (repeats are skipped) |
| `import <export> --list / --pick` | claude.ai export, selectively |
| `ingest claude-code` | this folder's Claude Code sessions on disk |
| `show` · `viewer start/stop/restart/status/autostart` | the map viewer |
| `builder-prompt` · `chunks` · `chunk` · `ops` · `coverage` · `validate` | building the map |

Settings: `REMEMBER_HOME` (store, default `~/.remember`), `REMEMBER_PROJECT` (pin the project),
`REMEMBER_AUTOBUILD` (`session` default, `background`, `off`). Last-resort viewer stop without remember.me:
`lsof -ti tcp:8765 | xargs kill` (macOS/Linux); `netstat -ano | findstr :8765` then `taskkill /PID <pid> /F` (Windows).
Tested on Linux (Python 3.9, 3.11–3.13) and macOS via the Claude desktop app; Windows not yet tried.
