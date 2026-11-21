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


# Row-level security: the database itself only returns or changes the rows of the user named for the current
# transaction (app.user_id), even if a query forgets its own user filter. FORCE makes it apply to the table's owner
# too (the role the service connects as); only superusers bypass it, and the service never connects as one.
RLS = """
alter table rm_files enable row level security;
alter table rm_files force row level security;
do $$ begin
    if not exists (select 1 from pg_policies where tablename = 'rm_files' and policyname = 'rm_files_own_rows') then
        create policy rm_files_own_rows on rm_files
            using (user_id = current_setting('app.user_id', true))
            with check (user_id = current_setting('app.user_id', true));
    end if;
end $$;
"""


def ensure_schema(conn):
    conn.execute(SCHEMA)
    conn.execute(RLS)


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

    def _x(self, sql, args=()):
        """Run one statement as this user: inside a transaction that first names the user for row-level security.
        Per transaction, not per connection, because a pooler (Neon's) may hand each transaction a different one."""
        with self.conn.transaction():
            self.conn.execute("select set_config('app.user_id', %s, true)", (self.user,))
            cur = self.conn.execute(sql, args)
            return cur.fetchall() if cur.description else None

    def _one(self, sql, *args):
        rows = self._x(sql, (self.user, *args))
        return rows[0][0] if rows else None

    def read(self, rel):
        return self._one("select content from rm_files where user_id = %s and path = %s", rel)

    def write(self, rel, text):
        self._x("insert into rm_files (user_id, path, content) values (%s, %s, %s) "
                "on conflict (user_id, path) do update set content = excluded.content, updated_at = now()",
                (self.user, rel, text))

    def append(self, rel, text):
        self._x("insert into rm_files (user_id, path, content) values (%s, %s, %s) "
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
        rows = self._x("select distinct split_part(substr(path, %s), '/', 1) from rm_files "
                       "where user_id = %s and path like %s", (len(prefix) + 1, self.user, _like(prefix)))
        return sorted(r[0] for r in rows if r[0] and r[0] != KEEP)

    def remove(self, rel):
        self._x("delete from rm_files where user_id = %s and path = %s", (self.user, rel))

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
        return [tuple(r) for r in self._x(
            "select path, content from rm_files where user_id = %s and path not like %s order by path",
            (self.user, "%/" + KEEP))]

    def delete_all(self):
        """Delete everything this user has stored."""
        self._x("delete from rm_files where user_id = %s", (self.user,))
