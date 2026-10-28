"""Map census: how many monsters are alive on the map, and how many players are on it.

Settles the map-capacity questions (floors-only vs walls, the player bonus, timed spawns,
the refill timer) from the server's own packets, with no counting by hand. It only reads
packets, like the rest of the tracker.

Opcodes (MapleStory Classic, see killparse.py / statparse.py):

  0x1A9 set field          short form (b[32] == 0, map change): u32 map id at 36.
                           full form (login / channel change, char data): the map id is
                           the u32 right after the first ff ff ff ff past our name.
  0x3C9 mob spawn          (oid, mob type) as in killparse._mob_type. On entering a map the
                           server sends one for every monster already there.
                           The spawn position (s16 x, s16 y, client coordinates) sits 203
                           bytes after the oid on every spawn seen so far (313/317/321 bytes).
  0x3DC mob move          u32 oid, ..., s16 x, s16 y at 30 = where the move starts (matches the
                           spawn position on a mob's first move, 182 of 182 on a capture)
  0x3D4 mob removed        u32 oid  (every death or removal on the map)
  0x22D user enter field   another player on the map: ... u32 char id, u32 0, u32 level,
                           u16 name length, name. Sent for everyone present when we arrive.
  0x22E user leave field   u32 char id

Our own character is never sent as 0x22D, so players on the map = others + 1.
"""
import json
import struct
from collections import Counter

from killparse import _mob_type
from statparse import find_char

OP_SET_FIELD = 0x1A9
OP_SPAWN = 0x3C9
OP_REMOVE = 0x3D4
OP_MOVE = 0x3DC
OP_USER_ENTER = 0x22D
OP_USER_LEAVE = 0x22E

BURST_GAP = 1.0     # spawns closer than this (s) belong to one refill
ARRIVAL = 2.0       # spawns within this long of entering a map are the monsters already there


def _u32(b, i):
    return struct.unpack_from("<I", b, i)[0]


def field_map_id(b):
    """Map id from a 0x1A9 set-field packet, else None."""
    if len(b) >= 40 and b[32] == 0:
        return _u32(b, 36)
    n = find_char(b)
    if n is None:
        return None
    i = b.find(b"\xff\xff\xff\xff", n)
    return _u32(b, i + 4) if 0 <= i <= len(b) - 8 else None


POS_AFTER_OID = 203


def spawn_pos(b):
    """(x, y) where a 0x3C9 spawn puts the monster, else None."""
    o = 7 if len(b) >= 16 and b[7] != 1 and b[11] == 1 else 3
    i = o + POS_AFTER_OID
    if len(b) < i + 4:
        return None
    x, y = struct.unpack_from("<hh", b, i)
    return (x, y) if -10000 < x < 20000 and -10000 < y < 20000 else None


def user_enter(b):
    """(char id, level, name) from a 0x22D user-enter packet, else None."""
    for n in range(14, min(len(b) - 3, 40)):
        ln = b[n] | b[n + 1] << 8
        name = b[n + 2:n + 2 + ln]
        if 2 <= ln <= 12 and len(name) == ln and all(48 <= c < 123 for c in name):
            lvl = _u32(b, n - 4)
            if 1 <= lvl <= 250 and _u32(b, n - 8) == 0:
                return _u32(b, n - 12), lvl, name.decode()
    return None


