"""Golden-output regression test: runs every read/write path of the CLI and MCP server against a fixed
sample store and compares the masked output with tests/golden.txt.

  python3 tests/golden.py            compare (exit 1 on any difference)
  python3 tests/golden.py --record   rewrite tests/golden.txt
  REMEMBER_TEST_BACKEND=pg ...       run against the Postgres backend (needs REMEMBER_DATABASE_URL)
"""
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "..", "remember", "cli.py")
GOLDEN = os.path.join(HERE, "golden.txt")

CONV_A = """## user · 2026-09-01T10:00:00Z
We need a memory system for agents. Keep the raw conversation verbatim and build a map over it, with every claim pointing back to the messages.
## assistant · 2026-09-01T10:01:00Z
Agreed. I'd store the log as plain markdown per session and the map as an append-only event list replayed in time order, so nothing is ever lost.
## user · 2026-09-01T10:05:00Z
Decision: the viewer runs on localhost port 8765 and never auto-opens the browser; just give the user a link.
## assistant · 2026-09-01T10:06:00Z
Noted. The viewer gives a link only. Open question: should the viewer start on login?
"""
CONV_B = [
    {"role": "user", "ts": "2026-09-03T09:00:00Z", "text": "Change of plan on the viewer: start it automatically when a session starts, and offer to start it at login too."},
    {"role": "assistant", "ts": "2026-09-03T09:01:00Z", "text": "Done: the viewer now starts at session start, and there is an autostart option for login. That answers the open question."},
    {"role": "user", "ts": "2026-09-03T09:10:00Z", "text": "Rule for every project: never ask the user to run terminal commands; the assistant does everything through the tools."},
]
OPS = [
    {"op": "node.create", "id": "T1", "type": "thread", "title": "Memory design", "status": "active",
     "summary": "Verbatim log plus an event-sourced map", "src": "demo/design.md#m0001-m0002"},
    {"op": "claim.add", "node": "T1", "kind": "decision", "text": "Log as markdown per session; map as replayed events",
     "spans": ["demo/design.md#m0002"], "src": "demo/design.md#m0002"},
    {"op": "node.create", "id": "T2", "type": "thread", "title": "Map viewer", "status": "active",
     "summary": "Local viewer that gives a link", "src": "demo/design.md#m0003-m0004"},
    {"op": "claim.add", "node": "T2", "kind": "decision", "cid": "T2.d1", "text": "Viewer gives a link and never auto-opens",
     "spans": ["demo/design.md#m0003"], "src": "demo/design.md#m0003"},
    {"op": "claim.add", "node": "T2", "kind": "open", "cid": "T2.o1", "text": "Should the viewer start on login?",
     "spans": ["demo/design.md#m0004"], "src": "demo/design.md#m0004"},
    {"op": "claim.supersede", "id": "T2.o1", "reason": "resolved: autostart option added",
     "new": {"kind": "decision", "text": "Viewer starts with each session; optional start at login",
             "spans": ["demo/followup.md#m0001-m0002"]}, "src": "demo/followup.md#m0001"},
    {"op": "edge.add", "from": "T2", "to": "T1", "type": "depends_on", "src": "demo/followup.md#m0001"},
    {"op": "rule.add", "id": "R1", "scope": "user", "text": "Never ask the user to run terminal commands",
     "spans": ["demo/followup.md#m0003"], "src": "demo/followup.md#m0003"},
]


def mask(s, store):
    s = s.replace(store, "<STORE>")
    s = re.sub(r"\b\d{14}\b", "<STAMP>", s)
    s = re.sub(r'"logged_at": "[^"]+"', '"logged_at": "<T>"', s)
    s = re.sub(r"\b20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ\b(?!\S*#)", lambda m: m.group(0) if m.group(0).startswith("2026-09") else "<NOW>", s)
    s = re.sub(r"\b(20\d\d-\d\d-\d\d[T ]\d\d:\d\d)\b", lambda m: m.group(1) if m.group(1).startswith("2026-09") else "<NOW>", s)
    return s


def run(env, *args, stdin=None):
    r = subprocess.run([sys.executable, CLI, *args], input=stdin, capture_output=True, text=True, env=env, cwd="/")
    return (r.stdout + r.stderr).rstrip() + f"\n[exit {r.returncode}]"


