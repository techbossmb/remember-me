"""remember-me online: the same remember_* tools as the local plugin, served over MCP's HTTP transport, with each
signed-in user's memory in Postgres (Neon). Deployed on FastAPI Cloud (`fastapi deploy` from the repo root).

How sign-in works: this service is an OAuth *resource server*. An auth provider (the "authorization server") signs
users in and issues access tokens; Claude and other MCP clients discover it through
/.well-known/oauth-protected-resource, send the user there to sign in, and then call /mcp with the token. We only
verify the token (JWT, against the provider's published keys) and use its subject as the user id. A user's first
call creates their (empty) memory: sign-up and sign-in are the same step.

Environment:
  REMEMBER_DATABASE_URL   Postgres connection string (Neon: the pooled URL, with sslmode=require)
  REMEMBER_PUBLIC_URL     this service's public base URL, e.g. https://remember-me.fastapicloud.dev
  AUTH_ISSUER             the auth provider's issuer URL (its OAuth/OIDC discovery lives under it)
  AUTH_JWKS_URL           optional: its signing keys (default: discovered from the issuer)
  AUTH_AUDIENCE           optional: the audience tokens must carry (usually this service's /mcp URL)
  REMEMBER_DEV_TOKENS     development only: "token=user,token2=user2" accepted as bearer tokens
"""
import base64
import hashlib
import hmac
import io
import json
import os
import sys
import threading
import time
import zipfile

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "remember"))
import memmap  # noqa: E402
import mcp_server  # noqa: E402
import store_pg  # noqa: E402
import viz  # noqa: E402

memmap.TOOL_PREFIX = "remember_"

from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def lifespan(app):
    yield
    if _pool is not None:
        _pool.close()


app = FastAPI(title="remember-me", docs_url=None, redoc_url=None, lifespan=lifespan)

DB_URL = os.environ.get("REMEMBER_DATABASE_URL", "")
PUBLIC_URL = os.environ.get("REMEMBER_PUBLIC_URL", "").rstrip("/")
ISSUER = os.environ.get("AUTH_ISSUER", "").rstrip("/")
AUDIENCE = os.environ.get("AUTH_AUDIENCE") or None
DEV_TOKENS = dict(x.split("=", 1) for x in os.environ.get("REMEMBER_DEV_TOKENS", "").split(",") if "=" in x)

# ---------------------------------------------------------------- database

_pool = None
_pool_lock = threading.Lock()


def pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool
            # autocommit like the CLI; no server-side prepared statements, so a transaction-mode pooler (Neon's) works
            p = ConnectionPool(DB_URL, min_size=0, max_size=8, open=True, timeout=30, reconnect_timeout=60,
                               check=ConnectionPool.check_connection,  # drop connections Neon closed while asleep
                               kwargs={"autocommit": True, "prepare_threshold": None})
            for attempt in range(3):  # the first connection can fail while Neon wakes up
                try:
                    with p.connection() as c:
                        store_pg.ensure_schema(c)
                        c.execute(ACCOUNT_SCHEMA)
                    break
                except Exception:
                    if attempt == 2:
                        p.close()
                        raise
                    time.sleep(1 + attempt)
            _pool = p
        return _pool


# Account-level tables (no conversation content): computers signed in with the device flow, and projects a computer
# has taken offline ("held"). Queries on them always filter by user id in this file.
ACCOUNT_SCHEMA = """
create table if not exists rm_device_codes (
    device_code text primary key, user_code text unique not null, name text not null,
    user_id text, token text, expires timestamptz not null, created timestamptz not null default now());
create table if not exists rm_tokens (
    token_hash text primary key, user_id text not null, device_id text not null, name text not null,
    created timestamptz not null default now(), last_used timestamptz);
create table if not exists rm_holds (
    user_id text not null, project text not null, device_id text not null, device_name text not null,
    keep_online boolean not null, since timestamptz not null default now(), primary key (user_id, project));
"""

