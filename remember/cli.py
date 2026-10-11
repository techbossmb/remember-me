#!/usr/bin/env python3
"""remember: long-term memory for AI agents, as plain files any agent can use.

Store (default ~/.remember, override REMEMBER_HOME):
    log/<project>/<session>.md      verbatim conversation, append-only, one file per session
    maps/<project>/events.jsonl     the map, as an append-only event log (replayed in time order)
    maps/<project>/sources.json     timeline of the project's log files (kept up to date automatically)
    projects.json                   which folder belongs to which project

Nothing here is tied to one assistant: the files are markdown + JSON, the CLI is stdlib Python, and
`remember mcp` serves the same tools over MCP for any client that speaks it.

Using memory
  remember status                         what's stored for this project, what isn't mapped yet
  remember overview                       the session-start map: every thread, rules, open items
  remember zoom <id> [<id>…] [--q Q]      open threads together (+ neighbours, + raw-log hits for Q)
  remember zoom --recent N                most recently active threads ("where were we")
  remember spans <file#mA-mB> …           verbatim messages
  remember grep <regex> [--from D --to D] search the raw log
  remember window <from> [<to>]           everything said in a time window
  remember find <words>                   keyword fallback

Getting history in
  remember ingest claude-code [--current F]   this folder's Claude Code sessions already on disk
  remember log --session S [--source recalled] < conversation.md
                                          add messages an agent has in context ("## user" / "## assistant")
  remember import <export.zip|dir> --list     claude.ai data export: list conversations
  remember import <export.zip|dir> --pick N [--pick N …]   import only the chosen ones (number, uuid or name)

Building the map (automatic. REMEMBER_AUTOBUILD=session (default): your own assistant updates it through a sub-agent
at a natural pause, outside your conversation; =background: a headless job keeps it current; =off: only when asked)
  remember build [--background]               update the map now, using Claude Code or REMEMBER_BUILDER_CMD
  remember chunks [--new]  |  chunk N  |  ops FILE.json|-  |  op JSON  |  coverage  |  validate
                                              the same by hand: any assistant can follow `remember builder-prompt`

Setup
  remember init [--project NAME] [--write]    name this folder's project; print/append the agent snippet
  remember install-cli [DIR]                  put a `remember` command on PATH (default ~/.local/bin)
  remember mcp                                MCP server on stdio
  remember atlas [--open] [--all] [--out F]   static HTML view of the map (one file, opens in any browser)
  remember show [--open]                      start (or reuse) the live map viewer and print its link
  remember viewer start|stop|restart|status   manage the viewer; `stop` finds it wherever it was started
  remember viewer autostart [on|off]          start the viewer when you log in
  remember serve [PORT] [--open]              run the live viewer in the foreground (all projects; edit status/merge/move)
  remember hook                               Claude Code hook entry point (capture + session start)
"""
import argparse
import glob
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memmap  # noqa: E402
import capture  # noqa: E402

memmap.TOOL_PREFIX = "remember "
OVERVIEW_BUDGET = 9000  # Claude Code caps injected session-start context at 10,000 characters


# ---------------------------------------------------------------- store + project resolution

def store_dir():
    return os.path.abspath(os.path.expanduser(os.environ.get("REMEMBER_HOME") or "~/.remember"))


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "default"


