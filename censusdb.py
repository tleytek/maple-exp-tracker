"""SQLite store for the map census, for leaving it running for months.

One file (census.db by default). Every map visit is a row in `visits`; what happened on the
map is stored as events as they arrive (so a crash or power cut loses at most a few seconds):

  visits   (id, map, start, end, summary)   summary = census.Visit.summary() JSON, set when
                                            the visit ends (peak, refills, refill snapshots...)
  spawns   (visit, ts, oid, mob, x, y, arrival)   arrival = 1: already there when we arrived
  removals (visit, ts, oid)                 every death/removal on the map
  kills    (visit, ts, oid, killer)         who landed each kill (character id)
  players  (visit, ts, char_id, level, event)     event 'enter' / 'leave'
  refills  (visit, ts, added, alive, players, positions)   one row per finished refill;
                                            positions = JSON [[x, y], ...] of every monster then

Times are Unix seconds. Coordinates are the client's.
"""
import json
import sqlite3
import struct
import time

from census import (ARRIVAL, BURST_GAP, OP_REMOVE, OP_SPAWN, OP_USER_ENTER, OP_USER_LEAVE,
                    spawn_pos, user_enter)
from killparse import OP_KILLED, _mob_type

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (id INTEGER PRIMARY KEY, map INTEGER, start REAL, end REAL, summary TEXT);
CREATE TABLE IF NOT EXISTS spawns (visit INTEGER, ts REAL, oid INTEGER, mob INTEGER, x INTEGER, y INTEGER, arrival INTEGER);
CREATE TABLE IF NOT EXISTS removals (visit INTEGER, ts REAL, oid INTEGER);
CREATE TABLE IF NOT EXISTS kills (visit INTEGER, ts REAL, oid INTEGER, killer INTEGER);
CREATE TABLE IF NOT EXISTS players (visit INTEGER, ts REAL, char_id INTEGER, level INTEGER, event TEXT);
CREATE TABLE IF NOT EXISTS refills (visit INTEGER, ts REAL, added INTEGER, alive INTEGER, players INTEGER, positions TEXT);
CREATE INDEX IF NOT EXISTS visits_map ON visits (map);
CREATE INDEX IF NOT EXISTS spawns_visit ON spawns (visit);
CREATE INDEX IF NOT EXISTS refills_visit ON refills (visit);
"""


def _u32(b, i):
    return struct.unpack_from("<I", b, i)[0]


class CensusStore:
    COMMIT_EVERY = 5.0   # seconds

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.visit_obj, self.visit_id, self.done = None, None, 0
        self._last_commit = time.time()

    def _row(self, table, *vals):
        self.db.execute(f"INSERT INTO {table} VALUES ({','.join('?' * len(vals))})", vals)

    def observe(self, census, ts, p, summary=None):
        """Call after census.feed(ts, p) with what it returned."""
        if summary:
            self.finish(summary)
        v = census.visit
        if v is not self.visit_obj:
            self.visit_obj, self.done = v, 0
            if v:
                cur = self.db.execute("INSERT INTO visits (map, start, end) VALUES (?, ?, ?)", (v.map_id, v.start, v.start))
                self.visit_id = cur.lastrowid
        if v is None or len(p) < 2:
            return
        op = p[0] | p[1] << 8
        vid = self.visit_id
        if op == OP_SPAWN:
            r = _mob_type(p)
            if r:
                pos = spawn_pos(p) or (None, None)
                self._row("spawns", vid, ts, r[0], r[1], pos[0], pos[1], int(ts - v.start < ARRIVAL))
        elif op == OP_REMOVE and len(p) >= 6:
            self._row("removals", vid, ts, _u32(p, 2))
        elif op == OP_KILLED and len(p) >= 10:
            self._row("kills", vid, ts, _u32(p, 2), _u32(p, 6))
        elif op == OP_USER_ENTER:
            u = user_enter(p)
            if u:
                self._row("players", vid, ts, u[0], u[1], "enter")
        elif op == OP_USER_LEAVE and len(p) >= 6:
            self._row("players", vid, ts, _u32(p, 2), None, "leave")
        # finished refills (the last one is finished once BURST_GAP has passed)
        self._flush_refills(ts)
        if ts - self._last_commit >= self.COMMIT_EVERY:
            self.db.execute("UPDATE visits SET end = ? WHERE id = ?", (ts, vid))
            self.commit(ts)

    def _flush_refills(self, ts=None):
        v = self.visit_obj
        while v and self.done < len(v.bursts) and (ts is None or self.done < len(v.bursts) - 1
                                                   or ts - v.bursts[self.done][0] > BURST_GAP):
            b, snap = v.bursts[self.done], v.snaps[self.done]
            self._row("refills", self.visit_id, b[0], b[1], b[2], snap.get("players"), json.dumps(snap.get("positions", [])))
            self.done += 1

    def finish(self, summary):
        self._flush_refills()
        if self.visit_id is not None:
            self.db.execute("UPDATE visits SET end = ?, summary = ? WHERE id = ?",
                            (summary["start"] + summary["seconds"], json.dumps(summary), self.visit_id))
        self.visit_obj, self.visit_id = None, None
        self.commit()

    def commit(self, ts=None):
        self.db.commit()
        self._last_commit = ts if ts is not None else time.time()

    def import_jsonl(self, path):
        """Add visit summaries from an older census.jsonl (no events)."""
        n = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    s = json.loads(line)
                    self.db.execute("INSERT INTO visits (map, start, end, summary) VALUES (?, ?, ?, ?)",
                                    (s["map"], s["start"], s["start"] + s["seconds"], line.strip()))
                    n += 1
        self.commit()
        return n

    def close(self):
        self.commit()
        self.db.close()
