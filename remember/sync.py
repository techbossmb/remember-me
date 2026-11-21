"""Online/offline switching for the local remember.me server. Stdlib only.

The local server (plugin / `remember mcp`) has a mode, kept in <store>/config.json:
  offline  memory is the files in the store folder (~/.remember); the default
  online   every remember_* tool call is passed through to the user's remember.me account, so this computer, the
           phone and the web all see the same memory

Switching moves projects, so a project lives in one place at a time and there is never anything to merge:
  go offline  download projects into the store folder; online each becomes "held" by this computer: read-only there
              (keep_online=True), or removed from the cloud (keep_online=False)
  go online   upload what this computer holds (replacing the online copy) and release the hold; projects that only
              exist here are uploaded too; a project that exists in both places without a hold is a conflict, and
              nothing moves until the user says which copy to keep. Afterwards this computer's copies are set aside
              under backups/, since the account now has them

The computer signs in once with a device code (connect -> the user allows it in the browser -> finish).
"""
import getpass
import io
import json
import os
import platform
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from store import as_store

DEFAULT_SERVER = "https://remember-me.fastapicloud.dev"


# ---------------------------------------------------------------- config

def _cfg_path(store):
    return os.path.join(as_store(store).path, "config.json")


def config(store):
    if not as_store(store).path:  # memory in a database (the online service itself): no computer settings
        return {}
    try:
        with open(_cfg_path(store), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(store, cfg):
    p = _cfg_path(store)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)
    try:
        os.chmod(p, 0o600)  # it holds this computer's sign-in token
    except OSError:
        pass


def mode(store):
    return config(store).get("mode", "offline")


def server(store):
    return (os.environ.get("REMEMBER_SERVER") or config(store).get("server") or DEFAULT_SERVER).rstrip("/")


# ---------------------------------------------------------------- http

class SyncError(Exception):
    pass


