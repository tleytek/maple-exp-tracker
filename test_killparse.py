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
        if e:
            kills.append(e)
    fails += check("capture: 30 own kills", len(kills) == 30, len(kills))
    fails += check("capture: learned our character id", k.self_id == ME, k.self_id)
    by_mob = collections.Counter(e["mob"] for e in kills)
    fails += check("capture: per-mob 30/20/35 = 16/11/3", by_mob == {30: 16, 20: 11, 35: 3}, dict(by_mob))

    # 2. Other players' kills are never counted, even after our id is known.
    k = KillCounter()
    k.feed(0, pkt(0x3C9, b"\0", u32(1), b"\1", u32(30)))
    k.feed(1, pkt(0x3F3, u32(1), b"\0\0\0\0\0"))
    fails += check("own kill counted", k.feed(2, pkt(0x44D, u32(1), u32(ME), u32(420))) is not None)
    k.feed(3, pkt(0x3C9, b"\0", u32(2), b"\1", u32(30)))
    fails += check("other player's kill ignored", k.feed(4, pkt(0x44D, u32(2), u32(OTHER), u32(1))) is None)

    # 3. Once our id is known, a kill without an HP-bar packet still counts; duplicates don't.
    e = k.feed(5, pkt(0x44D, u32(3), u32(ME), u32(420)))
    fails += check("kill without HP bar counted (type unknown)", e is not None and e["mob"] is None, e)
    fails += check("duplicate 0x44D ignored", k.feed(6, pkt(0x44D, u32(3), u32(ME), u32(420))) is None)

    # 4. Before our id is known, nothing without an HP-bar-0 is counted (can't tell whose).
    k = KillCounter()
    fails += check("unknown id, no HP bar: not counted", k.feed(0, pkt(0x44D, u32(9), u32(ME), u32(1))) is None)

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

    print("ALL OK" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
