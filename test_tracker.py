"""Tests for RateTracker. Run: .venv\\Scripts\\python.exe test_tracker.py"""
import sys

from main import RateTracker

MAX = 28980  # level 22 EXP requirement seen in game


def pct(exp, mx=MAX):
    return int(exp / mx * 10000) / 100  # game truncates to 2 decimals


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    return 0 if cond else 1


def main():
    fails = 0

    # 1. Steady grinding: rate and ETA are right.
    t = RateTracker()
    for i in range(60):  # 26 EXP every 5s for 5 min
        exp = 20000 + 26 * i
        t.feed(i * 5, exp, pct(exp))
    fails += check("steady rate ~312/min", t.rate and abs(t.rate - 312) < 5, t.rate)
    fails += check("max learned ~28980", abs(t.estimated_max - MAX) < 30, t.estimated_max)

    # 2. The reported bug: a '7' misread as '3' (22,726 -> 22,326) must be
    #    ignored instead of dropping the rate / blowing up time to level.
    t = RateTracker()
    now = 0
    for exp in range(22000, 22700, 26):
        t.feed(now, exp, pct(exp)); now += 5
    rate_before, eta_before = t.rate, t.eta_minutes
    for _ in range(10):  # 5s of the misread, % read correctly
        t.feed(now, 22326, pct(22726)); now += 0.5
    fails += check("misread digit ignored", t.current >= 22674, t.current)
    fails += check("rate unchanged by misread", abs(t.rate - rate_before) < 1e-6,
                   f"{rate_before} -> {t.rate}")
    fails += check("ETA unchanged by misread", abs(t.eta_minutes - eta_before) < 1,
                   f"{eta_before} -> {t.eta_minutes}")

    # 3. Misread in the percent instead.
    t.feed(now, 22726, 38.42); now += 0.5
    fails += check("misread percent ignored", t.percent != 38.42, t.percent)

    # 4. Level-up is still detected and counted.
    t = RateTracker()
    now = 0
    for exp in range(28000, 28980, 98):
        t.feed(now, exp, pct(exp)); now += 5
    gained_before = t.session_gained
    new_max = 32000
    for _ in range(5):
        t.feed(now, 50, pct(50, new_max)); now += 0.5
    fails += check("level-up counted", t.levels == 1, t.levels)
    fails += check("level-up gain counted",
                   abs(t.session_gained - ((28882 - 28000) + (MAX - 28882) + 50)) <= 5,
                   f"{gained_before} -> {t.session_gained}")
    for exp in range(150, 3000, 100):
        t.feed(now, exp, pct(exp, new_max)); now += 5
    fails += check("new level max learned", abs(t.estimated_max - new_max) < 200,
                   t.estimated_max)

    # 5. Death EXP loss (same level) is accepted and the rate restarts.
    t = RateTracker()
    now = 0
    for exp in range(20000, 21000, 100):
        t.feed(now, exp, pct(exp)); now += 5
    t.feed(now, 19000, pct(19000)); now += 0.5
    fails += check("death loss accepted", t.current == 19000, t.current)
    fails += check("death doesn't count as level", t.levels == 0, t.levels)

    print("ALL PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