def _registry(store):
    p = os.path.join(store, "projects.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}


def _save_registry(store, reg):
    os.makedirs(store, exist_ok=True)
    with open(os.path.join(store, "projects.json"), "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=1)


def resolve_project(store, explicit=None, cwd=None):
    if explicit:
        return slug(explicit)
    if os.environ.get("REMEMBER_PROJECT"):
        return slug(os.environ["REMEMBER_PROJECT"])
    cwd = os.path.abspath(cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    reg = _registry(store)
    d = cwd
    while True:  # a subfolder belongs to its registered parent
        if d in reg:
            return reg[d]
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return slug(os.path.basename(cwd))


def list_projects(store):
    names = set()
    for sub in ("log", "maps"):
        d = os.path.join(store, sub)
        if os.path.isdir(d):
            names |= {n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n))}
    return sorted(names)


def enabled(store, project):
    return os.path.isdir(os.path.join(store, "log", project)) or os.path.isdir(os.path.join(store, "maps", project))


def refresh_sources(store, project):
    """Rebuild maps/<p>/sources.json from the log files: start/end from message timestamps."""
    ldir = os.path.join(store, "log", project)
    out = []
    for f in sorted(glob.glob(os.path.join(ldir, "*.md"))):
        if f.endswith(".compactions.md"):
            continue
        text = open(f, encoding="utf-8").read()
        ts = [m.group(3) for m in memmap.HDR.finditer(text) if m.group(3)]
        rel = f"{project}/{os.path.basename(f)}"
        meta = {}
        mp = f[:-3] + ".meta.json"
        if os.path.exists(mp):
            meta = json.load(open(mp, encoding="utf-8"))
        if ts:
            out.append({"file": rel, "start": min(ts), "end": max(ts), **meta})
        elif memmap.HDR.search(text):
            now = meta.get("logged_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            out.append({"file": rel, "start": now, "end": now, "order_uncertain": True, **meta})
    out.sort(key=lambda s: s["start"])
    mdir = os.path.join(store, "maps", project)
    os.makedirs(mdir, exist_ok=True)
    with open(os.path.join(mdir, "sources.json"), "w", encoding="utf-8") as f:
        json.dump({"sources": out}, f, indent=1)
    memmap._LOG_CACHE.clear()
    return out


def load(store, project):
    if not os.path.exists(os.path.join(store, "maps", project, "sources.json")):
        refresh_sources(store, project)
    return memmap.load(store, project)


# ---------------------------------------------------------------- status

def unmapped_msgs(g, tl, min_chars=80):
    """Substantive messages no node, claim or rule points to (acks and small talk don't count)."""
    covered = memmap.covered_ids(g) if g.get("nodes") else set()
    return [r for r in tl if (r[1], r[2]) not in covered and len(r[4]) >= min_chars]


def status(store, project):
    srcs = refresh_sources(store, project) if enabled(store, project) else []
    lines = [f"project: {project}   store: {store}"]
    if not srcs:
        lines.append("nothing stored for this project yet.")
        cc = claude_code_sessions(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        if cc:
            lines.append(f"{len(cc)} Claude Code session file(s) for this folder exist on disk: "
                         "`remember ingest claude-code` brings them in.")
        lines.append("other ways in: `remember log` (what's in the agent's context), `remember import` (claude.ai export).")
        return "\n".join(lines)
    g = load(store, project)
    tl = memmap.timeline(store, project)
    covered = memmap.covered_ids(g)
    nodes = [n for n in g.get("nodes", []) if n.get("type") != "project"]
    events = memmap.read_events(store, project)
    last_at = max([e.get("at", "") for e in events], default="")
    unmapped = unmapped_msgs(g, tl)
    lines.append(f"log: {len(srcs)} session(s), {len(tl)} messages, {srcs[0]['start'][:10]} → {srcs[-1]['end'][:10]}")
    summ = [r for r in tl if r[3] == "summary"]
    if summ:
        lines.append(f"note: {len(summ)} message(s) are harness summaries of earlier turns; the verbatim turns before "
                     "them were not on disk. A claude.ai/assistant export can fill that gap.")
    rec = [s for s in srcs if s.get("source") == "recalled"]
    if rec:
        lines.append(f"note: {len(rec)} session(s) were written from an agent's context (recalled, not verbatim).")
    if events:
        lines.append(f"map: {len(nodes)} threads, {len(g.get('rules', []))} rules, built up to {last_at[:16]}; "
                     f"{len(covered)} messages referenced")
    else:
        lines.append("map: not built yet")
    if build_running(store, project):
        lines.append("a background map update is running now")
    if unmapped:
        lines.append(f"unmapped: {len(unmapped)} substantive messages not yet reachable from the map "
                     f"({unmapped[0][0][:16]} → {unmapped[-1][0][:16]}). `remember chunks --new` lists them for the builder.")
    return "\n".join(lines)


# ---------------------------------------------------------------- ingest: Claude Code sessions on disk

def claude_code_dir(cwd):
    return os.path.join(os.path.expanduser("~/.claude/projects"), re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd)))


def claude_code_sessions(cwd):
    d = claude_code_dir(cwd)
    return sorted(glob.glob(os.path.join(d, "*.jsonl")), key=os.path.getmtime) if os.path.isdir(d) else []


def ingest_claude_code(store, project, files):
    out_dir = os.path.join(store, "log", project)
    report = []
    for f in files:
        entries = list(capture.read_claude_code(f))
        msgs = [e for e in entries if e[1] == "msg"]
        if not msgs and not any(e[1] == "summary" for e in entries):
            continue
        sid = os.path.splitext(os.path.basename(f))[0]
        day = next((e[3][:10] for e in entries if e[3]), "undated")
        session = f"cc-{day}-{sid[:8]}"
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, ".lock"), "a+", encoding="utf-8") as lk:
            memmap._lock(lk)  # Stop and SessionEnd hooks can fire together
            new = capture.append_log(entries, out_dir, session)
        summaries = sum(1 for e in entries if e[1] == "summary")
        report.append(f"{session}: +{new} messages" + (f", {summaries} compaction summary(ies): earlier turns of this "
                                                       "session survive only as a summary" if summaries else ""))
    refresh_sources(store, project)
    return "\n".join(report) or "no Claude Code messages found"


