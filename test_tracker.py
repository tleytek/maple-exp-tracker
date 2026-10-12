"""Tests for RateTracker. Run: .venv\\Scripts\\python.exe test_tracker.py"""
import sys

from main import MesoTracker, RateTracker

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
    rate_before = t.rate
    ref = RateTracker()  # same stream, but the 5s read correctly
    ref_now = 0
    for exp in range(22000, 22700, 26):
        ref.feed(ref_now, exp, pct(exp)); ref_now += 5
    for _ in range(10):
        ref.feed(ref_now, 22676, pct(22676)); ref_now += 0.5
    for _ in range(10):  # 5s of the misread, % read correctly
        t.feed(now, 22326, pct(22726)); now += 0.5
    fails += check("misread digit ignored", t.current >= 22674, t.current)
    fails += check("rate unchanged by misread", abs(t.rate - rate_before) < 1e-6,
                   f"{rate_before} -> {t.rate}")
    fails += check("ETA unaffected by misread", abs(t.eta_minutes - ref.eta_minutes) < 0.01,
                   f"{ref.eta_minutes} vs {t.eta_minutes}")

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

    # 6. The reported problem: after a long fast session, a few slow minutes
    #    must show up in the recent rate even though the average barely moves.
    t = RateTracker()
    now, exp = 0, 1000
    big_max = 10_000_000
    while now < 3600:  # 1 hour at 600 EXP/min (50 EXP every 5s)
        t.feed(now, exp, pct(exp, big_max)); now += 5; exp += 50
    fails += check("recent rate ~600/min while fast",
                   abs(t.rolling_rate(now) - 600) < 15, t.rolling_rate(now))
    end_fast = now
    while now < end_fast + 300:  # 5 min at 60 EXP/min (5 EXP every 5s)
        t.feed(now, exp, pct(exp, big_max)); now += 5; exp += 5
    fails += check("recent rate drops to ~60/min", abs(t.rolling_rate(now) - 60) < 15,
                   t.rolling_rate(now))
    fails += check("average still high (old behaviour)", t.rate > 500, t.rate)

    # 7. Recent rate keeps decaying between gains (uses 'now', not last gain).
    t = RateTracker()
    for i in range(61):  # 5 min at 120/min
        exp = 20000 + 10 * i
        t.feed(i * 5, exp, pct(exp))
    r_now = t.rolling_rate(300)
    r_later = t.rolling_rate(300 + 50)  # 50s with no gain
    fails += check("recent rate decays with no gains", r_later < r_now * 0.9,
                   f"{r_now} -> {r_later}")

    # 8. Recent rate runs straight through a level-up.
    t = RateTracker()
    now = 0
    for exp in range(28000, 28980, 98):
        t.feed(now, exp, pct(exp)); now += 5
    for _ in range(5):
        t.feed(now, 50, pct(50, 32000)); now += 0.5
    fails += check("recent rate survives level-up",
                   t.rolling_rate(now) and t.rolling_rate(now) > 0, t.rolling_rate(now))

    # 9. No recent rate from a handful of seconds of history.
    t = RateTracker()
    t.feed(0, 20000, pct(20000)); t.feed(2, 20500, pct(20500))
    fails += check("no recent rate from 2s of data", t.rolling_rate(2) is None,
                   t.rolling_rate(2))

    # 10. Being idle doesn't reset tracking: both rates keep their history.
    t = RateTracker()
    for i in range(61):  # 5 min at 120/min
        exp = 20000 + 10 * i
        t.feed(i * 5, exp, pct(exp))
    for s in range(300, 480):  # 3 min standing still, readings keep coming
        t.feed(s, 20600, pct(20600))
    fails += check("idle keeps average", t.rate and abs(t.rate - 120) < 1, t.rate)
    fails += check("idle lowers recent rate, not reset",
                   t.rolling_rate(480) is not None and 30 < t.rolling_rate(480) < 60,
                   t.rolling_rate(480))
    t.feed(480, 20610, pct(20610))
    fails += check("average includes idle time after next gain",
                   abs(t.rate - 610 / 8) < 2, t.rate)

    # 11. Rolling totals for 1 / 5 / 60 minutes.
    t = RateTracker()
    now, exp = 0, 1000
    while now <= 3600:  # 1 hour at 600 EXP/min (50 EXP every 5s)
        t.feed(now, exp, pct(exp, 10_000_000)); now += 5; exp += 50
    end_fast = now - 5
    while now <= end_fast + 120:  # then 2 min at 60 EXP/min
        t.feed(now, exp, pct(exp, 10_000_000)); now += 5; exp += 5
    now -= 5
    g1, s1 = t.gained_in(now, 60)
    g5, s5 = t.gained_in(now, 300)
    g60, s60 = t.gained_in(now, 3600)
    fails += check("1 min total ~60 after slowdown", abs(g1 - 60) <= 5 and s1 == 60, (g1, s1))
    fails += check("5 min total = 2 slow + 3 fast min", abs(g5 - (2 * 60 + 3 * 600)) <= 50
                   and s5 == 300, (g5, s5))
    fails += check("60 min total = 2 slow + 58 fast min",
                   abs(g60 - (2 * 60 + 58 * 600)) <= 60 and s60 == 3600, (g60, s60))
    g1_later, _ = t.gained_in(now + 60, 60)
    fails += check("1 min total drops to 0 after a minute of nothing", g1_later == 0, g1_later)

    # 12. Windows that aren't full yet report how much time they cover.
    t = RateTracker()
    for i in range(25):  # 2 min at 120/min
        exp = 20000 + 10 * i
        t.feed(i * 5, exp, pct(exp))
    g60, s60 = t.gained_in(120, 3600)
    fails += check("partial 60-min window", g60 == 240 and s60 == 120, (g60, s60))

    # --- MesoTracker ---------------------------------------------------- #
    # 13. Pickups count as gained; a single misread frame is ignored.
    m = MesoTracker()
    m.feed(0, 69776); m.feed(0.5, 69776)
    fails += check("meso start read", m.current == 69776 and m.session_gained == 0, m.current)
    m.feed(1, 69376)  # one-frame misread (7 -> 3)
    m.feed(1.5, 69776)
    fails += check("one-frame meso misread ignored",
                   m.current == 69776 and m.spent == 0, (m.current, m.spent))
    m.feed(2, 70000); m.feed(2.5, 70000)
    fails += check("pickup counted", m.session_gained == 224 and m.net == 224,
                   (m.session_gained, m.net))
    # 14. Unreadable frames (inventory covered) don't break confirmation.
    m.feed(3, 70100); m.feed(3.5, None); m.feed(4, 70100)
    fails += check("None between reads ok", m.current == 70100, m.current)
    # 15. Spending is tracked separately and doesn't reduce what was earned.
    msg = (m.feed(5, 60100), m.feed(5.5, 60100))[1]
    fails += check("spend tracked", m.spent == 10000 and m.session_gained == 324
                   and m.net == 60100 - 69776, (m.spent, m.session_gained, m.net))
    fails += check("spend message", msg == "Spent 10,000 mesos", msg)
    # 16. Rolling totals over pickups only.
    m = MesoTracker()
    mesos = 1000
    for i in range(0, 600, 5):  # 10 min, +50 every 5s (two reads each)
        m.feed(i, mesos); m.feed(i + 0.5, mesos); mesos += 50
    g1, s1 = m.gained_in(600, 60)
    g5, s5 = m.gained_in(600, 300)
    fails += check("meso 1 min total ~600", abs(g1 - 600) <= 50 and s1 == 60, (g1, s1))
    fails += check("meso 5 min total ~3000", abs(g5 - 3000) <= 50 and s5 == 300, (g5, s5))
    fails += check("meso rate ~600/min", abs(m.rolling_rate(600) - 600) < 20, m.rolling_rate(600))

    print("ALL PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
