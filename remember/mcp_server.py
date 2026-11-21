"""remember.me MCP server (stdio, JSON-RPC 2.0, newline-delimited). Stdlib only.

Exposes the same operations as the CLI to any MCP client (Claude, Gemini CLI, Copilot/VS Code, Cursor, Codex, …).
Each tool takes an optional `project`; otherwise the project is resolved from REMEMBER_PROJECT or the server's
working directory (most clients start servers in the workspace folder).
"""
import contextlib
import io
import json
import os
import sys

import cli
import memmap

PROTOCOL = "2025-06-18"
VERSION = "0.5.0"

S = {"type": "string"}
PROJECT = {"project": {"type": "string", "description": "the name of what the user is working on (remember_projects lists existing ones). Needed unless the app runs in the project's own folder; the tool says so if it can't tell."}}

TOOLS = [
    ("remember_guide", "How to use remember.me: read this first if you haven't been told how to use the remember tools.", {}, []),
    ("remember_status", "What is stored for this project, how much is mapped, and what is not mapped yet.", {}, []),
    ("remember_overview", "The project's memory map: every thread with status and summary, standing rules, open items. "
     "Read it at the start of work; then open the threads that matter with remember_zoom.",
     {"ids": {"type": "boolean", "description": "include claim ids (for map builders)"}}, []),
    ("remember_zoom", "Open threads from the overview together: claims with verbatim evidence, where each thread stopped, "
     "neighbours, and raw-log messages outside them that match the question. Pick ids by meaning.",
     {"ids": {"type": "array", "items": S}, "question": S, "recent": {"type": "integer", "description": "also open the N most recently active threads"}}, []),
    ("remember_spans", "Verbatim messages from the raw log, e.g. ['proj/session.md#m0004-m0010'].",
     {"spans": {"type": "array", "items": S}}, ["spans"]),
    ("remember_grep", "Search the raw log (regex, case-insensitive), optionally within a time window.",
     {"pattern": S, "from": S, "to": S}, ["pattern"]),
    ("remember_window", "Everything said between two times (ISO dates or datetimes).", {"from": S, "to": S}, ["from"]),
    ("remember_log", "Save conversation messages to the project's memory: to start memory from what is in your "
     "context, and again at natural pauses so the conversation is kept. Safe to repeat: messages already saved are "
     "skipped. User messages verbatim; source='recalled' when writing from context; ISO timestamps if known.",
     {"session": S, "source": S, "note": S,
      "messages": {"type": "array", "items": {"type": "object", "properties": {"role": {"type": "string", "enum": ["user", "assistant", "summary"]}, "text": S, "ts": S}, "required": ["role", "text"]}}},
     ["messages"]),
    ("remember_show_map", "Start (or reuse) the local memory map viewer and return its link for the user to click. "
     "Use when the user asks to see their memory or map.", {}, []),
    ("remember_projects", "List the projects that have memory. Each tool takes `project`; use the name of what the "
     "user is working on (ask once if unclear).", {}, []),
    ("remember_viewer", "Manage the local map viewer: start, stop (finds it wherever it was started), restart, status, "
     "autostart_on / autostart_off (start it when the user logs in).",
     {"action": {"type": "string", "enum": ["start", "stop", "restart", "status", "autostart_on", "autostart_off"]}}, ["action"]),
    ("remember_import", "Import past conversations from a claude.ai data export (zip or folder on the user's computer). "
     "Without picks it lists the conversations; picks are numbers, uuid prefixes or name substrings, or 'all'.",
     {"path": S, "picks": {"type": "array", "items": S}}, ["path"]),
    ("remember_ingest_sessions", "Bring in Claude Code session files already on the user's computer for a folder "
     "(only useful where Claude Code has been used).", {"folder": S}, []),
    ("remember_mode", "Where this computer keeps the user's memory: here (offline) or in their remember.me account "
     "(online, the same on every device), and switching. Actions: status; connect, then connect_finish once the user "
     "has pressed Allow (signs this computer in to their account, once); go_online (uploads what this computer holds; "
     "replace=[projects] when the user chose this computer's copy over a different online one); go_offline (downloads "
     "projects here; keep_online=false removes them from the cloud, true keeps a read-only copy online). The first "
     "time someone goes offline, ask which they want.",
     {"action": {"type": "string", "enum": ["status", "connect", "connect_finish", "go_online", "go_offline"]},
      "keep_online": {"type": "boolean"}, "projects": {"type": "array", "items": S},
      "replace": {"type": "array", "items": S}}, ["action"]),
    ("remember_builder_prompt", "Instructions for building or updating the map from the raw log (chunk by chunk).", {}, []),
    ("remember_chunks", "List replay chunks for the builder; new_only lists only what is not mapped yet.",
     {"new_only": {"type": "boolean"}}, []),
    ("remember_chunk", "Print one replay chunk (time-ordered messages) for the builder.", {"n": {"type": "integer"}}, ["n"]),
    ("remember_apply_ops", "Apply map ops (see remember_builder_prompt) and rebuild.", {"ops": {"type": "array", "items": {"type": "object"}}}, ["ops"]),
    ("remember_coverage", "Which substantive messages are not yet reachable from the map.", {}, []),
    ("remember_validate", "Check the map for problems after building.", {}, []),
]


