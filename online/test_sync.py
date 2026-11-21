"""End to end: a computer's local remember-me server switching between offline and online against a real running
service (uvicorn) and Postgres. Run: REMEMBER_DATABASE_URL=postgresql://<non-superuser>@... python3 online/test_sync.py"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "remember"))
run = uuid.uuid4().hex[:8]
USER = f"syncuser-{run}"
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
URL = f"http://127.0.0.1:{PORT}"
env = dict(os.environ, REMEMBER_SESSION_SECRET="sync-test", REMEMBER_PUBLIC_URL=URL,
           REMEMBER_DEV_TOKENS=f"phone-{run}={USER}", WORKOS_CLIENT_ID="client_test")
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "online.main:app", "--port", str(PORT)], cwd=ROOT, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(50):
    try:
        urllib.request.urlopen(URL + "/healthz", timeout=1)
        break
    except Exception:
        time.sleep(0.2)
os.environ.update(env)
sys.path.insert(0, HERE)
import main as online  # noqa: E402  (only to sign a browser session for the test)

STORE = tempfile.mkdtemp(prefix="rm-mac-")
os.environ["REMEMBER_HOME"] = STORE
os.environ["REMEMBER_SERVER"] = URL
os.environ.pop("REMEMBER_DATABASE_URL")  # the "Mac" uses files, not the database
import cli  # noqa: E402
import mcp_server  # noqa: E402
import memmap  # noqa: E402
memmap.TOOL_PREFIX = "remember_"
mac = cli.as_store(STORE)
n = 0
ok = True


def check(label, cond, detail=""):
    global ok
    print(("ok   " if cond else "FAIL ") + label + ("" if cond else f"\n     {str(detail)[:500]}"))
    ok &= bool(cond)


def mac_tool(name, **args):  # what the assistant on the Mac calls (local server, honours the mode)
    global n
    n += 1
    out = mcp_server._local_or_forward(mac, {"jsonrpc": "2.0", "id": n, "method": "tools/call",
                                             "params": {"name": name, "arguments": args}})
    res = out["result"]
    return ("ERROR " if res["isError"] else "") + res["content"][0]["text"]


def phone_tool(name, **args):  # the connector on the phone: straight to the service
    global n
    n += 1
    req = urllib.request.Request(URL + "/mcp", method="POST", headers={"Authorization": f"Bearer phone-{run}",
                                                                      "Content-Type": "application/json"},
                                 data=json.dumps({"jsonrpc": "2.0", "id": n, "method": "tools/call",
                                                  "params": {"name": name, "arguments": args}}).encode())
    res = json.load(urllib.request.urlopen(req))["result"]
    return ("ERROR " if res["isError"] else "") + res["content"][0]["text"]


def msgs(*texts, day="2026-11-01"):
    return [{"role": "user" if i % 2 == 0 else "assistant", "ts": f"{day}T10:{i:02d}:00Z", "text": t}
            for i, t in enumerate(texts)]


OPS = lambda p, s: [{"op": "node.create", "id": "T1", "type": "thread", "title": "Topic", "status": "active",  # noqa: E731
                     "summary": "x", "src": f"{p}/{s}.md#m0001"}]
try:
    # the phone has an online project; the Mac has two local ones, one with the same name as an online one
    check("phone saves online", "+2" in phone_tool("remember_log", project="notes", session="phone-chat",
                                                   messages=msgs("Remember the dentist is on Tuesday afternoon", "Noted, Tuesday afternoon.")))
    phone_tool("remember_apply_ops", project="notes", ops=OPS("notes", "phone-chat"))
    phone_tool("remember_log", project="shared", session="web", messages=msgs("online copy of shared work, started on the web"))
    cli.log_messages(mac, "garden-mac", "mac-chat", [("user", "2026-11-02T09:00:00Z", "Tomatoes go in the south bed in April")], "recalled")
    cli.log_messages(mac, "shared", "mac-chat", [("user", "2026-11-02T09:00:00Z", "the Mac's copy of shared work")], "recalled")

    t = mac_tool("remember_mode", action="status")
    check("starts offline, not connected", "mode: offline" in t and "isn't connected" in t, t)
    t = mac_tool("remember_mode", action="go_online")
    check("go online before connecting explains", t.startswith("ERROR") and "connect" in t, t)

    # device sign-in: the assistant gets a link + code, the user allows it in the browser
    t = mac_tool("remember_mode", action="connect")
    code = t.split("code=")[1].split()[0]
    check("connect gives link and code", f"{URL}/device?code=" in t and code in t, t)
    cookie = "rm_session=" + online._sign({"u": USER, "exp": 9999999999})
    page = urllib.request.urlopen(urllib.request.Request(f"{URL}/device?code={code}", headers={"Cookie": cookie})).read().decode()
    check("approval page names the computer", "Allow" in page and "remember-me memory" in page, page[:300])
    done = urllib.request.urlopen(urllib.request.Request(f"{URL}/device/approve", method="POST", data=f"code={code}".encode(),
                                                         headers={"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})).read().decode()
    check("approved", "is connected" in done, done[:300])
    t = mac_tool("remember_mode", action="connect_finish")
    check("connect_finish stores the token", t.startswith("connected"), t)

    # going online: conflict on 'shared' is reported, nothing switches
    t = mac_tool("remember_mode", action="go_online")
    check("conflict reported, not switched", "not switched" in t and "shared" in t, t)
    check("still offline", "mode: offline" in mac_tool("remember_mode", action="status"))
    t = mac_tool("remember_mode", action="go_online", replace=["shared"])
    check("go online with replace", t.startswith("online") and "garden-mac" in t and "replacing" in t, t)
    t = phone_tool("remember_projects")
    check("phone sees the Mac's projects", all(p in t for p in ("notes", "garden-mac", "shared")), t)
    check("shared now has the Mac's copy", "Mac's copy" in phone_tool("remember_grep", project="shared", pattern="copy"))

    # online mode: the Mac's tools go to the account
    t = mac_tool("remember_log", project="notes", session="mac-chat", messages=msgs("Also pick up the dry cleaning", day="2026-11-03"))
    check("Mac saves online in online mode", "+1" in t, t)
    check("phone sees the Mac's new message", "dry cleaning" in phone_tool("remember_grep", project="notes", pattern="cleaning"))
    check("viewer points online", "/map" in mac_tool("remember_viewer", action="status"))
    export = os.path.join(STORE, "..", f"export-{run}.json")
    with open(export, "w") as f:
        json.dump([{"uuid": "c0ffee00-1111", "name": "Trip planning", "created_at": "2026-10-01T09:00:00Z",
                    "updated_at": "2026-10-01T09:05:00Z", "chat_messages": [
                        {"uuid": "m1", "sender": "human", "created_at": "2026-10-01T09:00:00Z", "content": [{"type": "text", "text": "Plan a weekend in Lisbon in May"}]},
                        {"uuid": "m2", "sender": "assistant", "created_at": "2026-10-01T09:01:00Z", "content": [{"type": "text", "text": "Day one: Alfama and the castle."}]}]}], f)
    check("import lists while online", "Trip planning" in mac_tool("remember_import", path=export))
    t = mac_tool("remember_import", path=export, picks=["all"], project="trips")
    check("import while online sends to the account", "imported into the account" in t and "2 messages" in t, t)
    check("phone sees the imported chat", "Lisbon" in phone_tool("remember_grep", project="trips", pattern="Lisbon"))

    # going offline, keeping a read-only copy online
    t = mac_tool("remember_mode", action="go_offline")
    check("go offline asks which kind first", t.startswith("ERROR") and "keep_online" in t, t)
    t = mac_tool("remember_mode", action="go_offline", keep_online=True)
    check("go offline (keep online copy)", t.startswith("offline") and "read-only online" in t, t)
    check("files are on the Mac", os.path.exists(os.path.join(STORE, "log", "notes", "phone-chat.md")))
    t = phone_tool("remember_log", project="notes", session="phone-chat", messages=msgs("phone tries to write", day="2026-11-04"))
    check("phone write refused while held", t.startswith("ERROR") and "offline on" in t, t)
    check("phone can still read", "dentist" in phone_tool("remember_grep", project="notes", pattern="dentist"))
    data = json.load(urllib.request.urlopen(urllib.request.Request(f"{URL}/api/data", headers={"Cookie": cookie})))
    check("online map marks it held", data["projects"]["notes"].get("held", {}).get("device"), str(data["projects"].get("notes", {}).keys()))
    t = mac_tool("remember_log", project="notes", session="mac-chat", messages=msgs("Offline: book the flight to Lagos", day="2026-11-05"))
    check("Mac saves locally while offline", not t.startswith("ERROR") and "Lagos" in open(os.path.join(STORE, "log", "notes", "mac-chat.md")).read(), t)
    check("not online yet", "Lagos" not in phone_tool("remember_grep", project="notes", pattern="Lagos"))

    # back online: offline changes go up, hold released
    t = mac_tool("remember_mode", action="go_online")
    check("back online", t.startswith("online"), t)
    check("Mac's copies set aside after going online", not os.path.isdir(os.path.join(STORE, "log", "notes")) and
          os.path.isdir(os.path.join(STORE, "backups")))
    check("offline message now online", "Lagos" in phone_tool("remember_grep", project="notes", pattern="Lagos"))
    check("phone can write again", "+1" in phone_tool("remember_log", project="notes", session="phone-chat",
                                                      messages=msgs("phone writes again after", day="2026-11-06")))
    check("map still there", "[T1]" in phone_tool("remember_overview", project="notes"))

    # going offline and removing from the cloud, then back
    t = mac_tool("remember_mode", action="go_offline", keep_online=False, projects=["garden-mac"])
    check("go offline (remove from cloud)", "removed from the cloud" in t, t)
    check("gone from the cloud", "Tomatoes" not in phone_tool("remember_grep", project="garden-mac", pattern="Tomatoes"))
    t = mac_tool("remember_mode", action="go_online")
    check("back online restores it", "Tomatoes" in phone_tool("remember_grep", project="garden-mac", pattern="Tomatoes"), t)
finally:
    srv.terminate()
    import store_pg
    c = store_pg.connect(env["REMEMBER_DATABASE_URL"])
    store_pg.PgStore(c, USER).delete_all()
    for tbl in ("rm_holds", "rm_tokens", "rm_device_codes"):
        c.execute(f"delete from {tbl} where user_id = %s", (USER,))
sys.exit(0 if ok else 1)
