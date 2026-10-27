"""Tests for censusdb.CensusStore. Run: .venv\\Scripts\\python.exe test_censusdb.py"""
import json
import os
import unittest

from census import MapCensus
from censusdb import CensusStore

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "tests", "census", "burnt_land_capture.jsonl")


class CensusStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = CensusStore(":memory:")
        c = MapCensus()
        with open(FIXTURE, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                p = bytes.fromhex(r["hex"])
                cls.store.observe(c, r["ts"], p, c.feed(r["ts"], p))
        s = c.finish()
        cls.store.finish(s)
        cls.db = cls.store.db

    def q(self, sql):
        return self.db.execute(sql).fetchall()

    def test_one_row_per_visit_with_summary(self):
        rows = self.q("SELECT map, summary FROM visits ORDER BY id")
        self.assertEqual([r[0] for r in rows], [10004114, 10004115])
        self.assertEqual(json.loads(rows[0][1])["peak_alive"], 15)

    def test_every_refill_is_stored(self):
        # the summaries count 10 and 3 refills; the store must hold all of them, the last ones included
        self.assertEqual(self.q("SELECT visit, COUNT(*), MIN(alive), MAX(alive) FROM refills GROUP BY visit"),
                         [(1, 10, 15, 15), (2, 3, 17, 17)])
        pos = json.loads(self.q("SELECT positions FROM refills LIMIT 1")[0][0])
        self.assertTrue(pos and all(len(p) == 2 for p in pos))

    def test_spawns_and_removals(self):
        (n, arrival, placed), = self.q("SELECT COUNT(*), SUM(arrival), SUM(x IS NOT NULL) FROM spawns")
        self.assertEqual(n, placed)
        self.assertGreater(arrival, 0)
        self.assertGreater(self.q("SELECT COUNT(*) FROM removals")[0][0], 100)

    def test_players(self):
        self.assertEqual(self.q("SELECT COUNT(DISTINCT char_id) FROM players WHERE visit = 1 AND event = 'enter'"), [(4,)])


if __name__ == "__main__":
    unittest.main()
