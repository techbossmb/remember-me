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
import hashlib
import io
import json
import os
import sys
import threading
import time
import zipfile

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "remember"))
import memmap  # noqa: E402
import mcp_server  # noqa: E402
import store_pg  # noqa: E402

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
        return mcp_server.respond(store_pg.PgStore(conn, user), msg, online=True)


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
