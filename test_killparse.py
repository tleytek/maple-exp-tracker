"""Tests for killparse.KillCounter. Run: .venv\\Scripts\\python.exe test_killparse.py"""
import collections
import json
import os
import struct
import sys

from killparse import KillCounter

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "tests", "kills", "kills30_capture.jsonl")
ME, OTHER = 0x8D9D, 0x16DA5


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return 0 if cond else 1


def pkt(op, *parts):
    return struct.pack("<H", op) + b"".join(parts)


def u32(v):
    return struct.pack("<I", v)


def main():
    fails = 0

    # 1. Real capture: 120 s on a busy map, 30 own kills among 156 deaths on the map.
    #    (own kills cross-checked against 30 HP-bar-0 packets and 30 EXP updates)
    k = KillCounter()
    kills = []
    for line in open(FIXTURE):
        r = json.loads(line)
        e = k.feed(r["ts"], bytes.fromhex(r["hex"]))
        if e and e["own"]:
            kills.append(e)
    fails += check("capture: 30 own kills", len(kills) == 30, len(kills))
    fails += check("capture: learned our character id", k.self_id == ME, k.self_id)
    by_mob = collections.Counter(e["mob"] for e in kills)
    fails += check("capture: per-mob 30/20/35 = 16/11/3", by_mob == {30: 16, 20: 11, 35: 3}, dict(by_mob))

    # 1b. Same capture, every other death on the map: 126 kills by 10 other players.
    k = KillCounter()
    others = collections.Counter()
    for line in open(FIXTURE):
        r = json.loads(line)
        e = k.feed(r["ts"], bytes.fromhex(r["hex"]))
        if e and not e["own"]:
            others[e["killer"]] += 1
    fails += check("capture: 126 kills by 10 other players", sum(others.values()) == 126
                   and len(others) == 10 and ME not in others, dict(others))
    fails += check("capture: top other killer 0x2bb07 with 21", others.most_common(1) == [(0x2BB07, 21)],
                   others.most_common(1))

    # 2. Other players' kills are reported as not ours, even after our id is known.
    k = KillCounter()
    k.feed(0, pkt(0x3C9, b"\0", u32(1), b"\1", u32(30)))
    k.feed(1, pkt(0x3F3, u32(1), b"\0\0\0\0\0"))
    e = k.feed(2, pkt(0x44D, u32(1), u32(ME), u32(420)))
    fails += check("own kill counted", e is not None and e["own"] and e["killer"] == ME, e)
    k.feed(3, pkt(0x3C9, b"\0", u32(2), b"\1", u32(30)))
    e = k.feed(4, pkt(0x44D, u32(2), u32(OTHER), u32(1)))
    fails += check("other player's kill not ours", e is not None and not e["own"]
                   and e["killer"] == OTHER and e["mob"] == 30, e)

    # 3. Once our id is known, a kill without an HP-bar packet still counts; duplicates don't.
    e = k.feed(5, pkt(0x44D, u32(3), u32(ME), u32(420)))
    fails += check("kill without HP bar counted (type unknown)", e is not None and e["own"] and e["mob"] is None, e)
    fails += check("duplicate 0x44D ignored", k.feed(6, pkt(0x44D, u32(3), u32(ME), u32(420))) is None)

    # 4. Before our id is known, a kill without an HP-bar-0 can't be ours by any sign seen.
    k = KillCounter()
    e = k.feed(0, pkt(0x44D, u32(9), u32(ME), u32(1)))
    fails += check("unknown id, no HP bar: not own", e is not None and not e["own"], e)

    # 5. Spawn variant with 4 extra bytes before the oid still yields the mob type.
    k = KillCounter()
    k.feed(0, pkt(0x3C9, b"\0", u32(0), u32(7), b"\1", u32(20)))
    k.feed(1, pkt(0x3F3, u32(7), b"\0\0\0\0\0"))
    e = k.feed(2, pkt(0x44D, u32(7), u32(ME), u32(1)))
    fails += check("spawn variant mob type", e and e["mob"] == 20, e)

    # 6. Kill session stats (same model as EXP): session average excludes paused time,
    #    kills while paused aren't counted, rolling window counts the last 60 s.
    from packetmain import KillTracker
    k = KillTracker()
    k.session_start = 0
    for t in range(0, 120, 10):  # 12 kills in 2 min
        k.add(t, 30)
    fails += check("6 kills/min", abs(k.session_rate(120) - 6) < 1e-9, k.session_rate(120))
    k.pause(120)
    fails += check("kill while paused ignored", not k.add(150, 30) and k.current == 12)
    k.resume(180)
    fails += check("paused minute not session time", k.session_seconds(240) == 180, k.session_seconds(240))
    fails += check("rolling 60 s (kills at 80-110)", k.gained_in(130, 60)[0] == 4, k.gained_in(130, 60))
    fails += check("per-mob count", k.by_mob == {30: 12}, dict(k.by_mob))

    # 7. Other players: kills/min over their time on the map with us.
    from packetmain import OtherPlayers
    o = OtherPlayers(now=0)
    o.enter(0, OTHER, 41, "Kendikos")
    for t in range(5, 120, 10):  # 12 kills in 2 min
        o.kill(t, OTHER)
    fails += check("other: 6 kills/min", abs(o.rate(OTHER, 120) - 6) < 1e-9, o.rate(OTHER, 120))
    fails += check("other: last minute", o.last_minute(OTHER, 120) == 6, o.last_minute(OTHER, 120))
    fails += check("other: name and level", o.rows(120)[0][1] == "Kendikos (41)", o.rows(120))
    o.leave(120, OTHER)  # time stops while they're gone
    fails += check("other: rate frozen after leaving", abs(o.rate(OTHER, 600) - 6) < 1e-9, o.rate(OTHER, 600))
    fails += check("other: marked as left", o.rows(600)[0][5] is False, o.rows(600))
    o.enter(600, OTHER, 41, "Kendikos")
    o.pause(630)
    fails += check("other: kill while paused ignored", not o.kill(640, OTHER) and o.kills[OTHER] == 12)
    o.resume(700)
    fails += check("other: paused time not counted", o.seconds_with_us(OTHER, 730) == 180,
                   o.seconds_with_us(OTHER, 730))

    # 8. A killer we never saw enter was on the map since we started watching (or since
    #    our last map change); a map change ends everyone's time with us.
    o = OtherPlayers(now=0)
    o.kill(50, 0x111)
    fails += check("unseen player: counted from the start", o.seconds_with_us(0x111, 60) == 60
                   and o.name(0x111) == "#273", (o.seconds_with_us(0x111, 60), o.name(0x111)))
    o.field(60)
    o.kill(90, 0x222)
    fails += check("map change: old map's players stop", o.seconds_with_us(0x111, 200) == 60
                   and o.seconds_with_us(0x222, 100) == 40,
                   (o.seconds_with_us(0x111, 200), o.seconds_with_us(0x222, 100)))
    o.forget(0x222)  # turned out to be us
    fails += check("forget: own id dropped", [r[0] for r in o.rows(100)] == [0x111], o.rows(100))
    o.reset_session(100)
    fails += check("reset: no kills, old time gone", not o.rows(100) and 0x111 not in o.present
                   and o.seconds_with_us(0x111, 110) == 0, o.present)

    print("ALL OK" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
