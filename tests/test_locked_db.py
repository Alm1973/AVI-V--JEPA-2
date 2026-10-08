"""The stores the dashboard's threads share: one lock, rows read under it."""
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avi_bot.memory import db as memdb                        # noqa: E402
from avi_bot.memory import locked                             # noqa: E402
from avi_bot.perception import presence as presence_mod       # noqa: E402


class LikeACursor(unittest.TestCase):
    def setUp(self):
        self.db = locked.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("CREATE TABLE t (a INTEGER, b TEXT);")

    def test_fetchone_walks_then_runs_out(self):
        for i in range(3):
            self.db.execute("INSERT INTO t VALUES (?, ?)", (i, str(i)))
        rows = self.db.execute("SELECT a FROM t ORDER BY a")
        self.assertEqual(rows.fetchone()[0], 0)
        self.assertEqual([r[0] for r in rows.fetchall()], [1, 2])
        self.assertIsNone(rows.fetchone())

    def test_iterates(self):
        self.db.execute("INSERT INTO t VALUES (1, 'x')")
        self.assertEqual([(a, b) for a, b in self.db.execute(
            "SELECT a, b FROM t")], [(1, "x")])

    def test_lastrowid_and_rowcount(self):
        cur = self.db.execute("INSERT INTO t VALUES (5, 'y')")
        self.assertEqual(cur.lastrowid, 1)
        self.db.execute("INSERT INTO t VALUES (6, 'y')")
        self.assertEqual(self.db.execute(
            "DELETE FROM t WHERE b = 'y'").rowcount, 2)

    def test_row_factory_passes_through(self):
        self.db.row_factory = sqlite3.Row
        self.db.execute("INSERT INTO t VALUES (7, 'z')")
        self.assertEqual(self.db.execute("SELECT b FROM t").fetchone()["b"],
                         "z")


def hammer(work, threads=8, each=150):
    errors = []

    def run(k):
        try:
            for i in range(each):
                work(k, i)
        except Exception as exc:          # collected, not lost in a thread
            errors.append(repr(exc))
    ts = [threading.Thread(target=run, args=(k,)) for k in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return errors


class SharedBetweenThreads(unittest.TestCase):
    """The MSI crash: /health read presence while the seat loop wrote it."""

    def test_presence_reads_and_writes_at_once(self):
        with tempfile.TemporaryDirectory() as d:
            p = presence_mod.Presence(str(Path(d) / "p.db"))
            self.addCleanup(p.close)

            def work(k, i):
                if k % 2:
                    p.sample(bool(i % 3), conf=0.9)
                else:
                    p.recent(5)
                    p.summary()
            self.assertEqual(hammer(work), [])
            self.assertEqual(len(p.recent(10_000)), 4 * 150)

    def test_memory_store_from_many_threads(self):
        with tempfile.TemporaryDirectory() as d:
            s = memdb.Store(str(Path(d) / "m.db"))
            self.addCleanup(s.close)

            def work(k, i):
                s.remember(f"fact {k}-{i % 20}")
                s.recall("fact")
            self.assertEqual(hammer(work, each=60), [])
            self.assertLessEqual(s.count_facts(), memdb.MAX_FACTS)


if __name__ == "__main__":
    unittest.main()
