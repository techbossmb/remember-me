"""Tests for the online service against a real Postgres (REMEMBER_DATABASE_URL), with development tokens.
Run: REMEMBER_DATABASE_URL=... python3 online/test_online.py"""
import os
import sys
import uuid

run = uuid.uuid4().hex[:8]
os.environ["REMEMBER_SESSION_SECRET"] = "test-secret"
os.environ["WORKOS_CLIENT_ID"] = "client_test"
os.environ["REMEMBER_PUBLIC_URL"] = "http://testserver"
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
check("local-only tools hidden", not names & {"remember_viewer", "remember_import", "remember_ingest_sessions"}, str(names))
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
t = tool(A, "remember_show_map", project="garden")
check("show_map gives the live map address", t.startswith("http://testserver/map"), t)
r = c.get("/map", follow_redirects=False)
check("map without sign-in -> /login", r.status_code == 303 and r.headers["location"] == "/login", str(r.status_code))
r = c.get("/login", follow_redirects=False)
check("login -> WorkOS AuthKit", "api.workos.com/user_management/authorize" in r.headers.get("location", "") and "rm_state" in r.headers.get("set-cookie", ""), str(r.headers))
check("callback without state refused", c.get("/auth/callback?code=x&state=y").status_code == 400)
check("api/data needs sign-in", c.get("/api/data").status_code == 401)
alice_cookie = {"rm_session": main._sign({"u": f"alice-{run}", "exp": 9999999999})}
bob_cookie = {"rm_session": main._sign({"u": f"bob-{run}", "exp": 9999999999})}
c.cookies.clear(); c.cookies.update(alice_cookie)
check("signed-in map page", c.get("/map").status_code == 200)
d = c.get("/api/data").json()
check("api/data has alice's garden", "garden" in d["projects"] and any(n["title"] == "Tomatoes" for n in d["projects"]["garden"]["graph"]["nodes"]), str(d)[:200])
r = c.post("/api/op", json={"project": "garden", "op": {"op": "node.update", "id": "T1", "title": "Tomato plan"}})
check("viewer edit applies", r.status_code == 200 and r.json()["ok"], r.text)
check("edit shows in overview", "Tomato plan" in tool(A, "remember_overview", project="garden"))
check("viewer can't add claims", c.post("/api/op", json={"project": "garden", "op": {"op": "claim.add", "node": "T1", "kind": "decision", "text": "x"}}).status_code == 400)
check("other-site edit refused", c.post("/api/op", headers={"origin": "https://evil.example"}, json={"project": "garden", "op": {"op": "node.update", "id": "T1", "title": "x"}}).status_code == 403)
forged = main._sign({"u": f"alice-{run}", "exp": 9999999999})[:-4] + "AAAA"
c.cookies.clear(); c.cookies.update({"rm_session": forged})
check("forged session refused", c.get("/api/data").status_code == 401)
c.cookies.clear(); c.cookies.update(bob_cookie)
check("bob's map page doesn't show alice's garden", "garden" not in c.get("/api/data").json()["projects"])
c.cookies.clear()
names2 = {t["name"] for t in rpc(A, "tools/list")["result"]["tools"]}
check("show_map listed online", "remember_show_map" in names2)
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