class Visit:
    """One stay on one map."""

    def __init__(self, map_id, ts, resumed=False):
        self.map_id, self.start, self.end = map_id, ts, ts
        # resumed: started after the capture lost data, so the monsters already on the map are
        # unknown; counts from it are not used (they would mix ghosts of missed deaths in)
        self.resumed = resumed
        self.mobs = {}                 # oid -> mob type, alive now
        self.others = {}               # char id -> (level, name)
        self.peak = 0
        self.peak_by_players = {}      # players on map -> highest alive count seen with them
        self.peak_types = Counter()    # mob types alive at the overall peak
        self.player_seconds = Counter()
        self.alive_seconds = Counter()
        self.bursts = []               # (ts, spawns, alive after) for refills after arrival
        self.pos = {}                  # oid -> spawn position, for monsters that spawned after we arrived
        self.where = {}                # oid -> latest known position (spawn, then each move)
        self.snaps = []                # per refill: where it spawned, and every spawn position then occupied
        self.kills = 0
        self._last_ts = ts
        self._burst = None

    @property
    def players(self):
        return len(self.others) + 1

    def _tick(self, ts):
        dt = max(0.0, ts - self._last_ts)
        self.player_seconds[self.players] += dt
        self.alive_seconds[len(self.mobs)] += dt
        self._last_ts = self.end = ts

    def _note(self):
        if self.resumed:
            return
        n = len(self.mobs)
        if n > self.peak:
            self.peak, self.peak_types = n, Counter(self.mobs.values())
        p = self.players
        self.peak_by_players[p] = max(self.peak_by_players.get(p, 0), n)

    def spawn(self, ts, oid, mob, pos=None):
        self._tick(ts)
        if oid in self.mobs:
            return
        self.mobs[oid] = mob
        self._note()
        if ts - self.start < ARRIVAL:
            return
        if pos:
            self.pos[oid] = pos
            self.where[oid] = pos
        if self._burst and ts - self._burst[0] <= BURST_GAP:
            self._burst[1] += 1
            self._burst[2] = len(self.mobs)
            snap = self.snaps[-1]
        else:
            self._burst = [ts, 1, len(self.mobs)]
            self.bursts.append(self._burst)
            snap = {"t": round(ts - self.start, 2), "spawned": []}
            self.snaps.append(snap)
        if pos:
            snap["spawned"].append(list(pos))
        snap["alive"] = len(self.mobs)
        snap["players"] = self.players
        snap["occupied"] = sorted(list(self.pos[o]) for o in self.mobs if o in self.pos)
        snap["unknown"] = sum(1 for o in self.mobs if o not in self.pos)
        snap["positions"] = sorted(list(self.where[o]) for o in self.mobs if o in self.where)

    def remove(self, ts, oid):
        self._tick(ts)
        self.pos.pop(oid, None)
        self.where.pop(oid, None)
        if self.mobs.pop(oid, None) is not None:
            self.kills += 1

    def move(self, oid, pos):
        if oid in self.mobs:
            self.where[oid] = pos

    def enter(self, ts, cid, level, name):
        self._tick(ts)
        self.others[cid] = (level, name)

    def leave(self, ts, cid):
        self._tick(ts)
        self.others.pop(cid, None)

    def summary(self):
        gaps = [round(b[0] - a[0], 2) for a, b in zip(self.bursts, self.bursts[1:])]
        full = Counter(b[2] for b in self.bursts)
        return {
            "map": self.map_id, "start": round(self.start, 3), "seconds": round(self.end - self.start, 1),
            "resumed": self.resumed,
            "peak_alive": self.peak, "peak_types": dict(self.peak_types),
            "peak_alive_by_players": {str(k): v for k, v in sorted(self.peak_by_players.items())},
            "players_seconds": {str(k): round(v, 1) for k, v in sorted(self.player_seconds.items()) if v >= 0.05},
            "alive_after_refill": {str(k): v for k, v in sorted(full.items())},
            "refills": len(self.bursts), "refill_gaps": gaps, "removed": self.kills,
            "refill_snapshots": self.snaps,
        }


class MapCensus:
    """Feed decoded server packets; yields a summary dict each time a map visit ends."""

    def __init__(self):
        self.visit = None

    def feed(self, ts, p):
        if len(p) < 2:
            return None
        op = p[0] | p[1] << 8
        if op == OP_SET_FIELD:
            mid = field_map_id(p)
            if mid is None:
                return None
            done = self.finish()
            self.visit = Visit(mid, ts)
            return done
        v = self.visit
        if v is None:
            return None
        if op == OP_SPAWN:
            r = _mob_type(p)
            if r:
                v.spawn(ts, r[0], r[1], spawn_pos(p))
        elif op == OP_REMOVE and len(p) >= 6:
            v.remove(ts, _u32(p, 2))
        elif op == OP_MOVE and len(p) >= 34:
            v.move(_u32(p, 2), struct.unpack_from("<hh", p, 30))
        elif op == OP_USER_ENTER:
            u = user_enter(p)
            if u:
                v.enter(ts, *u)
        elif op == OP_USER_LEAVE and len(p) >= 6:
            v.leave(ts, _u32(p, 2))
        return None

    def lost_data(self, ts):
        """The capture dropped packets (decoder resync): deaths may have been missed, so the alive
        count can't be trusted any more. Close the visit; carry on in a 'resumed' one until the
        next map change gives a clean start. Returns the closed visit's summary."""
        v = self.visit
        if v is None:
            return None
        s = self.finish()
        self.visit = Visit(v.map_id, ts, resumed=True)
        return s

    def finish(self):
        """Close the current visit; its summary (None if there was none)."""
        v, self.visit = self.visit, None
        return v.summary() if v else None


