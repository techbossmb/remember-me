"""Tests for the online service against a real Postgres (REMEMBER_DATABASE_URL), with development tokens.
Run: REMEMBER_DATABASE_URL=... python3 online/test_online.py"""
import os
import sys
import uuid

run = uuid.uuid4().hex[:8]
os.environ["REMEMBER_DEV_TOKENS"] = f"tok-a=alice-{run},tok-b=bob-{run}"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

c = TestClient(main.app)
A = {"Authorization": "Bearer tok-a"}
B = {"Authorization": "Bearer tok-b"}
n = 0


def rpc(h, method, params=None):
    global n
    n += 1
    r = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": n, "method": method, "params": params or {}})
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def tool(h, name, **args):
    res = rpc(h, "tools/call", {"name": name, "arguments": args})["result"]
    return ("ERROR " if res["isError"] else "") + res["content"][0]["text"]


def check(label, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + label + ("" if cond else f"\n     {detail[:400]}"))
    if not cond:
        check.failed = True


check.failed = False

# no token / bad token: 401 that points at the sign-in metadata
r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
check("no token -> 401", r.status_code == 401)
check("401 names resource metadata", "resource_metadata=" in r.headers.get("www-authenticate", ""), str(r.headers))
check("bad token -> 401", c.post("/mcp", headers={"Authorization": "Bearer nope"}, json={}).status_code == 401)
m = c.get("/.well-known/oauth-protected-resource").json()
check("protected resource metadata", m["resource"].endswith("/mcp"), str(m))
check("notification -> 202", c.post("/mcp", headers=A, json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202)

init = rpc(A, "initialize", {"protocolVersion": "2025-06-18"})["result"]
check("initialize", init["serverInfo"]["name"] == "remember-me" and "online" in init["instructions"], str(init)[:300])
names = {t["name"] for t in rpc(A, "tools/list")["result"]["tools"]}
check("local-only tools hidden", not names & {"remember_viewer", "remember_show_map", "remember_import", "remember_ingest_sessions"}, str(names))
check("core tools listed", {"remember_overview", "remember_log", "remember_zoom", "remember_apply_ops"} <= names, str(names))

t = tool(A, "remember_status")
check("no project -> asks which", "Which project?" in t, t)
t = tool(A, "remember_log", project="garden", session="planning",
         messages=[{"role": "user", "ts": "2026-10-01T09:00:00Z", "text": "Plant tomatoes in the south bed after the last frost, around mid April this year."},
                   {"role": "assistant", "ts": "2026-10-01T09:01:00Z", "text": "Noted: tomatoes go in the south bed in mid April, once frost is past."}])
check("log", "+2 messages" in t, t)
t = tool(A, "remember_log", project="garden", session="planning",
         messages=[{"role": "user", "ts": "2026-10-01T09:00:00Z", "text": "Plant tomatoes in the south bed after the last frost, around mid April this year."}])
check("log dedup", "already saved" in t, t)
t = tool(A, "remember_apply_ops", project="garden", ops=[
    {"op": "node.create", "id": "T1", "type": "thread", "title": "Tomatoes", "status": "active", "summary": "South bed, mid April", "src": "garden/planning.md#m0001-m0002"},
    {"op": "claim.add", "node": "T1", "kind": "decision", "text": "Tomatoes in the south bed in mid April", "src": "garden/planning.md#m0001"}])
check("apply ops", "ok: garden #2" in t, t)
t = tool(A, "remember_overview", project="garden")
check("overview", "[T1] (active) Tomatoes" in t, t)
t = tool(A, "remember_zoom", project="garden", ids=["T1"], question="when do tomatoes go in")
check("zoom with evidence", "mid April" in t and "evidence" in t, t)
t = tool(A, "remember_grep", project="garden", pattern="frost")
check("grep", "garden/planning.md#m0001" in t, t)
t = tool(A, "remember_coverage", project="garden")
check("coverage", "100%" in t, t)
t = tool(A, "remember_projects")
check("projects (alice)", "garden" in t, t)

# isolation: bob sees nothing of alice's
t = tool(B, "remember_projects")
check("bob has no projects", "no projects" in t, t)
t = tool(B, "remember_overview", project="garden")
check("bob can't read alice's garden", "Tomatoes" not in t and "mid April" not in t, t)
t = tool(B, "remember_grep", project="garden", pattern="frost")
check("bob can't grep alice's log", "planning.md" not in t, t)
t = tool(A, "remember_viewer", action="status")
check("local tool refused online", t.startswith("ERROR"), t)

# export: same layout as ~/.remember
r = c.get("/account/export", headers=A)
import io, zipfile  # noqa: E402,E401
names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
check("export zip", "log/garden/planning.md" in names and "maps/garden/events.jsonl" in names, str(names))
check("export needs sign-in", c.get("/account/export").status_code == 401)
check("db health", c.get("/healthz/db").json()["ok"])

# clean up
check("delete all (alice)", c.delete("/account/data", headers=A).json()["ok"])
check("alice now empty", "no projects" in tool(A, "remember_projects"))
c.delete("/account/data", headers=B)
sys.exit(1 if check.failed else 0)