# The tool code was written for one process per user (stdio): it prints into a captured buffer and keeps a per-process
# parse cache. Until that's reworked, tool calls run one at a time in each worker. Fine for a proof of concept.
_tool_lock = threading.Lock()

# ---------------------------------------------------------------- auth

_jwks = None


def _jwks_client():
    global _jwks
    if _jwks is None:
        import jwt
        url = os.environ.get("AUTH_JWKS_URL")
        if not url:
            import urllib.request
            for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
                try:
                    with urllib.request.urlopen(ISSUER + path, timeout=5) as r:
                        url = json.load(r).get("jwks_uri")
                    if url:
                        break
                except Exception:
                    continue
        if not url:
            raise RuntimeError("can't find the auth provider's signing keys: set AUTH_JWKS_URL")
        _jwks = jwt.PyJWKClient(url)
    return _jwks


def principal(token):
    """(user id, device) for a bearer token, or (None, None). device is {"id", "name"} for a signed-in computer
    (device token), else None (a connector's sign-in)."""
    if token and token.startswith("rmd_"):
        h = hashlib.sha256(token.encode()).hexdigest()
        with pool().connection() as c:
            row = c.execute("update rm_tokens set last_used = now() where token_hash = %s "
                            "returning user_id, device_id, name", (h,)).fetchone()
        return (row[0], {"id": row[1], "name": row[2]}) if row else (None, None)
    return user_for(token), None


def user_for(token):
    """The user id a bearer token belongs to, or None if it isn't valid."""
    if not token:
        return None
    if token.startswith("rmd_"):
        return principal(token)[0]
    if token in DEV_TOKENS:
        return DEV_TOKENS[token]
    if not ISSUER:
        return None
    import jwt
    try:
        key = _jwks_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256", "ES256", "EdDSA"], issuer=ISSUER, audience=AUDIENCE,
                            options={"verify_aud": AUDIENCE is not None})
    except Exception:
        return None
    # namespaced by provider, so switching providers later can't merge two people's memory
    return hashlib.sha256(ISSUER.encode()).hexdigest()[:8] + ":" + str(claims["sub"])


def _base(request):
    return PUBLIC_URL or str(request.base_url).rstrip("/")


def _unauthorized(request):
    meta = f"{_base(request)}/.well-known/oauth-protected-resource"
    return JSONResponse({"error": "unauthorized", "message": "sign in to remember-me"}, status_code=401,
                        headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})


def _token(request):
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else None

# ---------------------------------------------------------------- routes


@app.get("/")
def home():
    return HTMLResponse(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "home.html"),
                             encoding="utf-8").read())


@app.get("/llms.txt")
def llms_txt():
    return PlainTextResponse(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "llms.txt"),
                                  encoding="utf-8").read())


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/healthz/db")
def healthz_db():
    """How long a database round trip takes right now (the first one after Neon has been idle includes its wake-up)."""
    t = time.perf_counter()
    with pool().connection() as c:
        c.execute("select 1")
    return {"ok": True, "ms": round((time.perf_counter() - t) * 1000)}


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource(request: Request):
    base = _base(request)
    return {"resource": f"{base}/mcp", "authorization_servers": [ISSUER] if ISSUER else [],
            "bearer_methods_supported": ["header"], "resource_name": "remember-me"}


@app.post("/mcp")
async def mcp(request: Request):
    user = user_for(_token(request))
    if not user:
        return _unauthorized(request)
    try:
        msg = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
                            status_code=400)
    if isinstance(msg, list):
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "batches not supported"}},
                            status_code=400)
    if msg.get("id") is None:  # notification or response from the client
        return Response(status_code=202)
    import anyio
    out = await anyio.to_thread.run_sync(_handle, user, msg)
    return JSONResponse(out)


def _holds(conn, user):
    return {r[0]: {"device": r[1], "since": r[2].isoformat(), "keep_online": r[3]} for r in conn.execute(
        "select project, device_name, since, keep_online from rm_holds where user_id = %s", (user,))}