# ---------------------------------------------------------------- log: messages from an agent's context

def parse_conversation(text):
    """'## user' / '## assistant' (optionally '· <ISO time>') sections -> [(role, ts, text)]."""
    parts = re.split(r"^##\s*(user|assistant|human|summary)\b[^\n]*?(?:·\s*(\S+))?\s*$", text, flags=re.M | re.I)
    out = []
    for i in range(1, len(parts) - 2, 3):
        role = parts[i].lower().replace("human", "user")
        body = parts[i + 2].strip()
        if body:
            out.append((role, parts[i + 1] or "", body))
    return out


def log_messages(store, project, session, msgs, source="recalled", note=None):
    """msgs: [(role, ts, text)]. Recalled = written from an agent's context, not a verbatim transcript."""
    out_dir = os.path.join(store, "log", project)
    session = slug(session)
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    # Saving the same conversation twice (an assistant saving at each pause, or a hook that already captured it)
    # must not duplicate it: skip messages whose text the project's log already holds for that speaker.
    norm = lambda s: " ".join((s or "").split())
    have = set()
    if os.path.isdir(out_dir):
        refresh_sources(store, project)
        have = {(r[3], norm(r[4])) for r in memmap.timeline(store, project)}
    fresh = [(r, ts, t) for r, ts, t in msgs
             if (r, norm(t)) not in have and not any(norm(t) in b for rr, b in have if rr == r and len(norm(t)) > 40)]
    skipped = len(msgs) - len(fresh)
    import hashlib
    entries = [(f"{source}:{session}:{stamp}:{i}:" + hashlib.sha1(f"{r}\n{t}".encode("utf-8")).hexdigest()[:12],
                "msg", r, ts, t) for i, (r, ts, t) in enumerate(fresh)]
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, ".lock"), "a+", encoding="utf-8") as lk:
        memmap._lock(lk)
        new = capture.append_log(entries, out_dir, session)
    meta = {"source": source, "logged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if note:
        meta["note"] = note
    with open(os.path.join(out_dir, f"{session}.meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    refresh_sources(store, project)
    return f"{session}: +{new} messages ({source})" + (f", {skipped} already saved" if skipped else "")


# ---------------------------------------------------------------- import: claude.ai data export, selectively

def _load_export(path):
    if os.path.isdir(path):
        for f in glob.glob(os.path.join(path, "**", "conversations.json"), recursive=True):
            return json.load(open(f, encoding="utf-8"))
        zips = glob.glob(os.path.join(path, "conversations*.zip"))
        if zips:
            path = zips[0]
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.endswith("conversations.json")), None)
            if name:
                return json.load(io.TextIOWrapper(z.open(name), encoding="utf-8"))
            inner = next((n for n in z.namelist() if n.startswith("conversations") and n.endswith(".zip")), None)
            if inner:
                with zipfile.ZipFile(io.BytesIO(z.read(inner))) as z2:
                    n2 = next(n for n in z2.namelist() if n.endswith("conversations.json"))
                    return json.load(io.TextIOWrapper(z2.open(n2), encoding="utf-8"))
    raise SystemExit(f"no conversations.json found in {path} (pass the conversations-*.zip, the full export, or its folder)")


def import_export(store, project, path, picks=None, list_only=False):
    convs = sorted(_load_export(path), key=lambda c: c.get("updated_at") or "", reverse=True)
    if list_only or not picks:
        rows = [f"{i + 1:3}. {(c.get('name') or '(untitled)')[:60]:60}  {len(c.get('chat_messages') or []):4} msgs  "
                f"{(c.get('created_at') or '')[:10]} → {(c.get('updated_at') or '')[:10]}  {c['uuid'][:8]}"
                for i, c in enumerate(convs)]
        return ("conversations in this export (newest first). Import some with --pick <number|uuid|name> (repeatable), "
                "or --pick all:\n" + "\n".join(rows))
    chosen = []
    for p in picks:
        if p == "all":
            chosen = convs
            break
        if p.isdigit() and 1 <= int(p) <= len(convs):
            chosen.append(convs[int(p) - 1])
            continue
        m = [c for c in convs if c["uuid"].startswith(p) or p.lower() in (c.get("name") or "").lower()]
        if not m:
            raise SystemExit(f"no conversation matches {p!r}; run with --list")
        chosen += m
    tmp = os.path.join(store, ".import-tmp.json")
    os.makedirs(store, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(convs, f)
    report = []
    try:
        for c in {c["uuid"]: c for c in chosen}.values():
            session = "ai-" + slug((c.get("name") or "chat")[:40]) + "-" + c["uuid"][:6]
            new = capture.append_log(list(capture.read_claude_export(tmp, c["uuid"])), os.path.join(store, "log", project), session)
            report.append(f"{session}: +{new} messages")
    finally:
        os.remove(tmp)
    refresh_sources(store, project)
    return "\n".join(report)



# ---------------------------------------------------------------- automatic map updates

AUTOBUILD_MIN = int(os.environ.get("REMEMBER_AUTOBUILD_MIN") or 12)


def autobuild_mode():
    """session (default): the user's own assistant updates the map through a sub-agent, out of the conversation;
    background: a headless job does it, so the map stays current with no session open; off: only when asked."""
    v = (os.environ.get("REMEMBER_AUTOBUILD") or "session").lower()
    return {"0": "off", "false": "off", "no": "off", "1": "session"}.get(v, v)
CHILD_ENV_DROP = ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
                  "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED")

BUILD_PROMPT = """You are the remember.me map builder for project '{p}'. Use only the remember_* tools.
1. Call remember_builder_prompt and follow it exactly. Where it says `remember <command>`, use the matching tool:
   overview --ids -> remember_overview(ids=true); chunks --new -> remember_chunks(new_only=true);
   chunk N -> remember_chunk(n=N); ops -> remember_apply_ops(ops=[...]); coverage -> remember_coverage;
   validate -> remember_validate.
2. Map only what is new: remember_chunks(new_only=true), then each listed chunk in order: read it, apply its ops.
3. Finish with remember_coverage and remember_validate.
Reply with one line: threads/rules changed and coverage."""


def _runner():
    return f'"{sys.executable}" "{os.path.join(HERE, "cli.py")}"' if " " in sys.executable + HERE else \
        f"{sys.executable} {os.path.join(HERE, 'cli.py')}"


def builder_command(project):
    """The assistant CLI that builds the map in the background. REMEMBER_BUILDER_CMD overrides, e.g.
    'gemini -p {prompt}' or 'codex exec {prompt}'; otherwise Claude Code if installed."""
    prompt = BUILD_PROMPT.format(p=project)
    custom = os.environ.get("REMEMBER_BUILDER_CMD")  # the assistant needs the remember MCP server configured
    if custom:
        return [prompt if x == "{prompt}" else x for x in shlex.split(custom)]
    claude = shutil.which("claude")
    if claude:
        mcp = {"mcpServers": {"remember": {"command": sys.executable, "args": [os.path.join(HERE, "cli.py"), "mcp"],
                                           "env": {"REMEMBER_PROJECT": project, "REMEMBER_HOME": store_dir(),
                                                   "REMEMBER_BUILDING": "1"}}}}
        return [claude, "-p", prompt, "--mcp-config", json.dumps(mcp), "--strict-mcp-config",
                "--allowedTools", "mcp__remember"]
    return None


def _lock_path(store, project):
    return os.path.join(store, "maps", project, ".building")


def build_running(store, project):
    lp = _lock_path(store, project)
    return os.path.exists(lp) and time.time() - os.path.getmtime(lp) < 1800


def maybe_autobuild(store, project, reason=""):
    """Start a background map update when enough is unmapped. Returns a short note, or None."""
    if autobuild_mode() != "background" or os.environ.get("REMEMBER_BUILDING") or not enabled(store, project):
        return None
    if build_running(store, project):
        return "a map update is running in the background"
    g = load(store, project)
    n = len(unmapped_msgs(g, memmap.timeline(store, project)))
    if n < (1 if reason in ("SessionEnd", "PreCompact") else AUTOBUILD_MIN) or not builder_command(project):
        return None
    with open(_lock_path(store, project), "w", encoding="utf-8") as f:
        json.dump({"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "unmapped": n, "reason": reason}, f)
    env = {k: v for k, v in os.environ.items() if k not in CHILD_ENV_DROP}
    env.update(REMEMBER_BUILDING="1", REMEMBER_PROJECT=project)
    log = open(os.path.join(store, "build.log"), "a", encoding="utf-8")
    kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen([sys.executable, os.path.join(HERE, "cli.py"), "build", "--project", project], cwd=store, env=env,
                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **kw)
    return f"started a background map update for {n} unmapped messages"


def run_build(store, project):
    """Foreground: run the builder assistant now (used by the background job, or directly)."""
    cmd = builder_command(project)
    if not cmd:
        raise SystemExit("no builder available: install Claude Code, or set REMEMBER_BUILDER_CMD (e.g. 'gemini -p {prompt}'); "
                         "or ask your assistant to follow `remember builder-prompt`.")
    env = {k: v for k, v in os.environ.items() if k not in CHILD_ENV_DROP}
    env.update(REMEMBER_BUILDING="1", REMEMBER_PROJECT=project)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] build {project}: {os.path.basename(cmd[0])}", flush=True)
    try:
        r = subprocess.run(cmd, cwd=store, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=3600)
        print((r.stdout or "").strip()[-2000:], (r.stderr or "").strip()[-1000:], sep="\n", flush=True)
    finally:
        try:
            os.remove(_lock_path(store, project))
        except OSError:
            pass
        memmap._LOG_CACHE.clear()
        memmap.save_cache(store, project)
    print(status(store, project), flush=True)


# ---------------------------------------------------------------- the viewer, for agents

def show_map(store, project, open_browser=False):
    """Make sure the viewer is running (start it, or replace a stale one) and return its link."""
    import viewer
    port = viewer.start(store)
    url = f"http://127.0.0.1:{port}/?project={project}"
    if open_browser:
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return f"Memory map: {url}"


# ---------------------------------------------------------------- Claude Code hook

SNIPPET = """## Long-term memory (remember.me)
This project keeps its conversations and a map of them in plain files on this computer (`~/.remember`).
Use the remember_* tools (MCP server: `python3 <remember.me folder>/remember/cli.py mcp`) and follow `remember_guide`:
read `remember_overview` at the start of work, open threads with `remember_zoom`, save new turns with `remember_log`
at natural pauses, and keep the map current when `remember_status` says messages aren't mapped yet.
Without the tools, the same commands exist as `{cmd} <command>` (`{cmd} --help`).
"""


UPDATE_NOTE = ("remember.me: {n} messages aren't in the memory map yet. After you finish what the user is asking, "
               "update it without involving the user: hand it to a sub-agent so the work stays out of this conversation "
               "(in Claude Code or Cowork, the remember-me:builder agent, with the project name '{p}'); if you can't start "
               "sub-agents, follow remember_builder_prompt for the new chunks only. No need to mention it unless asked.")


def session_update_note(store, project, n, reason):
    """In session mode, nudge the user's own assistant (once per batch of new messages)."""
    if autobuild_mode() != "session" or not n:
        return None
    if reason == "Stop":
        if n < AUTOBUILD_MIN:
            return None
        mark = os.path.join(store, "maps", project, ".nudged")
        last = int(open(mark, encoding="utf-8").read() or 0) if os.path.exists(mark) else 0
        if n < last + AUTOBUILD_MIN and last <= n:
            return None
        with open(mark, "w", encoding="utf-8") as f:
            f.write(str(n))
    return UPDATE_NOTE.format(n=n, p=project)


def hook(store):
    data = json.load(sys.stdin)
    if os.environ.get("REMEMBER_BUILDING"):
        return  # the background builder's own session: don't capture or inject
    event = data.get("hook_event_name", "")
    cwd = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    project = resolve_project(store, cwd=cwd)
    if event == "SessionStart":
        if enabled(store, project) and os.environ.get("REMEMBER_VIEWER", "1") != "0":
            try:
                import viewer
                viewer.start(store)
            except BaseException:
                pass
        if enabled(store, project):
            g = load(store, project)
            ov = memmap.render(g, budget=OVERVIEW_BUDGET - 700) if g.get("nodes") else ""
            n = len(unmapped_msgs(g, memmap.timeline(store, project)))
            note = maybe_autobuild(store, project, "SessionStart")
            if note:
                state = f"remember.me: {note}; the map below may lag the last few messages (the raw log is complete)."
            else:
                state = session_update_note(store, project, n, "SessionStart") or \
                    "remember.me memory for this project; use the remember_* tools (remember_guide)."
            ctx = state + "\n\n" + ov
        else:
            n = len(claude_code_sessions(cwd))
            ctx = ("remember.me (long-term memory) is installed but not started for this folder. "
                   + (f"{n} earlier Claude Code session file(s) for this folder are on disk and could be mapped. " if n else "")
                   + "If the user wants it, the remember skill starts it from what is already available.")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx[:OVERVIEW_BUDGET]}}))
        return
    if not enabled(store, project):
        return  # capture is opt-in per project: nothing is written until the user starts memory here
    tp = data.get("transcript_path")
    if tp and os.path.exists(tp):
        ingest_claude_code(store, project, [tp])
    if maybe_autobuild(store, project, event) or event != "Stop":
        return
    g = load(store, project)
    note = session_update_note(store, project, len(unmapped_msgs(g, memmap.timeline(store, project))), "Stop")
    if note:  # reaches the assistant on its next turn
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": note}}))


