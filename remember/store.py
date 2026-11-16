"""Where memory lives. Everything above this module (the log, the map, the tools) reads and writes through a Store,
using file-like paths relative to the store root:

  projects.json                      folder -> project registry (offline only)
  log/<project>/<session>.md         verbatim log, one file per conversation
  log/<project>/<session>.meta.json  where that conversation came from
  log/<project>/<session>.state.json which messages are already in the log (dedup)
  maps/<project>/events.jsonl        the map's append-only history of edits
  maps/<project>/sources.json        timeline of the log files (derived)
  maps/<project>/graph.json          cache of the replayed map (derived)

FileStore keeps these as real files under a folder (offline, the default: ~/.remember). The same layout is the
export format, so memory moves between offline and online unchanged. PgStore (store_pg.py) keeps them as rows in
Postgres, one user per store, so one user's store can never read another's.
"""
import contextlib
import os


class Store:
    """Interface. Paths use '/' and are relative to the store root. Directories are implied by the paths in them."""
    path = None  # a local folder, for features that only exist offline (viewer, hooks, background builds)

    @property
    def key(self):  # identity, for caches
        raise NotImplementedError

    def read(self, rel):  # -> str, or None if missing
        raise NotImplementedError

    def write(self, rel, text):
        raise NotImplementedError

    def append(self, rel, text):
        raise NotImplementedError

    def exists(self, rel):  # a file, or a directory with something in it
        raise NotImplementedError

    def isdir(self, rel):
        raise NotImplementedError

    def listdir(self, rel):  # sorted immediate children (files and directories); [] if none
        raise NotImplementedError

    def remove(self, rel):
        raise NotImplementedError

    def mtime(self, rel):  # seconds since the epoch, or None
        raise NotImplementedError

    def mkdir(self, rel):  # make a (possibly empty) directory exist
        raise NotImplementedError

    def lock(self, rel):  # context manager: exclusive across processes/connections for this path
        raise NotImplementedError

    def __str__(self):
        return self.path or "online"


def _lock_file(f):
    """Exclusive lock on an open file, released when it closes. POSIX flock; on Windows msvcrt."""
    try:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX)
    except ImportError:
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)


class FileStore(Store):
    def __init__(self, root):
        self.path = os.path.abspath(root)

    @property
    def key(self):
        return self.path

    def _p(self, rel):
        return os.path.join(self.path, *rel.split("/")) if rel else self.path

    def read(self, rel):
        try:
            with open(self._p(rel), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def write(self, rel, text):
        p = self._p(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def append(self, rel, text):
        p = self._p(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(text)

    def exists(self, rel):
        return os.path.exists(self._p(rel))

    def isdir(self, rel):
        return os.path.isdir(self._p(rel))

    def listdir(self, rel):
        p = self._p(rel)
        return sorted(os.listdir(p)) if os.path.isdir(p) else []

    def remove(self, rel):
        try:
            os.remove(self._p(rel))
        except FileNotFoundError:
            pass

    def mtime(self, rel):
        try:
            return os.path.getmtime(self._p(rel))
        except OSError:
            return None

    def mkdir(self, rel):
        os.makedirs(self._p(rel), exist_ok=True)

    @contextlib.contextmanager
    def lock(self, rel):
        p = self._p(rel)  # lock the file itself (created empty if missing), as earlier versions did
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a+", encoding="utf-8") as f:
            _lock_file(f)
            yield


_FILE_STORES = {}


def as_store(x):
    """A Store from a Store or a folder path (the code and tests used to pass folder paths around)."""
    if isinstance(x, Store):
        return x
    p = os.path.abspath(x)
    if p not in _FILE_STORES:
        _FILE_STORES[p] = FileStore(p)
    return _FILE_STORES[p]