def _http(store, method, path, body=None, raw=None, auth=True, ctype="application/json"):
    cfg = config(store)
    headers = {"Accept": "application/json"}
    if auth:
        if not cfg.get("token"):
            raise SyncError("this computer isn't connected to a remember.me account yet (remember_mode connect)")
        headers["Authorization"] = "Bearer " + cfg["token"]
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(server(store) + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = r.read()
            return out if "json" not in r.headers.get("Content-Type", "") else json.loads(out)
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(msg).get("error") or msg
        except ValueError:
            pass
        if e.code == 401 and auth:
            raise SyncError("remember.me no longer accepts this computer's sign-in; connect it again "
                            "(remember_mode connect)") from None
        raise SyncError(f"remember.me said {e.code}: {msg}") from None
    except urllib.error.URLError as e:
        raise SyncError(f"can't reach remember.me ({e.reason}); check the internet connection") from None


# ---------------------------------------------------------------- connect this computer

def connect(store):
    cfg = config(store)
    name = f"{getpass.getuser()}'s {platform.system() or 'computer'}".replace("Darwin", "Mac")
    r = _http(store, "POST", "/device/start", {"name": name}, auth=False)
    cfg.update(server=server(store), pending=r["device_code"], device=name)
    save_config(store, cfg)
    return (f"To connect this computer to the user's remember.me account, ask them to open this link and press "
            f"Allow (it asks them to sign in first if needed):\n{r['verification_uri_complete']}\n"
            f"The page shows the code {r['user_code']}; it should match. Then call remember_mode with action "
            f"connect_finish.")


def connect_finish(store, wait=45):
    cfg = config(store)
    if not cfg.get("pending"):
        return "connected already" if cfg.get("token") else "start with remember_mode connect"
    deadline = time.time() + wait
    while True:
        try:
            r = _http(store, "POST", "/device/poll", {"device_code": cfg["pending"]}, auth=False)
        except SyncError:
            cfg.pop("pending", None)
            save_config(store, cfg)
            return "that sign-in link expired; start again with remember_mode connect"
        if r.get("status") == "approved":
            cfg["token"] = r["token"]
            cfg.pop("pending", None)
            save_config(store, cfg)
            return (f"connected: {cfg['device']} can now use the user's remember.me account. Memory here is still "
                    f"{mode(store)}; remember_mode go_online moves it to the account.")
        if time.time() > deadline:
            return "not allowed yet: ask the user to open the link and press Allow, then call connect_finish again"
        time.sleep(3)


# ---------------------------------------------------------------- status

def _local_projects(store):
    import cli
    return cli.list_projects(store)


def status(store):
    cfg = config(store)
    lines = [f"mode: {mode(store)}  (memory is " + ("in the user's remember.me account)" if mode(store) == "online"
                                                   else f"in {as_store(store).path} on this computer)")]
    if not cfg.get("token"):
        lines.append("this computer isn't connected to a remember.me account (remember_mode connect)")
        return "\n".join(lines)
    lines.append(f"connected as {cfg.get('device')} to {server(store)}")
    try:
        st = _http(store, "GET", "/api/sync/state")
    except SyncError as e:
        lines.append(str(e))
        return "\n".join(lines)
    local = set(_local_projects(store))
    for p in sorted(set(st["projects"]) | local):
        o = st["projects"].get(p)
        where = []
        if o and o.get("files"):
            where.append("online")
        if p in local:
            where.append("on this computer")
        h = (o or {}).get("hold")
        if h:
            where.append(("held offline here" if h["this_computer"] else f"held offline on {h['device']}") +
                         ("" if h["keep_online"] else ", removed from the cloud"))
        lines.append(f"- {p}: " + ", ".join(where or ["empty"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- moving projects

def _project_files(store, p):
    st = as_store(store)
    out = {}
    for sub in ("log", "maps"):
        base = os.path.join(st.path, sub, p)
        for dp, _, fns in os.walk(base):
            for n in fns:
                if n in (".lock",) or n.startswith(".building") or n == ".nudged":
                    continue
                full = os.path.join(dp, n)
                rel = os.path.relpath(full, st.path).replace(os.sep, "/")
                with open(full, encoding="utf-8") as f:
                    out[rel] = f.read()
    return out


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, t in files.items():
            z.writestr(n, t)
    return buf.getvalue()


def _backup_local(store, p):
    st = as_store(store)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for sub in ("log", "maps"):
        src = os.path.join(st.path, sub, p)
        if os.path.isdir(src):
            dst = os.path.join(st.path, "backups", stamp, sub, p)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            moved.append(dst)
    return os.path.join(st.path, "backups", stamp) if moved else None


def go_offline(store, keep_online=True, projects=None):
    """Download projects from the account and hold them here; then this computer works offline."""
    import memmap
    st = _http(store, "GET", "/api/sync/state")
    names = projects or sorted(st["projects"])
    report = []
    for p in names:
        o = st["projects"].get(p)
        if not o:
            report.append(f"- {p}: not in the account")
            continue
        h = o.get("hold")
        if h and not h["this_computer"]:
            report.append(f"- {p}: skipped, it's offline on {h['device']}")
            continue
        if h and h["this_computer"] and not o.get("files"):
            report.append(f"- {p}: already here")
            continue
        data = _http(store, "GET", "/api/sync/pull?" + urllib.parse.urlencode({"project": p}))
        z = zipfile.ZipFile(io.BytesIO(data))
        files = {n: z.read(n).decode("utf-8") for n in z.namelist()}
        local = _project_files(store, p)
        backup = _backup_local(store, p) if local and local != files else None
        root = as_store(store).path
        for n, t in files.items():
            full = os.path.join(root, *n.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(t)
        if _project_files(store, p) != files:
            raise SyncError(f"{p}: the download didn't match; nothing was changed online")
        _http(store, "POST", "/api/sync/hold", {"project": p, "keep_online": keep_online})
        report.append(f"- {p}: {len(files)} files here; " +
                      ("read-only online until you go back online" if keep_online else "removed from the cloud") +
                      (f" (the older copy that was here is in {backup})" if backup else ""))
    memmap._LOG_CACHE.clear()
    cfg = config(store)
    cfg["mode"] = "offline"
    save_config(store, cfg)
    return "offline: memory is now on this computer.\n" + "\n".join(report) + \
        "\nThe map viewer for offline memory is local: remember_show_map gives the link."


def _online_files(store, p):
    data = _http(store, "GET", "/api/sync/pull?" + urllib.parse.urlencode({"project": p}))
    z = zipfile.ZipFile(io.BytesIO(data))
    return {n: z.read(n).decode("utf-8") for n in z.namelist()}


def go_online(store, replace=()):
    """Upload what this computer holds (and projects that only exist here); then this computer works online.
    Nothing is uploaded while any project is in conflict (different copies here and online, no hold)."""
    st = _http(store, "GET", "/api/sync/state")
    local = _local_projects(store)
    plan, report, conflicts = [], [], []
    for p in local:
        o = st["projects"].get(p) or {}
        h = o.get("hold")
        if h and not h["this_computer"]:
            report.append(f"- {p}: left here, it's offline on {h['device']} too; sort that out on that computer first")
            continue
        files = _project_files(store, p)
        if o.get("files") and not h:
            if _online_files(store, p) == files:
                report.append(f"- {p}: already the same online")
                plan.append((p, None, False))
                continue
            if p not in replace:
                conflicts.append(p)
                continue
        plan.append((p, files, bool(o.get("files")) and not h))
    if conflicts:
        return ("not switched yet, nothing uploaded: these projects exist both here and online, as different copies: "
                + ", ".join(conflicts) + ". Ask the user which to keep. To make this computer's copy the online one, "
                "call go_online with replace=[those names]; to keep the online one, leave this computer's copy "
                "(it stays here unused) or remove it.")
    for p, files, replacing in plan:
        if files is None:
            continue
        r = _http(store, "POST", "/api/sync/push?" + urllib.parse.urlencode({"project": p, "release": "true"}),
                  raw=_zip(files), ctype="application/zip")
        report.append(f"- {p}: {r['files']} files uploaded" + (", replacing the online copy" if replacing else ""))
    # once a project is online, this computer's copy would only go stale: set it aside (kept under backups/), so the
    # next go_offline starts from the online copy and never mistakes an old copy for a conflict
    for p in local:
        o = st["projects"].get(p) or {}
        h = o.get("hold")
        if not (h and not h["this_computer"]):
            _backup_local(store, p)
    for p, o in st["projects"].items():  # held here but no longer on this computer: just release
        h = o.get("hold")
        if h and h["this_computer"] and p not in local:
            _http(store, "POST", "/api/sync/release", {"project": p})
            report.append(f"- {p}: released (no copy here)")
    cfg = config(store)
    cfg["mode"] = "online"
    save_config(store, cfg)
    return "online: memory now lives in the user's remember.me account, the same on every device.\n" + "\n".join(report)


# ---------------------------------------------------------------- pass-through in online mode

def forward(store, msg):
    """Send one MCP request to the account and return its response."""
    try:
        return _http(store, "POST", "/mcp", msg)
    except SyncError as e:
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {
            "content": [{"type": "text", "text": f"error: {e}"}], "isError": True}}
