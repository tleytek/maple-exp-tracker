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

    def __init__(self, map_id, ts):
        self.map_id, self.start, self.end = map_id, ts, ts
        self.mobs = {}                 # oid -> mob type, alive now
        self.others = {}               # char id -> (level, name)
        self.peak = 0
        self.peak_by_players = {}      # players on map -> highest alive count seen with them
        self.peak_types = Counter()    # mob types alive at the overall peak
        self.player_seconds = Counter()
        self.alive_seconds = Counter()
        self.bursts = []               # (ts, spawns, alive after) for refills after arrival
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
        n = len(self.mobs)
        if n > self.peak:
            self.peak, self.peak_types = n, Counter(self.mobs.values())
        p = self.players
        self.peak_by_players[p] = max(self.peak_by_players.get(p, 0), n)

    def spawn(self, ts, oid, mob):
        self._tick(ts)
        if oid in self.mobs:
            return
        self.mobs[oid] = mob
        self._note()
        if ts - self.start < ARRIVAL:
            return
        if self._burst and ts - self._burst[0] <= BURST_GAP:
            self._burst[1] += 1
            self._burst[2] = len(self.mobs)
        else:
            self._burst = [ts, 1, len(self.mobs)]
            self.bursts.append(self._burst)

    def remove(self, ts, oid):
        self._tick(ts)
        if self.mobs.pop(oid, None) is not None:
            self.kills += 1

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
            "peak_alive": self.peak, "peak_types": dict(self.peak_types),
            "peak_alive_by_players": {str(k): v for k, v in sorted(self.peak_by_players.items())},
            "players_seconds": {str(k): round(v, 1) for k, v in sorted(self.player_seconds.items()) if v >= 0.05},
            "alive_after_refill": {str(k): v for k, v in sorted(full.items())},
            "refills": len(self.bursts), "refill_gaps": gaps, "removed": self.kills,
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
                v.spawn(ts, r[0], r[1])
        elif op == OP_REMOVE and len(p) >= 6:
            v.remove(ts, _u32(p, 2))
        elif op == OP_USER_ENTER:
            u = user_enter(p)
            if u:
                v.enter(ts, *u)
        elif op == OP_USER_LEAVE and len(p) >= 6:
            v.leave(ts, _u32(p, 2))
        return None

    def finish(self):
        """Close the current visit; its summary (None if there was none)."""
        v, self.visit = self.visit, None
        return v.summary() if v else None


def format_summary(s, names=None):
    name = (names or {}).get(s["map"], "")
    by = ", ".join(f"{p} player{'s' if p != '1' else ''}: {n}" for p, n in s["peak_alive_by_players"].items())
    gaps = sorted(g for g in s["refill_gaps"] if g < 30)
    med = gaps[len(gaps) // 2] if gaps else None
    return (f"map {s['map']} {name} | {s['seconds']:.0f} s | highest alive {s['peak_alive']} ({by})"
            f" | alive after refills {s['alive_after_refill']} | {s['refills']} refills"
            + (f", median gap {med} s" if med else ""))


def main():
    import argparse
    import os
    import maplepackets as mp
    ap = argparse.ArgumentParser(description="Log monsters alive and players present per map visit.")
    ap.add_argument("--pcap", help="read a saved capture instead of capturing live")
    ap.add_argument("--iface", help="network adapter (default: found from the game's connection)")
    ap.add_argument("--out", default="census.jsonl", help="append one JSON line per map visit")
    ap.add_argument("--names", help="OSMS maps.json, to print map names")
    a = ap.parse_args()
    names = {}
    if a.names and os.path.exists(a.names):
        with open(a.names, encoding="utf-8") as f:
            names = {m["id"]: m["name"] for r in json.load(f)["regions"] for m in r["maps"]}
    if a.pcap:
        import subprocess
        lines = subprocess.run(mp.tshark_cmd(pcap=a.pcap), capture_output=True, text=True).stdout.splitlines()
    else:
        iface = a.iface
        if not iface:
            from packetmain import find_game_iface
            iface, why = find_game_iface()
            if not iface:
                raise SystemExit(f"no game connection: {why}")
        print(f"capturing on {iface}; Ctrl+C to stop")
        lines = mp.open_tshark(iface=iface).stdout
    c = MapCensus()

    def emit(s):
        if not s:
            return
        with open(a.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(s) + "\n")
        print(format_summary(s, names), flush=True)
    try:
        for ts, _k, p in mp.decode_lines(lines):
            emit(c.feed(ts, p))
    except KeyboardInterrupt:
        pass
    emit(c.finish())


if __name__ == "__main__":
    main()