# ---------------------------------------------------------------- main

def main(argv=None):
    for s in (sys.stdout, sys.stderr, sys.stdin):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    store = store_dir()
    ap = argparse.ArgumentParser(prog="remember", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--project")
    ap.add_argument("--q")
    ap.add_argument("--recent", type=int, default=0)
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--budget", type=int, default=OVERVIEW_BUDGET)
    ap.add_argument("--size", type=int, default=20)
    ap.add_argument("--new", action="store_true")
    ap.add_argument("--current")
    ap.add_argument("--session")
    ap.add_argument("--source", default="recalled")
    ap.add_argument("--note")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--pick", action="append")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--by", default="builder", choices=["builder", "user", "dream"])
    ap.add_argument("--background", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--open", action="store_true", help="atlas/serve: open in the browser")
    ap.add_argument("--all", action="store_true", help="atlas: every project, not just this folder's")
    ap.add_argument("--ids", action="store_true")
    ap.add_argument("-i", action="store_true")
    a = ap.parse_args(argv)
    p = resolve_project(store, a.project)
    cmd = a.cmd

    if cmd == "hook":
        try:
            hook(store)
        except Exception as e:  # never break the user's session
            os.makedirs(store, exist_ok=True)
            with open(os.path.join(store, "hook.err"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {e!r}\n")
        return
    if cmd == "install-cli":
        win = os.name == "nt"
        bindir = os.path.expanduser(a.args[0] if a.args else ("~/AppData/Local/remember/bin" if win else "~/.local/bin"))
        os.makedirs(bindir, exist_ok=True)
        cli_path = os.path.join(HERE, "cli.py")
        if win:
            shim = os.path.join(bindir, "remember.cmd")
            with open(shim, "w", encoding="utf-8") as f:
                f.write(f'@echo off\r\n"{sys.executable}" "{cli_path}" %*\r\n')
        else:
            shim = os.path.join(bindir, "remember")
            with open(shim, "w", encoding="utf-8", newline="\n") as f:
                f.write(f'#!/bin/sh\nexec "{sys.executable}" "{cli_path}" "$@"\n')
            os.chmod(shim, 0o755)
        on_path = os.path.normcase(os.path.abspath(bindir)) in [os.path.normcase(os.path.abspath(x)) for x in os.environ.get("PATH", "").split(os.pathsep) if x]
        print(f"installed {shim}" + ("" if on_path else f"  (add {bindir} to your PATH)"))
        return
    if cmd == "mcp":
        import mcp_server
        mcp_server.serve(store)
        return
    if cmd == "init":
        cwd = os.path.abspath(os.getcwd())
        reg = _registry(store)
        reg[cwd] = slug(a.project) if a.project else reg.get(cwd, slug(os.path.basename(cwd)))
        _save_registry(store, reg)
        os.makedirs(os.path.join(store, "log", reg[cwd]), exist_ok=True)
        snip = SNIPPET.format(cmd="remember")
        msg = f"project '{reg[cwd]}' ↔ {cwd}  (store {store})"
        if a.write:
            targets = [f for f in ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".github/copilot-instructions.md") if os.path.exists(f)] or ["AGENTS.md"]
            for t in targets:
                body = open(t, encoding="utf-8").read() if os.path.exists(t) else ""
                if "remember.me" not in body:
                    with open(t, "a", encoding="utf-8") as f:
                        f.write(("\n" if body else "") + snip)
                    msg += f"\nadded the memory snippet to {t}"
        else:
            msg += "\n\nAdd this to your agent's instructions file (AGENTS.md, CLAUDE.md, GEMINI.md, …), or rerun with --write:\n\n" + snip
        print(msg)
        return
    if cmd == "status":
        print(status(store, p))
        note = maybe_autobuild(store, p, "read")
        if note:
            print(note)
        return
    if cmd == "ingest":
        kind = a.args[0] if a.args else "claude-code"
        if kind != "claude-code":
            raise SystemExit("ingest supports: claude-code (others: `remember log`, `remember import`)")
        files = [a.current] if a.current else claude_code_sessions(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        if not files:
            raise SystemExit(f"no Claude Code sessions on disk for this folder ({claude_code_dir(os.getcwd())})")
        print(ingest_claude_code(store, p, files))
        return
    if cmd == "log":
        text = open(a.args[0], encoding="utf-8").read() if a.args else sys.stdin.read()
        msgs = parse_conversation(text)
        if not msgs:
            raise SystemExit("no messages found: use '## user' / '## assistant' headings")
        print(log_messages(store, p, a.session or f"recalled-{time.strftime('%Y%m%d-%H%M')}", msgs, a.source, a.note))
        return
    if cmd == "import":
        print(import_export(store, p, a.args[0], a.pick, a.list))
        return
    if cmd == "builder-prompt":
        print(open(os.path.join(HERE, "prompts", "build_map.md"), encoding="utf-8").read())
        return
    if cmd == "atlas":
        import viz
        out = a.out or os.path.join(store, "atlas.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(viz.page(viz.build_data([p] if not a.all else None, store)))
        print(out)
        if a.open:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(out).replace(os.sep, "/"))
        return
    if cmd == "viewer":
        import viewer
        sub = a.args[0] if a.args else "status"
        if sub == "start":
            print(f"memory map viewer: http://127.0.0.1:{viewer.start(store)}")
        elif sub == "stop":
            print(viewer.stop(store))
        elif sub == "restart":
            viewer.stop(store)
            print(f"memory map viewer: http://127.0.0.1:{viewer.start(store)}")
        elif sub == "status":
            print(viewer.status(store))
        elif sub == "autostart":
            print(viewer.install_service(store) if (a.args[1:] or ["on"])[0] != "off" else viewer.uninstall_service())
        else:
            raise SystemExit("remember viewer start | stop | restart | status | autostart [on|off]")
        return
    if cmd == "show":
        print(show_map(store, p, open_browser=a.open))
        return
    if cmd == "serve":
        import serve
        from http.server import ThreadingHTTPServer
        port = int(a.args[0]) if a.args else 8765
        srv = ThreadingHTTPServer(("127.0.0.1", port), serve.make_handler(store))
        url = f"http://127.0.0.1:{port}"
        print(f"remember.me atlas (live, all projects): {url}   Ctrl+C to stop")
        if a.open:
            import webbrowser
            webbrowser.open(url)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return

    if not enabled(store, p):
        raise SystemExit(f"no memory for project '{p}' yet. `remember status` shows how to start.")
    if cmd == "build":
        if a.background:
            print(maybe_autobuild(store, p, "PreCompact") or "nothing to map, or no builder available (see `remember status`)")
        else:
            run_build(store, p)
        return
    if cmd in ("ops", "op"):
        if cmd == "ops":
            ops = json.load(sys.stdin) if a.args[0] == "-" else json.load(open(a.args[0], encoding="utf-8"))
        else:
            ops = [json.loads(" ".join(a.args))]
        for t, ev in memmap.apply_ops(store, p, ops, a.by):
            print(f"ok: {t} #{ev['seq']} {ev['op']} at {ev['at']}")
        memmap.save_cache(store, p)
        return
    if cmd in ("chunks", "chunk"):
        refresh_sources(store, p)
        chunks = memmap.chunk_list(store, p, a.size)
        if a.new:
            g = load(store, p)
            todo = {(r[1], r[2]) for r in unmapped_msgs(g, memmap.timeline(store, p))}
            idx = [i for i, ch in enumerate(chunks) if any((r[1], r[2]) in todo for r in ch)]
        else:
            idx = list(range(len(chunks)))
        if cmd == "chunks":
            for i in idx:
                ch = chunks[i]
                print(f"chunk {i}: {ch[0][0]} → {ch[-1][0]}  ({len(ch)} msgs: {ch[0][1]}#{ch[0][2]} … {ch[-1][1]}#{ch[-1][2]})")
            if not idx:
                print("nothing new to map")
        else:
            print(memmap.show_chunk(store, p, int(a.args[0]), a.size))
        return

    if cmd == "overview":
        maybe_autobuild(store, p, "read")  # assistants without hooks still get an up-to-date map
    g = load(store, p)
    if cmd == "overview":
        print(memmap.render(g, budget=a.budget, ids=a.ids) if g.get("nodes") else
              "no map yet: " + status(store, p))
    elif cmd == "zoom":
        print(memmap.zoom(g, a.args, a.q, a.recent))
    elif cmd == "expand":
        print("\n\n".join(memmap.expand(g, x) for x in a.args))
    elif cmd == "spans":
        print("\n\n".join(memmap.read_span(store, g, s) for s in a.args))
    elif cmd == "grep":
        print(memmap.grep(store, g, " ".join(a.args), a.max, a.start, a.end))
    elif cmd == "window":
        print(memmap.window(store, g, a.args[0], a.args[1] if len(a.args) > 1 else a.args[0]))
    elif cmd in ("find", "recall"):
        print(memmap.recall(g, " ".join(a.args)))
    elif cmd == "coverage":
        print(memmap.coverage(store, g))
    elif cmd == "validate":
        print(memmap.validate(store, g))
    else:
        raise SystemExit(f"unknown command {cmd!r}; see `remember --help`")


if __name__ == "__main__":
    main()
