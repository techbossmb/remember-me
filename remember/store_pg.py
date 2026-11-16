"""Postgres backend for the Store interface (store.py): the same file-like layout, kept as rows, one user per store.

Every query is scoped by user_id, so a store can only ever see its own user's memory. Directories are implied by the
paths in them (an empty one is a '<dir>/.keep' row). Locks are Postgres advisory locks held for a transaction, so
several requests or workers writing the same project's map stay in order.

Needs psycopg 3 (`pip install "psycopg[binary]"`). Connection string: REMEMBER_DATABASE_URL (e.g. a Neon URL).
"""
import contextlib

from store import Store

SCHEMA = """
create table if not exists rm_files (
    user_id    text        not null,
    path       text        not null,
    content    text        not null default '',
    updated_at timestamptz not null default now(),
    primary key (user_id, path)
);
"""

KEEP = ".keep"


def _like(prefix):
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def connect(dsn):
    import psycopg
    return psycopg.connect(dsn, autocommit=True)


def ensure_schema(conn):
    conn.execute(SCHEMA)


class PgStore(Store):
    """One user's memory in Postgres. `conn` is a psycopg 3 connection in autocommit mode (one per request/thread)."""

    def __init__(self, conn, user_id):
        if not user_id:
            raise ValueError("PgStore needs a user id")
        self.conn, self.user = conn, str(user_id)

    @property
    def key(self):
        return ("pg", self.user)

    def __str__(self):
        return "online"

    def _one(self, sql, *args):
        row = self.conn.execute(sql, (self.user, *args)).fetchone()
        return row[0] if row else None

    def read(self, rel):
        return self._one("select content from rm_files where user_id = %s and path = %s", rel)

    def write(self, rel, text):
        self.conn.execute("insert into rm_files (user_id, path, content) values (%s, %s, %s) "
                          "on conflict (user_id, path) do update set content = excluded.content, updated_at = now()",
                          (self.user, rel, text))

    def append(self, rel, text):
        self.conn.execute("insert into rm_files (user_id, path, content) values (%s, %s, %s) "
                          "on conflict (user_id, path) do update set content = rm_files.content || excluded.content, "
                          "updated_at = now()", (self.user, rel, text))

    def exists(self, rel):
        return self._one("select exists(select 1 from rm_files where user_id = %s and (path = %s or path like %s))",
                         rel, _like(rel + "/"))

    def isdir(self, rel):
        return self._one("select exists(select 1 from rm_files where user_id = %s and path like %s)",
                         _like(rel + "/" if rel else ""))

    def listdir(self, rel):
        prefix = rel + "/" if rel else ""
        rows = self.conn.execute("select distinct split_part(substr(path, %s), '/', 1) from rm_files "
                                 "where user_id = %s and path like %s", (len(prefix) + 1, self.user, _like(prefix)))
        return sorted(r[0] for r in rows if r[0] and r[0] != KEEP)

    def remove(self, rel):
        self.conn.execute("delete from rm_files where user_id = %s and path = %s", (self.user, rel))

    def mtime(self, rel):
        v = self._one("select extract(epoch from updated_at) from rm_files where user_id = %s and path = %s", rel)
        return float(v) if v is not None else None

    def mkdir(self, rel):
        if not self.isdir(rel):
            self.write(f"{rel}/{KEEP}", "")

    @contextlib.contextmanager
    def lock(self, rel):
        with self.conn.transaction():
            self.conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{self.user}/{rel}",))
            yield

    # --- account-level operations (not part of the Store interface)

    def walk(self):
        """Every file of this user: [(path, content)], for export."""
        return [tuple(r) for r in self.conn.execute(
            "select path, content from rm_files where user_id = %s and path not like %s order by path",
            (self.user, "%/" + KEEP))]

    def delete_all(self):
        """Delete everything this user has stored."""
        self.conn.execute("delete from rm_files where user_id = %s", (self.user,))