def _handle(user, msg):
    with _tool_lock, pool().connection() as conn:
        memmap._LOG_CACHE.clear()  # another worker may have written since
        held = {p: f"{p} is offline on {h['device']} (since {h['since'][:10]}); it's read-only here until that "
                   "computer brings it back online. Save to it there, or ask the user to go back online first."
                for p, h in _holds(conn, user).items()}
        return mcp_server.respond(store_pg.PgStore(conn, user), msg, online=True, show_map=_show_map,
                                  readonly=held)

# ---------------------------------------------------------------- map page
# A live, bookmarkable map at /map. The browser signs in with the same WorkOS account as the connector (AuthKit's
# hosted page: Google and the rest), then stays signed in with a signed cookie. The page is the same viewer as the
# local one: /api/data serves the signed-in user's maps, /api/op applies the viewer's few edits.

WORKOS_CLIENT_ID = os.environ.get("WORKOS_CLIENT_ID", "")
WORKOS_API_KEY = os.environ.get("WORKOS_API_KEY", "")
SESSION_SECRET = os.environ.get("REMEMBER_SESSION_SECRET", "").encode()
SESSION_DAYS = 30
COOKIE = "rm_session"
NOSTORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _sign(d):
    body = _b64(json.dumps(d).encode())
    return body + "." + _b64(hmac.new(SESSION_SECRET, body.encode(), hashlib.sha256).digest())


def _unsign(t):
    try:
        body, sig = t.split(".", 1)
        want = _b64(hmac.new(SESSION_SECRET, body.encode(), hashlib.sha256).digest())
        if not SESSION_SECRET or not hmac.compare_digest(sig, want):
            return None
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return d if d.get("exp", 0) > time.time() else None
    except Exception:
        return None


def _web_user(request):
    d = _unsign(request.cookies.get(COOKIE, ""))
    return d["u"] if d else None


def _user_id(sub):
    """Same user id as the connector's tokens (user_for), so the page shows the memory the assistant writes."""
    return hashlib.sha256(ISSUER.encode()).hexdigest()[:8] + ":" + str(sub)


def _map_url(project=None):
    return f"{PUBLIC_URL}/map"


def _show_map(project):
    return (f"{_map_url(project)}\nThe user's live memory map. It opens in any browser; the first time, they sign in "
            "with the same account they connected remember-me with, and then stay signed in.")


@app.get("/map")
def map_page(request: Request):
    if not _web_user(request):
        return Response(status_code=303, headers={"Location": "/login?next=/map", **NOSTORE})
    return HTMLResponse(viz.page(), headers=NOSTORE)