# What each tool does to the user's data, as MCP tool annotations, so an app can treat reading and writing differently
# (e.g. let reads through, ask before writes). Everything is local: no tool reaches outside the user's computer.
READS = {"remember_guide", "remember_status", "remember_overview", "remember_zoom", "remember_spans", "remember_grep",
         "remember_window", "remember_projects", "remember_builder_prompt", "remember_chunks", "remember_chunk",
         "remember_coverage", "remember_validate"}
# Writes that add to memory without deleting anything, and are safe to repeat (repeats are skipped / no-ops).
ADDS = {"remember_log", "remember_import", "remember_ingest_sessions"}
# remember_apply_ops changes the map: it can retire or merge threads, but map changes are appended as events, so
# nothing is lost (the log itself is never touched). It is not safe to repeat blindly, hence not idempotent.
# remember_show_map / remember_viewer start, stop or schedule the local viewer: no memory changes.
LOCAL_PROCESS = {"remember_show_map", "remember_viewer"}
MOVES = {"remember_mode"}  # moves projects between this computer and the account; nothing is deleted without a copy


def _annotations(name):
    if name in READS:
        return {"readOnlyHint": True, "openWorldHint": False}
    if name in ADDS:
        return {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    if name in MOVES:
        return {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
    if name in LOCAL_PROCESS:
        return {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    return {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}


def _tool_defs():
    return [{"name": n, "description": d, "annotations": _annotations(n),
             "inputSchema": {"type": "object", "properties": {**props, **PROJECT}, "required": req}}
            for n, d, props, req in TOOLS]


def _run(argv, store=None):
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cli.main(argv, store=store)
    except SystemExit as e:
        if e.code not in (None, 0):
            buf.write(str(e.code))
    return buf.getvalue().strip()


def call(store, name, args):
    run = lambda argv: _run(argv, store)  # noqa: E731
    pj = ["--project", args["project"]] if args.get("project") else []
    if name == "remember_status":
        return run(["status"] + pj)
    if name == "remember_overview":
        return run(["overview"] + pj + (["--ids"] if args.get("ids") else []))
    if name == "remember_zoom":
        argv = ["zoom"] + list(args.get("ids") or []) + pj
        if args.get("question"):
            argv += ["--q", args["question"]]
        if args.get("recent"):
            argv += ["--recent", str(args["recent"])]
        return run(argv)
    if name == "remember_spans":
        return run(["spans"] + list(args["spans"]) + pj)
    if name == "remember_grep":
        argv = ["grep", args["pattern"]] + pj
        if args.get("from"):
            argv += ["--from", args["from"]]
        if args.get("to"):
            argv += ["--to", args["to"]]
        return run(argv)
    if name == "remember_window":
        return run(["window", args["from"]] + ([args["to"]] if args.get("to") else []) + pj)
    if name == "remember_log":
        p = cli.resolve_project(store, args.get("project"))
        msgs = [(m["role"], m.get("ts", ""), m["text"]) for m in args["messages"]]
        return cli.log_messages(store, p, args.get("session") or "recalled", msgs, args.get("source") or "recalled", args.get("note"))
    if name == "remember_show_map":
        return run(["show"] + pj)
    if name == "remember_projects":
        ps = cli.list_projects(store)
        return ("projects with memory: " + ", ".join(ps)) if ps else "no projects have memory yet"
    if name == "remember_viewer":
        act = args["action"]
        sub = {"autostart_on": ["autostart", "on"], "autostart_off": ["autostart", "off"]}.get(act, [act])
        return run(["viewer"] + sub)
    if name == "remember_import":
        argv = ["import", args["path"]] + pj
        for p in args.get("picks") or []:
            argv += ["--pick", p]
        return run(argv + ([] if args.get("picks") else ["--list"]))
    if name == "remember_ingest_sessions":
        folder = args.get("folder") or os.getcwd()
        files = cli.claude_code_sessions(folder)
        if not files:
            return f"no Claude Code session files on this computer for {folder}"
        return cli.ingest_claude_code(store, cli.resolve_project(store, args.get("project"), cwd=folder), files)
    if name == "remember_guide":
        return guide_text()
    if name == "remember_mode":
        import sync
        act = args["action"]
        if act == "status":
            return sync.status(store)
        if act == "connect":
            return sync.connect(store)
        if act == "connect_finish":
            return sync.connect_finish(store)
        if act == "go_online":
            return sync.go_online(store, replace=tuple(cli.slug(p) for p in args.get("replace") or ()))
        if act == "go_offline":
            if "keep_online" not in args:
                raise ValueError("ask the user first: keep a read-only copy in the cloud while offline "
                                 "(keep_online=true), or remove it from the cloud (keep_online=false)?")
            return sync.go_offline(store, keep_online=bool(args["keep_online"]),
                                   projects=[cli.slug(p) for p in args.get("projects") or []] or None)
        raise ValueError(f"unknown action {act}")
    if name == "remember_builder_prompt":
        return run(["builder-prompt"])
    if name == "remember_chunks":
        return run(["chunks"] + (["--new"] if args.get("new_only") else []) + pj)
    if name == "remember_chunk":
        return run(["chunk", str(args["n"])] + pj)
    if name == "remember_apply_ops":
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(args["ops"], f)
        return run(["ops", f.name] + pj)
    if name == "remember_coverage":
        return run(["coverage"] + pj)
    if name == "remember_validate":
        return run(["validate"] + pj)
    raise ValueError(f"unknown tool {name}")


LOCAL_TOOLS = {"remember_show_map", "remember_viewer", "remember_import", "remember_ingest_sessions", "remember_mode"}
ONLINE_NOTE = ("You are connected to remember.me online: the user's memory is kept on the remember.me service under "
               "their account, so it is the same from any device or assistant they connect. Always pass `project`. "
               "Tools that only exist on the user's own computer (local viewer controls, importing files from their "
               "disk) are not available here.\n\n")


def guide_text(online=False):
    text = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "guide.md"), encoding="utf-8").read()
    return ONLINE_NOTE + text if online else text


WRITE_TOOLS = {"remember_log", "remember_apply_ops"}


def respond(store, msg, online=False, show_map=None, readonly=None):
    """One JSON-RPC message in, the response out (None for notifications). Shared by stdio and the online service.
    Online, `show_map(project or None)` returns a private link to the hosted map page."""
    local_only = LOCAL_TOOLS - ({"remember_show_map"} if show_map else set())
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:  # notification
        return None
    try:
        if method == "initialize":
            res = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "remember-me", "version": VERSION},
                   "instructions": guide_text(online)}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": [t for t in _tool_defs() if not (online and t["name"] in local_only)]}
        elif method == "tools/call":
            name = params["name"]
            try:
                args = params.get("arguments") or {}
                if online and name in local_only:
                    raise ValueError(f"{name} works only with memory on the user's computer")
                if readonly and name in WRITE_TOOLS and args.get("project") and cli.slug(args["project"]) in readonly:
                    raise ValueError(readonly[cli.slug(args["project"])])
                if online and name == "remember_show_map":
                    text = show_map(args.get("project"))
                else:
                    text = guide_text(online) if name == "remember_guide" else call(store, name, args)
                res = {"content": [{"type": "text", "text": text or "(empty)"}], "isError": False}
            except Exception as e:  # tool errors go back to the model, not the protocol
                res = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
        else:
            raise KeyError(method)
        return {"jsonrpc": "2.0", "id": mid, "result": res}
    except KeyError:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def _local_or_forward(store, msg):
    """On a computer in online mode, memory tools go to the user's account; switching and the guide stay here."""
    import sync
    if msg.get("method") != "tools/call" or msg.get("id") is None or sync.mode(store) != "online":
        return respond(store, msg)
    name = (msg.get("params") or {}).get("name")
    if name in ("remember_mode", "remember_guide"):
        return respond(store, msg)
    if name == "remember_viewer":
        text = f"memory is online, so the map is at {sync.server(store)}/map (no local viewer needed)"
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": text}], "isError": False}}
    if name in ("remember_import", "remember_ingest_sessions"):
        text = ("error: importing works with memory on this computer for now: go offline (remember_mode go_offline), "
                "import, then go back online")
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": text}], "isError": True}}
    return sync.forward(store, msg)


def serve(store):
    memmap.TOOL_PREFIX = "remember_"
    cli.memmap.TOOL_PREFIX = "remember_"
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        out = _local_or_forward(store, msg)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