def mcp(env, calls):
    lines = [json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})]
    for i, (name, args) in enumerate(calls, 1):
        lines.append(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}))
    r = subprocess.run([sys.executable, CLI, "mcp"], input="\n".join(lines) + "\n", capture_output=True, text=True, env=env, cwd="/")
    out = []
    for line in r.stdout.splitlines()[1:]:
        res = json.loads(line).get("result", {})
        out.append(("ERROR " if res.get("isError") else "") + res.get("content", [{}])[0].get("text", ""))
    return out


def scenario():
    store = tempfile.mkdtemp(prefix="remember-golden-")
    env = dict(os.environ, REMEMBER_HOME=store, REMEMBER_PROJECT="", REMEMBER_AUTOBUILD="off", REMEMBER_VIEWER="0")
    env.pop("REMEMBER_PROJECT")
    out = []

    def step(title, text):
        out.append(f"=== {title}\n{mask(text, store)}")

    P = ["--project", "demo"]
    step("status (empty)", run(env, "status", *P))
    step("no project from /", run(env, "status"))
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(CONV_A)
    step("log A (cli)", run(env, "log", f.name, *P, "--session", "design"))
    step("log A again (dedup)", run(env, "log", f.name, *P, "--session", "design"))
    r = mcp(env, [("remember_log", {"project": "demo", "session": "followup", "source": "recalled", "messages": CONV_B}),
                  ("remember_log", {"project": "demo", "session": "followup", "messages": CONV_B[:1]}),
                  ("remember_log", {"messages": CONV_B[:1]}),
                  ("remember_projects", {}),
                  ("remember_status", {"project": "demo"})])
    for i, t in enumerate(r):
        step(f"mcp {i}", t)
    step("chunks", run(env, "chunks", *P))
    step("chunks --new", run(env, "chunks", "--new", *P))
    step("chunk 1", run(env, "chunk", "1", *P))
    ops = [dict(o) for o in OPS]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(ops, f)
    step("ops", run(env, "ops", f.name, *P))
    for cmd in (["status"], ["overview"], ["overview", "--ids"], ["zoom", "T2", "--q", "viewer login"], ["zoom", "--recent", "2"],
                ["expand", "T2"], ["spans", "demo/design.md#m0001-m0002"], ["grep", "viewer"], ["grep", "viewer", "--from", "2026-09-02"],
                ["window", "2026-09-01", "2026-09-02"], ["find", "viewer link"], ["coverage"], ["validate"], ["chunks", "--new"]):
        step(" ".join(cmd), run(env, *cmd, *P))
    r = mcp(env, [("remember_overview", {"project": "demo"}), ("remember_zoom", {"project": "demo", "ids": ["T1"]}),
                  ("remember_apply_ops", {"project": "demo", "ops": [{"op": "node.update", "id": "T1", "status": "done", "src": "demo/followup.md#m0003"}]}),
                  ("remember_overview", {"project": "demo"}), ("remember_coverage", {"project": "demo"}),
                  ("remember_validate", {"project": "demo"}), ("remember_spans", {"project": "demo", "spans": ["demo/followup.md#m0003"]})])
    for i, t in enumerate(r):
        step(f"mcp b{i}", t)
    # what the store holds (files backend layout = the export format)
    if os.environ.get("REMEMBER_TEST_BACKEND", "files") == "files":
        tree = []
        for dp, dn, fn in sorted(os.walk(store)):
            dn.sort()
            for n in sorted(fn):
                p = os.path.join(dp, n)
                rel = os.path.relpath(p, store)
                if n in (".lock",) or n.endswith(".state.json"):
                    continue
                tree.append(f"--- {rel}\n" + open(p, encoding="utf-8").read())
        step("store files", "\n".join(tree))
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    got = scenario()
    if "--record" in sys.argv:
        open(GOLDEN, "w", encoding="utf-8").write(got)
        print(f"recorded {GOLDEN}")
        sys.exit(0)
    want = open(GOLDEN, encoding="utf-8").read()
    if got == want:
        print("golden: OK")
        sys.exit(0)
    sys.stdout.writelines(difflib.unified_diff(want.splitlines(True), got.splitlines(True), "golden", "now"))
    sys.exit(1)