@app.get("/login")
def login(next: str = "/map"):
    if not (WORKOS_CLIENT_ID and SESSION_SECRET):
        return PlainTextResponse("Sign-in for the map page isn't set up on this server yet.", status_code=503)
    import secrets
    import urllib.parse
    state = secrets.token_urlsafe(24)
    q = urllib.parse.urlencode({"client_id": WORKOS_CLIENT_ID, "redirect_uri": f"{PUBLIC_URL}/auth/callback",
                                "response_type": "code", "provider": "authkit", "state": state})
    r = Response(status_code=303, headers={"Location": f"https://api.workos.com/user_management/authorize?{q}", **NOSTORE})
    r.set_cookie("rm_state", state, max_age=600, httponly=True, secure=True, samesite="lax")
    r.set_cookie("rm_next", next if next.startswith("/") and not next.startswith("//") else "/map",
                 max_age=600, httponly=True, secure=True, samesite="lax")
    return r


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = ""):
    if not code or not state or not hmac.compare_digest(state, request.cookies.get("rm_state", "")):
        return PlainTextResponse("Sign-in didn't complete. Open the map link again.", status_code=400, headers=NOSTORE)
    import urllib.request
    req = urllib.request.Request(
        "https://api.workos.com/user_management/authenticate", method="POST",
        data=json.dumps({"client_id": WORKOS_CLIENT_ID, "client_secret": WORKOS_API_KEY,
                         "grant_type": "authorization_code", "code": code}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            user = json.load(resp)["user"]
    except Exception:
        return PlainTextResponse("Sign-in failed. Try again.", status_code=502, headers=NOSTORE)
    nxt = request.cookies.get("rm_next", "/map")
    r = Response(status_code=303, headers={"Location": nxt if nxt.startswith("/") and not nxt.startswith("//") else "/map",
                                           **NOSTORE})
    r.delete_cookie("rm_next")
    r.set_cookie(COOKIE, _sign({"u": _user_id(user["id"]), "e": user.get("email", ""),
                                "exp": int(time.time()) + SESSION_DAYS * 86400}),
                 max_age=SESSION_DAYS * 86400, httponly=True, secure=True, samesite="lax")
    r.delete_cookie("rm_state")
    return r


@app.get("/logout")
def logout():
    r = PlainTextResponse("Signed out of the remember-me map.", headers=NOSTORE)
    r.delete_cookie(COOKIE)
    return r


@app.get("/api/data")
def api_data(request: Request):
    user = _web_user(request)
    if not user:
        return JSONResponse({"error": "sign in at /map"}, status_code=401, headers=NOSTORE)
    with _tool_lock, pool().connection() as conn:
        memmap._LOG_CACHE.clear()
        data = viz.build_data(None, root=store_pg.PgStore(conn, user))
        for p, h in _holds(conn, user).items():
            if p in data["projects"]:
                data["projects"][p]["held"] = h
    return JSONResponse(data, headers=NOSTORE)


@app.post("/api/op")
async def api_op(request: Request):
    user = _web_user(request)
    if not user:
        return JSONResponse({"error": "sign in at /map"}, status_code=401)
    if request.headers.get("origin") not in (None, PUBLIC_URL):  # the cookie is SameSite=Lax; refuse other sites too
        return JSONResponse({"error": "bad origin"}, status_code=403)
    body = await request.json()
    project, op = body.get("project"), body.get("op") or {}
    if op.get("op") not in ("node.update", "node.merge", "node.move"):
        return PlainTextResponse("only node.update, node.merge and node.move are allowed from the viewer", status_code=400)
    import anyio

    def apply():
        with _tool_lock, pool().connection() as conn:
            if project in _holds(conn, user):
                raise ValueError(f"{project} is offline on another computer; it's read-only here")
            memmap._LOG_CACHE.clear()
            st = store_pg.PgStore(conn, user)
            (target, ev), = memmap.apply_ops(st, project, [op], by="user")
            g = memmap.save_cache(st, target)
            return {"ok": True, "seq": ev["seq"], "warnings": g["warnings"][-3:]}
    try:
        return JSONResponse(await anyio.to_thread.run_sync(apply))
    except Exception as e:
        return PlainTextResponse(f"{type(e).__name__}: {e}", status_code=400)


@app.get("/mcp")
def mcp_get():
    return Response(status_code=405, headers={"Allow": "POST"})


@app.get("/account/export")
def export(request: Request):
    """Everything this user has stored, as a zip in the same layout as ~/.remember (so it works offline too)."""
    user = _web_user(request) or user_for(_token(request))
    if not user:
        return _unauthorized(request)
    buf = io.BytesIO()
    with pool().connection() as conn, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in store_pg.PgStore(conn, user).walk():
            z.writestr(path, content)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="remember-me-export.zip"', **NOSTORE})


@app.delete("/account/data")
def delete_all(request: Request):
    """Delete everything this user has stored."""
    user = user_for(_token(request))
    if not user:
        return _unauthorized(request)
    with pool().connection() as conn:
        store_pg.PgStore(conn, user).delete_all()
    return {"ok": True, "deleted": "all of your remember-me memory"}


# ---------------------------------------------------------------- computers: device sign-in
# A computer running the local remember-me server signs in once: it gets a code and a link, the user opens the link
# (signed in to remember-me in the browser) and allows it, and the computer receives a long-lived token for this
# account. Same pattern as `fastapi login` or a TV app.

DEVICE_TTL = 600


def _user_code():
    import secrets
    a = "BCDFGHJKLMNPQRSTVWXZ"
    return "".join(secrets.choice(a) for _ in range(4)) + "-" + "".join(secrets.choice(a) for _ in range(4))


@app.post("/device/start")
async def device_start(request: Request):
    import secrets
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str(body.get("name") or "a computer")[:80]
    dc, uc = secrets.token_urlsafe(32), _user_code()
    with pool().connection() as c:
        c.execute("delete from rm_device_codes where expires < now()")
        c.execute("insert into rm_device_codes (device_code, user_code, name, expires) "
                  "values (%s, %s, %s, now() + make_interval(secs => %s))", (dc, uc, name, DEVICE_TTL))
    return {"device_code": dc, "user_code": uc, "verification_uri": f"{PUBLIC_URL}/device",
            "verification_uri_complete": f"{PUBLIC_URL}/device?code={uc}", "interval": 3, "expires_in": DEVICE_TTL}


def _page(title, body):
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>:root{{--bg:#fbfaf7;--fg:#1f1d1a;--mut:#6b665e;--acc:#2f5d50;--line:#e4e0d8}}
@media (prefers-color-scheme:dark){{:root{{--bg:#181715;--fg:#ece8e1;--mut:#a39d93;--acc:#7fb3a3;--line:#34312c}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}}
main{{max-width:28rem;margin:12vh auto;padding:0 16px}}h1{{font-size:1.3rem}}p{{color:var(--mut)}}
code{{font-size:1.4rem;letter-spacing:.08em;color:var(--fg)}}button{{font:inherit;padding:.6rem 1.2rem;border-radius:8px;
border:0;background:var(--acc);color:var(--bg);cursor:pointer}}</style></head><body><main>{body}</main></body></html>""",
                        headers=NOSTORE)


@app.get("/device")
def device_page(request: Request, code: str = ""):
    import html
    import urllib.parse
    if not _web_user(request):
        return Response(status_code=303, headers={"Location": "/login?next=" + urllib.parse.quote(f"/device?code={code}"),
                                                  **NOSTORE})
    with pool().connection() as c:
        row = c.execute("select name from rm_device_codes where user_code = %s and expires > now() and user_id is null",
                        (code.upper(),)).fetchone()
    if not row:
        return _page("remember-me", "<h1>That code has expired or was already used</h1><p>Ask your assistant to "
                                    "connect this computer again.</p>")
    return _page("Allow this computer", f"""<h1>Allow {html.escape(row[0])} to use your remember-me memory?</h1>
<p>Check this code matches the one your assistant showed you:</p><p><code>{html.escape(code.upper())}</code></p>
<p>It will be able to read and save memory in your account, and take projects offline.</p>
<form method="post" action="/device/approve"><input type="hidden" name="code" value="{html.escape(code.upper())}">
<button>Allow</button></form>""")


@app.post("/device/approve")
async def device_approve(request: Request):
    import secrets
    user = _web_user(request)
    if not user:
        return Response(status_code=303, headers={"Location": "/login", **NOSTORE})
    if request.headers.get("origin") not in (None, PUBLIC_URL):
        return PlainTextResponse("bad origin", status_code=403)
    form = await request.form()
    code = str(form.get("code", "")).upper()
    token = "rmd_" + secrets.token_urlsafe(32)
    with pool().connection() as c:
        row = c.execute("update rm_device_codes set user_id = %s, token = %s where user_code = %s and expires > now() "
                        "and user_id is null returning name", (user, token, code)).fetchone()
        if not row:
            return _page("remember-me", "<h1>That code has expired or was already used</h1>")
        c.execute("insert into rm_tokens (token_hash, user_id, device_id, name) values (%s, %s, %s, %s)",
                  (hashlib.sha256(token.encode()).hexdigest(), user, "dev_" + secrets.token_hex(8), row[0]))
    return _page("Done", f"<h1>Done</h1><p>{row[0]} is connected to your remember-me memory. You can close this page "
                         "and go back to your assistant.</p>")


@app.post("/device/poll")
async def device_poll(request: Request):
    body = await request.json()
    with pool().connection() as c:
        row = c.execute("select user_id, token, expires > now() from rm_device_codes where device_code = %s",
                        (str(body.get("device_code", "")),)).fetchone()
        if not row or (not row[0] and not row[2]):
            return JSONResponse({"status": "expired"}, status_code=400)
        if not row[0]:
            return {"status": "pending"}
        if row[1]:  # hand the token over once
            c.execute("update rm_device_codes set token = null where device_code = %s", (body["device_code"],))
            return {"status": "approved", "token": row[1]}
        return JSONResponse({"status": "expired"}, status_code=400)


# ---------------------------------------------------------------- sync: taking projects offline and back
# Only signed-in computers (device tokens) use these. A project lives in one place at a time: a computer that takes it
# offline "holds" it (online it becomes read-only, or is removed if the user doesn't want a cloud copy); coming back
# online replaces the online copy with the computer's and releases the hold. So there is never anything to merge.

def _device_call(request):
    user, device = principal(_token(request))
    if not user or not device:
        return None, None, JSONResponse({"error": "sign in this computer first (device sign-in)"}, status_code=401)
    return user, device, None


def _project_prefixes(p):
    return (f"log/{p}/", f"maps/{p}/")


@app.get("/api/sync/state")
def sync_state(request: Request):
    user, device, err = _device_call(request)
    if err:
        return err
    with pool().connection() as conn:
        st = store_pg.PgStore(conn, user)
        holds = _holds(conn, user)
        projects = {}
        for sub in ("log", "maps"):
            for p in st.listdir(sub):
                projects.setdefault(p, {"files": 0})
        for p in projects:
            projects[p]["files"] = sum(len(st.files(x)) for x in _project_prefixes(p))
        held_here = {r[0] for r in conn.execute("select project from rm_holds where user_id = %s and device_id = %s",
                                                (user, device["id"]))}
        for p, h in holds.items():
            projects.setdefault(p, {"files": 0})["hold"] = {**h, "this_computer": p in held_here}
    return {"projects": projects, "device": device["name"]}


@app.get("/api/sync/pull")
def sync_pull(request: Request, project: str):
    user, device, err = _device_call(request)
    if err:
        return err
    buf = io.BytesIO()
    with pool().connection() as conn, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        st = store_pg.PgStore(conn, user)
        for prefix in _project_prefixes(project):
            for path, content in st.files(prefix):
                z.writestr(path, content)
    return Response(buf.getvalue(), media_type="application/zip", headers=NOSTORE)


@app.post("/api/sync/hold")
async def sync_hold(request: Request):
    user, device, err = _device_call(request)
    if err:
        return err
    body = await request.json()
    p, keep = body["project"], bool(body.get("keep_online", True))
    with pool().connection() as conn:
        row = conn.execute("select device_id, device_name from rm_holds where user_id = %s and project = %s",
                           (user, p)).fetchone()
        if row and row[0] != device["id"]:
            return JSONResponse({"error": f"{p} is already offline on {row[1]}"}, status_code=409)
        conn.execute("insert into rm_holds (user_id, project, device_id, device_name, keep_online) "
                     "values (%s, %s, %s, %s, %s) on conflict (user_id, project) do update set "
                     "device_id = excluded.device_id, device_name = excluded.device_name, "
                     "keep_online = excluded.keep_online, since = now()", (user, p, device["id"], device["name"], keep))
        if not keep:
            st = store_pg.PgStore(conn, user)
            for prefix in _project_prefixes(p):
                st.delete_prefix(prefix)
    return {"ok": True, "project": p, "keep_online": keep}


@app.post("/api/sync/push")
async def sync_push(request: Request, project: str, release: bool = True):
    """Replace the online copy of a project with the computer's (a zip in the ~/.remember layout)."""
    user, device, err = _device_call(request)
    if err:
        return err
    data = await request.body()
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        files = {n: z.read(n).decode("utf-8") for n in z.namelist() if not n.endswith("/")}
    except Exception:
        return JSONResponse({"error": "expected a zip"}, status_code=400)
    prefixes = _project_prefixes(project)
    if any(not n.startswith(prefixes) for n in files):
        return JSONResponse({"error": f"every file must be under log/{project}/ or maps/{project}/"}, status_code=400)
    with pool().connection() as conn:
        row = conn.execute("select device_id, device_name from rm_holds where user_id = %s and project = %s",
                           (user, project)).fetchone()
        if row and row[0] != device["id"]:
            return JSONResponse({"error": f"{project} is offline on {row[1]}"}, status_code=409)
        st = store_pg.PgStore(conn, user)
        with conn.transaction():
            for prefix in prefixes:
                st.delete_prefix(prefix)
            for n, text in files.items():
                st.write(n, text)
        if release:
            conn.execute("delete from rm_holds where user_id = %s and project = %s", (user, project))
    return {"ok": True, "project": project, "files": len(files), "released": release}


@app.post("/api/sync/release")
async def sync_release(request: Request):
    user, device, err = _device_call(request)
    if err:
        return err
    p = (await request.json())["project"]
    with pool().connection() as conn:
        conn.execute("delete from rm_holds where user_id = %s and project = %s and device_id = %s",
                     (user, p, device["id"]))
    return {"ok": True}


# ---------------------------------------------------------------- account page
# /account: what's stored, export, import (a ~/.remember zip or a claude.ai export, read in the browser so only what
# the user picks is sent), connected computers, deleting a project or everything.

ACCOUNT_HTML = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "account.html"), encoding="utf-8").read()


def _web_call(request, mutate=False):
    user = _web_user(request)
    if not user:
        return None, JSONResponse({"error": "sign in again"}, status_code=401, headers=NOSTORE)
    if mutate and request.headers.get("origin") not in (None, PUBLIC_URL):
        return None, JSONResponse({"error": "bad origin"}, status_code=403)
    return user, None


@app.get("/account")
def account_page(request: Request):
    if not _web_user(request):
        return Response(status_code=303, headers={"Location": "/login?next=/account", **NOSTORE})
    return HTMLResponse(ACCOUNT_HTML, headers=NOSTORE)


@app.get("/api/account")
def account_data(request: Request):
    user, err = _web_call(request)
    if err:
        return err
    email = (_unsign(request.cookies.get(COOKIE, "")) or {}).get("e", "")
    with _tool_lock, pool().connection() as conn:
        st = store_pg.PgStore(conn, user)
        holds = _holds(conn, user)
        names = sorted(set(st.listdir("log")) | set(st.listdir("maps")))
        projects = []
        for p in names:
            files = st.files(f"log/{p}/") + st.files(f"maps/{p}/")
            projects.append({"name": p, "files": len(files), "bytes": sum(len(c.encode()) for _, c in files),
                             "chats": sum(1 for f, _ in files if f.startswith("log/") and f.endswith(".md")
                                          and not f.endswith(".compactions.md")),
                             "mapped": any(f == f"maps/{p}/events.jsonl" for f, _ in files), "held": holds.get(p)})
        devices = [{"id": r[0], "name": r[1], "created": r[2].isoformat(), "last_used": r[3].isoformat() if r[3] else None}
                   for r in conn.execute("select device_id, name, min(created), max(last_used) from rm_tokens "
                                         "where user_id = %s group by device_id, name order by 3", (user,))]
    return JSONResponse({"email": email, "projects": projects, "devices": devices}, headers=NOSTORE)


@app.delete("/api/account/project")
def account_delete_project(request: Request, name: str):
    user, err = _web_call(request, mutate=True)
    if err:
        return err
    with _tool_lock, pool().connection() as conn:
        st = store_pg.PgStore(conn, user)
        for prefix in (f"log/{name}/", f"maps/{name}/"):
            st.delete_prefix(prefix)
        conn.execute("delete from rm_holds where user_id = %s and project = %s", (user, name))
    return {"ok": True}


@app.delete("/api/account/device")
def account_revoke_device(request: Request, id: str):
    user, err = _web_call(request, mutate=True)
    if err:
        return err
    with pool().connection() as conn:
        conn.execute("delete from rm_tokens where user_id = %s and device_id = %s", (user, id))
    return {"ok": True}


@app.delete("/api/account/all")
def account_delete_all(request: Request, confirm: str = ""):
    user, err = _web_call(request, mutate=True)
    if err:
        return err
    if confirm != "delete everything":
        return JSONResponse({"error": "confirm with the words: delete everything"}, status_code=400)
    with _tool_lock, pool().connection() as conn:
        store_pg.PgStore(conn, user).delete_all()
        conn.execute("delete from rm_holds where user_id = %s", (user,))
    return {"ok": True}


@app.post("/api/account/import/files")
async def account_import_files(request: Request):
    """One project from a ~/.remember folder (zipped): {project, files: {path: text}, replace: bool}."""
    user, err = _web_call(request, mutate=True)
    if err:
        return err
    body = await request.json()
    import cli
    p = cli.slug(body.get("project") or "")
    files = body.get("files") or {}
    if not p or not files or any(not k.startswith((f"log/{p}/", f"maps/{p}/")) for k in files):
        return JSONResponse({"error": f"files must all be under log/{p}/ or maps/{p}/"}, status_code=400)
    with _tool_lock, pool().connection() as conn:
        st = store_pg.PgStore(conn, user)
        if p in _holds(conn, user):
            return JSONResponse({"error": f"{p} is offline on a computer; bring it back online there first"}, status_code=409)
        exists = st.isdir(f"log/{p}") or st.isdir(f"maps/{p}")
        if exists and not body.get("replace"):
            return JSONResponse({"error": f"{p} already exists here", "exists": True}, status_code=409)
        with conn.transaction():
            for prefix in (f"log/{p}/", f"maps/{p}/"):
                st.delete_prefix(prefix)
            for k, v in files.items():
                st.write(k, v)
        memmap._LOG_CACHE.clear()
    return {"ok": True, "project": p, "files": len(files), "replaced": exists}


@app.post("/api/account/import/claude")
async def account_import_claude(request: Request):
    """Conversations picked from a claude.ai data export: {project, conversations: [raw conversation objects]}.
    They join the project's log; the user's assistant maps them the next time they work in that project."""
    user, err = _web_call(request, mutate=True)
    if err:
        return err
    body = await request.json()
    import cli
    import tempfile
    p = cli.slug(body.get("project") or "")
    convs = [c for c in body.get("conversations") or [] if isinstance(c, dict) and c.get("uuid")]
    if not p or not convs:
        return JSONResponse({"error": "pick a project name and at least one conversation"}, status_code=400)
    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="rm-import-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(convs, f)
    try:
        with _tool_lock, pool().connection() as conn:
            if p in _holds(conn, user):
                return JSONResponse({"error": f"{p} is offline on a computer; bring it back online there first"},
                                    status_code=409)
            report = cli.import_export(store_pg.PgStore(conn, user), p, tmp, picks=["all"])
            memmap._LOG_CACHE.clear()
    finally:
        os.remove(tmp)
    return {"ok": True, "project": p, "report": report}


@app.get("/privacy")
def privacy():
    return HTMLResponse(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "privacy.html"),
                             encoding="utf-8").read())
