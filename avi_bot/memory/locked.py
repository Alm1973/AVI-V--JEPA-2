"""One SQLite connection, safe to share between the dashboard's threads.

The dashboard answers every request on its own thread, and the seat loop,
the latent logger and the brain each have one more; the presence, memory,
usage and desk stores are each a single connection opened with
check_same_thread=False and used from all of them. That flag only turns
off Python's check -- it does not make concurrent use safe. On the MSI a
/health request landing during a presence sample raised

    sqlite3.InterfaceError: bad parameter or other API misuse

(2026-10-08). So every call takes one lock, and a query's rows are read
out while it is held: a cursor iterated after the lock is released would
be the same race again.
"""
import sqlite3
import threading


class Rows(list):
    """A finished query: iterate it, or fetchone/fetchall it, like a
    cursor -- but its rows were already read under the lock."""

    def __init__(self, rows, lastrowid=None, rowcount=-1):
        super().__init__(rows)
        self.lastrowid = lastrowid
        self.rowcount = rowcount
        self._next = 0

    def fetchone(self):
        if self._next >= len(self):
            return None
        self._next += 1
        return self[self._next - 1]

    def fetchall(self):
        out = self[self._next:]
        self._next = len(self)
        return list(out)


class Connection:
    def __init__(self, path):
        self._db = sqlite3.connect(path, check_same_thread=False)
        # Re-entrant, so a store can hold it across several statements
        # (`with store.db.lock:`) and still call execute inside.
        self.lock = threading.RLock()

    @property
    def row_factory(self):
        return self._db.row_factory

    @row_factory.setter
    def row_factory(self, factory):
        with self.lock:
            self._db.row_factory = factory

    def execute(self, sql, params=()):
        with self.lock:
            cur = self._db.execute(sql, params)
            return Rows(cur.fetchall(), cur.lastrowid, cur.rowcount)

    def executescript(self, script):
        with self.lock:
            self._db.executescript(script)

    def commit(self):
        with self.lock:
            self._db.commit()

    def close(self):
        with self.lock:
            self._db.close()


def connect(path):
    return Connection(path)