def format_summary(s, names=None):
    name = (names or {}).get(s["map"], "")
    by = ", ".join(f"{p} player{'s' if p != '1' else ''}: {n}" for p, n in s["peak_alive_by_players"].items())
    gaps = sorted(g for g in s["refill_gaps"] if g < 30)
    med = gaps[len(gaps) // 2] if gaps else None
    return (f"LEFT map {s['map']} {name} | {s['seconds']:.0f} s | highest alive {s['peak_alive']} ({by})"
            f" | alive after refills {s['alive_after_refill']} | {s['refills']} refills"
            + (f", median gap {med} s" if med else ""))


class LiveLines:
    """Progress lines while on a map: arrival, then one line per finished refill."""

    def __init__(self, names=None):
        self.names = names or {}
        self.visit, self.arrived, self.done, self.kills = None, False, 0, 0

    def __call__(self, census, ts):
        v, out = census.visit, []
        if v is not self.visit:
            self.visit, self.arrived, self.done, self.kills = v, False, 0, 0
            if v:
                out.append(f"ENTERED map {v.map_id} {self.names.get(v.map_id, '')}")
        if v is None:
            return out
        if not self.arrived and ts - v.start >= ARRIVAL:
            self.arrived = True
            out.append(f"  already there: {len(v.mobs)} alive | {v.players} player(s) on the map")
        while self.done < len(v.bursts) and (self.done < len(v.bursts) - 1 or ts - v.bursts[self.done][0] > BURST_GAP):
            b = v.bursts[self.done]
            self.done += 1
            out.append(f"  {b[0] - v.start:6.1f} s  refill +{b[1]} -> {b[2]} alive | {v.kills - self.kills} died since"
                       f" last refill | {v.players} player(s) | highest so far {v.peak}")
            self.kills = v.kills
        return out


def main():
    import argparse
    import os
    import time
    import maplepackets as mp
    from censusdb import CensusStore
    ap = argparse.ArgumentParser(description="Log monsters alive and players present per map visit.")
    ap.add_argument("--pcap", help="read a saved capture instead of capturing live")
    ap.add_argument("--iface", help="network adapter (default: found from the game's connection)")
    ap.add_argument("--db", default="census.db", help="SQLite file the census adds to (see censusdb.py)")
    ap.add_argument("--out", default="", help="also append one JSON line per map visit to this file")
    ap.add_argument("--names", help="OSMS maps.json, to print map names")
    ap.add_argument("--raw", default="", help="also save the packets the census reads (large; for debugging)")
    ap.add_argument("--import-jsonl", help="add the visits from an older census.jsonl to the database and exit")
    ap.add_argument("--quiet", action="store_true", help="only print map arrivals and departures")
    a = ap.parse_args()
    store = CensusStore(a.db)
    if a.import_jsonl:
        print(f"imported {store.import_jsonl(a.import_jsonl)} visits into {a.db}")
        return
    names = {}
    if a.names and os.path.exists(a.names):
        with open(a.names, encoding="utf-8") as f:
            names = {m["id"]: m["name"] for r in json.load(f)["regions"] for m in r["maps"]}
    c = MapCensus()
    live = LiveLines(names)

    def emit(s):
        if not s:
            return
        if a.out:
            with open(a.out, "a", encoding="utf-8") as f:
                f.write(json.dumps(s) + "\n")
        print(format_summary(s, names), flush=True)
    keep = {OP_SET_FIELD, OP_SPAWN, OP_REMOVE, OP_MOVE, OP_USER_ENTER, OP_USER_LEAVE}
    raw = open(a.raw, "a", encoding="utf-8") if a.raw else None

    lost = []

    def on_log(msg):
        if "resync" in msg:
            lost.append(msg)

    def run(lines):
        for ts, _k, p in mp.decode_lines(lines, log=on_log):
            if lost:
                lost.clear()
                s = c.lost_data(ts)
                store.finish(s) if s else None
                emit(s)
                print("  (capture lost data: counts paused until the next map change)", flush=True)
            if raw and mp.opcode(p) in keep:
                raw.write(json.dumps({"ts": ts, "hex": p.hex()}) + "\n")
                raw.flush()
            s = c.feed(ts, p)
            store.observe(c, ts, p, s)
            emit(s)
            for line in live(c, ts):
                if not a.quiet or not line.startswith("  "):
                    print(line, flush=True)

    try:
        if a.pcap:
            import subprocess
            run(subprocess.run(mp.tshark_cmd(pcap=a.pcap), capture_output=True, text=True).stdout.splitlines())
        else:
            # Runs until Ctrl+C: waits for the game, and restarts the capture if it stops.
            from packetmain import find_game_iface
            print(f"census: writing to {os.path.abspath(a.db)}; Ctrl+C to stop", flush=True)
            waiting = None
            while True:
                iface = a.iface
                if not iface:
                    iface, why = find_game_iface()
                    if not iface:
                        if why != waiting:
                            print(f"waiting: {why} (retrying every 30 s)", flush=True)
                            waiting = why
                        time.sleep(30)
                        continue
                waiting = None
                print(f"capturing on {iface}; change map or channel once so the census sees you arrive", flush=True)
                proc = mp.open_tshark(iface=iface)
                run(proc.stdout)
                s = c.finish()   # the capture stopped: close the visit we were in
                store.finish(s) if s else None
                emit(s)
                print("capture stopped; restarting in 10 s", flush=True)
                time.sleep(10)
    except KeyboardInterrupt:
        pass
    s = c.finish()
    if s:
        store.finish(s)
    emit(s)
    store.close()


if __name__ == "__main__":
    main()
