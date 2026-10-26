"""Tests for the packet tracker's EXP logic. Run: .venv\\Scripts\\python.exe test_packetexp.py"""
import sys

from packetmain import PacketExpTracker


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return 0 if cond else 1


def main():
    fails = 0
    LV45, LV46 = 468450, 510420

    # 1. Level unknown: EXP still counts from the first packet; no % / max.
    t = PacketExpTracker()
    t.session_start = 0
    for i, exp in enumerate((280329, 280349, 280409, 280484)):
        t.feed(i * 10, exp, None)
    fails += check("gains without level", t.session_gained == 155, t.session_gained)
    fails += check("no % / max without level", t.percent is None and t.estimated_max is None)
    fails += check("rate without level", t.session_rate(60) is not None, t.session_rate(60))

    # 2. Level arrives: % and max show, gains carry on.
    t.full = LV45
    t.feed(40, 280544, int(280544 * 10000 / LV45) / 100)
    fails += check("level known later: max + %", t.estimated_max == LV45 and t.percent == 59.88, t.percent)
    fails += check("gains carry on", t.session_gained == 215, t.session_gained)

    # 3. Death penalty: small drop, no gain, not a level-up.
    t.feed(50, 270000, None)
    fails += check("death: no gain, no level", t.session_gained == 215 and t.levels == 0)

    # 4. Level-up: rest of the old bar + EXP on the new level.
    t.feed(60, 468000, None)
    gained = t.session_gained
    t.full = LV46
    msg = t.feed(70, 100, None)
    fails += check("level-up gain = 450 + 100", t.session_gained - gained == 550 and t.levels == 1,
                   (t.session_gained - gained, msg))

    print("ALL OK" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
