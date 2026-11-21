"""remember.me online: the same remember_* tools as the local plugin, served over MCP's HTTP transport, with each
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


app = FastAPI(title="remember.me", docs_url=None, redoc_url=None, lifespan=lifespan)

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
                    break
                except Exception:
                    if attempt == 2:
                        p.close()
                        raise
                    time.sleep(1 + attempt)
            _pool = p
        return _pool


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


def user_for(token):
    """The user id a bearer token belongs to, or None if it isn't valid."""
    if not token:
        return None
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
    return JSONResponse({"error": "unauthorized", "message": "sign in to remember.me"}, status_code=401,
                        headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})


def _token(request):
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else None

# ---------------------------------------------------------------- routes


@app.get("/")
def home():
    return PlainTextResponse("remember.me: long-term memory for AI assistants. Add this service's /mcp address to "
                             "your assistant as a connector.\n")


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
            "bearer_methods_supported": ["header"], "resource_name": "remember.me"}


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


def _handle(user, msg):
    with _tool_lock, pool().connection() as conn:
        memmap._LOG_CACHE.clear()  # another worker may have written since
        return mcp_server.respond(store_pg.PgStore(conn, user), msg, online=True, show_map=_show_map)

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
            "with the same account they connected remember.me with, and then stay signed in.")


@app.get("/map")
def map_page(request: Request):
    if not _web_user(request):
        return Response(status_code=303, headers={"Location": "/login", **NOSTORE})
    return HTMLResponse(viz.page(), headers=NOSTORE)


@app.get("/login")
def login():
    if not (WORKOS_CLIENT_ID and SESSION_SECRET):
        return PlainTextResponse("Sign-in for the map page isn't set up on this server yet.", status_code=503)
    import secrets
    import urllib.parse
    state = secrets.token_urlsafe(24)
    q = urllib.parse.urlencode({"client_id": WORKOS_CLIENT_ID, "redirect_uri": f"{PUBLIC_URL}/auth/callback",
                                "response_type": "code", "provider": "authkit", "state": state})
    r = Response(status_code=303, headers={"Location": f"https://api.workos.com/user_management/authorize?{q}", **NOSTORE})
    r.set_cookie("rm_state", state, max_age=600, httponly=True, secure=True, samesite="lax")
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
    r = Response(status_code=303, headers={"Location": "/map", **NOSTORE})
    r.set_cookie(COOKIE, _sign({"u": _user_id(user["id"]), "exp": int(time.time()) + SESSION_DAYS * 86400}),
                 max_age=SESSION_DAYS * 86400, httponly=True, secure=True, samesite="lax")
    r.delete_cookie("rm_state")
    return r


@app.get("/logout")
def logout():
    r = PlainTextResponse("Signed out of the remember.me map.", headers=NOSTORE)
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
    user = user_for(_token(request))
    if not user:
        return _unauthorized(request)
    buf = io.BytesIO()
    with pool().connection() as conn, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in store_pg.PgStore(conn, user).walk():
            z.writestr(path, content)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="remember-export.zip"'})


@app.delete("/account/data")
def delete_all(request: Request):
    """Delete everything this user has stored."""
    user = user_for(_token(request))
    if not user:
        return _unauthorized(request)
    with pool().connection() as conn:
        store_pg.PgStore(conn, user).delete_all()
    return {"ok": True, "deleted": "all of your remember.me memory"}
