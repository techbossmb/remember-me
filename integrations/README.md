# Using remember.me from other assistants

Everything is plain files in `~/.remember`, so a map built with one assistant works in any other.
There are two ways in, and you can use both:

- **CLI**: any assistant that can run shell commands. Run `python3 <this folder>/remember/cli.py install-cli` once to get a
  `remember` command, then `remember init --write` in a project to add a short memory note to its AGENTS.md /
  CLAUDE.md / GEMINI.md / Copilot instructions (`AGENTS-snippet.md` is the same note, to paste by hand).
- **MCP**: the same tools (`remember_overview`, `remember_zoom`, `remember_log`, …) for any MCP client.
  Server command: `python3 <this folder>/remember/cli.py mcp`.

Replace `/path/to/remember-me` below with where you put this folder. Only Claude Code has been tested so far;
the others follow each tool's documented MCP config format.

**Gemini CLI**: install as an extension (adds the MCP server and the GEMINI.md note):
`gemini extensions install /path/to/remember-me` (or the GitHub URL). Or add to `~/.gemini/settings.json`:
```json
{ "mcpServers": { "remember": { "command": "python3", "args": ["/path/to/remember-me/remember/cli.py", "mcp"] } } }
```

**GitHub Copilot in VS Code**: `.vscode/mcp.json` in the project (or your user MCP config):
```json
{ "servers": { "remember": { "type": "stdio", "command": "python3", "args": ["/path/to/remember-me/remember/cli.py", "mcp"] } } }
```

**Cursor**: `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json`:
```json
{ "mcpServers": { "remember": { "command": "python3", "args": ["/path/to/remember-me/remember/cli.py", "mcp"] } } }
```

**OpenAI Codex CLI**: `~/.codex/config.toml`:
```toml
[mcp_servers.remember]
command = "python3"
args = ["/path/to/remember-me/remember/cli.py", "mcp"]
```

**Which project?** The project is chosen from the folder the assistant runs in (`remember init` names it; subfolders
belong to their parent). To pin it, set `REMEMBER_PROJECT=<name>` in the server's environment, or pass `project`
to any tool.

**Automatic capture** (every turn written to the log) needs a hook after each turn, which only Claude Code has wired
up so far. Elsewhere, the assistant saves the conversation with `remember_log` when asked or at the end of a task, and
a claude.ai export can be imported with `remember import`.

**Map updates** are done by your assistant, outside your conversation: the overview tells it when messages aren't
mapped yet, and it hands them to a sub-agent if it has one, or does them at a natural pause. To keep the map current
even with no session open, set `REMEMBER_AUTOBUILD=background`; that uses Claude Code's headless mode if installed, or
your assistant's headless command via `REMEMBER_BUILDER_CMD` with `{prompt}` where the instructions go (for example
`REMEMBER_BUILDER_CMD='gemini -p {prompt}'`; that assistant needs the remember MCP server configured).

**Showing the map**: the `remember_show_map` tool (or `remember show`) opens it in the user's browser.
