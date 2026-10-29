"""Tests for the spawn-cycle countdown. Run: .venv\\Scripts\\python.exe test_spawncycle.py"""
import json
import os
import statistics
import sys

from packetmain import CYCLE, Capture, SpawnCycle

HERE = os.path.dirname(os.path.abspath(__file__))


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return 0 if cond else 1


def close(a, b):
    return a is not None and abs(a - b) < 1e-6


def main():
    fails = 0

    # 1. Arrival spawns are the monsters already there, not a refill.
    s = SpawnCycle()
    fails += check("no countdown before any refill", s.next_in(5) is None)
    s.field(0)
    for oid in range(1, 6):
        s.spawn(0.5, oid)
    fails += check("arrival is not a refill", s.next_in(5) is None and s.last is None, s.last)
    fails += check("alive counts arrival monsters", s.alive_count == 5, s.alive_count)

    # 2. A refill burst (spawns within a second) starts the countdown.
    s.remove(8, 1); s.remove(8.5, 2); s.remove(9, 3)
    s.spawn(10, 11); s.spawn(10.3, 12)
    fails += check("burst: +2 -> 4 alive", s.last == (2, 4), s.last)
    fails += check("countdown from the burst's first spawn", close(s.next_in(12), CYCLE - 2),
                   s.next_in(12))
    s.spawn(10 + CYCLE, 13)
    fails += check("next burst resets it", close(s.next_in(10 + CYCLE + 0.5), CYCLE - 0.5)
                   and s.last == (1, 5), (s.next_in(10 + CYCLE + 0.5), s.last))

    # 3. A cycle with nothing to spawn: the timer keeps running in CYCLE steps.
    t0 = 10 + CYCLE
    fails += check("skipped cycle keeps the beat", close(s.next_in(t0 + CYCLE + 1), CYCLE - 1),
                   s.next_in(t0 + CYCLE + 1))

    # 4. Map change: old map's monsters and timer are gone.
    s.field(30)
    fails += check("map change clears", s.next_in(31) is None and s.last is None
                   and s.alive_count == 0, (s.next_in(31), s.last, s.alive_count))

    # 5. Tracker started mid-map: refills still time the cycle, but the alive count
    #    is unknown (monsters already there were never announced).
    s = SpawnCycle()
    s.spawn(3, 1); s.spawn(3.2, 2)
    fails += check("mid-map: refill seen, alive unknown", s.last == (2, None), s.last)

    # 6. Real capture (The Burnt Land): refills every ~7.1 s.
    events = []

    class Q:
        def put(self, ev):
            events.append(ev)

    cap = Capture.__new__(Capture)  # no capture thread: feed saved packets to handle()
    from exptable import to_next
    from killparse import KillCounter
    from statparse import PlayerState
    cap.out, cap.characters = Q(), {}
    cap.state, cap.kills = PlayerState(to_next, {}), KillCounter()
    with open(os.path.join(HERE, "tests", "census", "burnt_land_capture.jsonl")) as f:
        for line in f:
            d = json.loads(line)
            cap.handle(d["ts"], bytes.fromhex(d["hex"]))
    s = SpawnCycle()
    bursts = []
    for kind, val in events:
        if kind == "field":
            s.field(val)
        elif kind == "mob_spawn":
            before = s.last_ts
            s.spawn(*val)
            if s.last_ts != before:
                bursts.append(s.last_ts)
        elif kind == "mob_gone":
            s.remove(*val)
    gaps = [b - a for a, b in zip(bursts, bursts[1:])]
    fails += check("capture: several refills", len(bursts) >= 5, len(bursts))
    fails += check("capture: median gap ~7.1 s", gaps and abs(statistics.median(gaps) - CYCLE) < 0.2,
                   gaps and round(statistics.median(gaps), 2))
    fails += check("capture: alive count known after arrival", s.alive_count > 0, s.alive_count)

    print("ALL PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
