"""Row-level security: the database itself keeps users apart, even for queries that forget the user filter.
Run as a NON-superuser role (superusers bypass RLS): REMEMBER_DATABASE_URL=postgresql://app_role@... python3 online/test_rls.py"""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "remember"))
import store_pg  # noqa: E402

c = store_pg.connect(os.environ["REMEMBER_DATABASE_URL"])
store_pg.ensure_schema(c)
run = uuid.uuid4().hex[:8]
a, b = store_pg.PgStore(c, f"rls-a-{run}"), store_pg.PgStore(c, f"rls-b-{run}")
a.write("log/p/s.md", "a secret")
b.write("log/p/s.md", "b secret")
ok = True


def check(label, cond):
    global ok
    print(("ok   " if cond else "FAIL ") + label)
    ok &= bool(cond)


check("not superuser (else RLS is bypassed)", not c.execute("select rolsuper from pg_roles where rolname = current_user").fetchone()[0])
check("no user named: no rows", c.execute("select count(*) from rm_files").fetchone()[0] == 0)
check("unfiltered query sees only own rows", a._x("select user_id from rm_files") == [(a.user,)])
try:
    a._x("insert into rm_files (user_id, path, content) values (%s, 'x', 'forged')", (b.user,))
    check("writing another user's row refused", False)
except Exception:
    check("writing another user's row refused", True)
a._x("delete from rm_files")
check("unfiltered delete removes only own rows", b.read("log/p/s.md") == "b secret" and a.read("log/p/s.md") is None)
b.delete_all()
sys.exit(0 if ok else 1)
