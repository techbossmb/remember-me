# remember.me online (proof of concept)

The same `remember_*` tools as the local plugin, served over MCP's HTTP transport, with each signed-in user's memory
in Postgres. Memory is stored in the same layout as `~/.remember` (see `remember/store.py`), so `/account/export`
gives a zip that works offline, and offline memory can be loaded online.

## Pieces
- **FastAPI Cloud** runs `online/main.py` (`[tool.fastapi] entrypoint` in the repo's `pyproject.toml`).
- **Neon** holds the data: one table, `rm_files`, every row keyed by user id.
- **An auth provider** signs people in (Google and the like) and issues tokens; this service only checks them.
  It has to support how MCP clients like Claude sign in: OAuth 2.1 with PKCE and dynamic client registration.

## Endpoints
| | |
|---|---|
| `POST /mcp` | the MCP endpoint (JSON responses; no streaming needed) |
| `GET /.well-known/oauth-protected-resource` | tells clients where to sign in |
| `GET /account/export` · `DELETE /account/data` | a user's whole memory as a zip; delete all of it |
| `GET /healthz` · `GET /healthz/db` | liveness; database round-trip time (shows Neon's wake-up delay) |

## Settings
`REMEMBER_DATABASE_URL` (Neon pooled URL), `REMEMBER_PUBLIC_URL`, `AUTH_ISSUER`, optional `AUTH_JWKS_URL` and
`AUTH_AUDIENCE`. For local testing only: `REMEMBER_DEV_TOKENS="token=user,…"`.

## Tests
`REMEMBER_DATABASE_URL=… python3 online/test_online.py` (sign-in, isolation between users, every core tool, export,
delete) and `REMEMBER_TEST_BACKEND=pg REMEMBER_DATABASE_URL=… python3 tests/golden.py` (same output as offline).

## Known limits of the proof of concept
- Tool calls run one at a time per server process (the tool code captures printed output). Fine for a handful of
  users; the next step is having commands return text instead of printing.
- Each log file is one row that grows as messages are added. Fine at personal scale; split into per-message rows
  if logs get large.
- No hosted map viewer yet; `remember_show_map` is local-only.
