"""Tests for statparse.PlayerState. Run: .venv\\Scripts\\python.exe test_statparse.py"""
import json
import os
import struct
import sys

import exptable
from statparse import PlayerState

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "tests", "kills", "stats_capture.jsonl")
HP_POT, MP_POT = 2010003, 2000003


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return 0 if cond else 1


def stat(mask, *vals, reaction=0):
    body = b"".join(struct.pack("<I" if f in (0x400, 0x800, 0x1000, 0x2000) else "<Q", v) for f, v in vals)
    return struct.pack("<HBBBI", 0x7E, reaction, 0, 1, mask) + body + b"\0\0"


def inv(*ops):
    return struct.pack("<HBBBI", 0x72, 0, 0, len(ops), 0) + b"".join(ops)


def main():
    fails = 0

    # 1. Real capture: two channel changes, kills, pickups, auto-potting. Ground truth
    #    from a screenshot of the HUD at the end: Lv 45, EXP 216,944 (46.31 %), MP 631/1208.
    s = PlayerState(exptable.to_next)
    events = []
    for line in open(FIXTURE):
        events += s.feed(bytes.fromhex(json.loads(line)["hex"]))
    fails += check("char info: name/level/job", (s.name, s.level, s.job) == ("Test", 45, 420), (s.name, s.level, s.job))
    fails += check("final EXP = HUD 216,944", s.exp == 216944, s.exp)
    fails += check("EXP % = HUD 46.31 (table lv45 468,450)", round(s.exp / 468450 * 100, 2) == 46.31)
    fails += check("MP = HUD 631/1208", (s.mp, s.max_mp) == (631, 1208), (s.mp, s.max_mp))
    fails += check("max HP = HUD 1464", s.max_hp == 1464, s.max_hp)
    exp_gain = sum(e[2] - e[1] for e in events if e[0] == "exp")
    fails += check("EXP gained 215,649 -> 216,944", exp_gain == 216944 - 215649, exp_gain)
    meso = [e for e in events if e[0] == "meso"]
    fails += check("mesos: 11 pickups, 394,017 -> 394,827", len(meso) == 11 and meso[-1][2] == 394827, len(meso))
    used = {}
    for e in events:
        if e[0] == "use" and e[3] < e[2]:
            used[e[1]] = used.get(e[1], 0) + e[2] - e[3]
    fails += check("potions used: 6 HP (2010003, slot 78->72), 2 MP (2000003)", used == {HP_POT: 6, MP_POT: 2}, used)
    fails += check("Use slot 29 = HP pot x72, slot 9 = MP pot x81",
                   s.use.get(29) == [HP_POT, 72] and s.use.get(9) == [MP_POT, 81], (s.use.get(29), s.use.get(9)))

    kinds = [e for e in events if e[0] == "pot_kind"]
    fails += check("potion kinds learned from the heal after use",
                   kinds == [("pot_kind", HP_POT, "hp", 292), ("pot_kind", MP_POT, "mp", 83)], kinds)
    fails += check("no level-up inferred from normal kills", not [e for e in events if e[0] == "level"])

    # 2. Inventory ops: move keeps totals; removing the last potion of a stack counts as use.
    s.feed(inv(struct.pack("<BBHH", 2, 2, 9, 40)))  # move MP pots slot 9 -> 40
    fails += check("move keeps item", s.use.get(40) == [MP_POT, 81] and 9 not in s.use)
    s.feed(inv(struct.pack("<BBHH", 1, 2, 40, 1)))
    ev = s.feed(inv(struct.pack("<BBH", 3, 2, 40)))
    fails += check("last potion removed = used", ev == [("use", MP_POT, 1, 0)], ev)

    # 3. Stat packet with an unknown flag before EXP is not misread.
    before = s.exp
    ev = s.feed(stat(0x10 | 0x10000, (0x10000, 999)))
    fails += check("unknown flag before EXP: ignored", s.exp == before and ev == [], (s.exp, ev))
    ev = s.feed(stat(0x400 | 0x10000, (0x400, 500), (0x10000, before + 10)))
    fails += check("HP + EXP in one packet", s.hp == 500 and ev == [("exp", before, before + 10, 45)], ev)

    # 4. Level-up: EXP drops by most of the bar -> next level (until char info confirms).
    s.exp = 468000
    ev = s.feed(stat(0x10000, (0x10000, 120)))
    fails += check("level-up inferred 45 -> 46", s.level == 46 and ev[0] == ("level", 45, 46), ev)
    s.exp = 120
    ev = s.feed(stat(0x10000, (0x10000, 100)))  # death penalty: small drop, same level
    fails += check("small EXP drop keeps the level", s.level == 46, ev)

    # 5. An item with a known kind doesn't emit pot_kind again; unknown use with no
    #    heal (e.g. a return scroll) stays unclassified.
    s.use[50] = [2030000, 3]
    s.feed(inv(struct.pack("<BBHH", 1, 2, 50, 2)))
    ev = s.feed(stat(0x400, (0x400, 600), reaction=0))
    fails += check("no heal -> not a potion", 2030000 not in s.item_kind, s.item_kind)

    print("ALL OK" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
