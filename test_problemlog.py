"""Tests for problem snapshots. Run: .venv\\Scripts\\python.exe test_problemlog.py"""
import os
import shutil
import sys
import tempfile

from PIL import Image

import problemlog
from problemlog import ProblemLog


def main():
    fails = 0

    def check(name, cond, detail=""):
        nonlocal fails
        fails += 0 if cond else 1
        print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))

    folder = tempfile.mkdtemp()
    try:
        focus = {"exe": "maplestory.exe"}
        log = ProblemLog(folder, lambda: Image.new("RGB", (64, 36), "blue"),
                         focused=lambda: focus["exe"], min_interval=300, keep=3)
        crop = Image.new("RGB", (20, 10), "red")
        region = {"left": 1, "top": 2, "width": 20, "height": 10}

        # 1. A problem while the game is focused: crop + screen + log line.
        p = log.record("hp", "cut off", crop, region, now=1000)
        files = sorted(os.listdir(folder))
        check("crop saved", p is not None and os.path.exists(p), files)
        check("screen saved", any(f.endswith("_screen.png") for f in files), files)
        with open(os.path.join(folder, "problems.log"), encoding="utf-8") as f:
            line = f.read()
        check("log line", "hp  cut off  region=1,2 20x10" in line and "_crop.png" in line, line)

        # 2. Same problem again soon: skipped. A different one: saved.
        check("same problem throttled", log.record("hp", "cut off", crop, region, now=1010) is None)
        check("other problem saved", log.record("hp", "no stack counts", crop, now=1010) is not None)
        check("same problem after interval", log.record("hp", "cut off", crop, now=1400) is not None)

        # 3. Another window focused (browser, the tracker itself): nothing saved.
        focus["exe"] = "chrome.exe"
        check("not saved when game not focused",
              log.record("mp", "cut off", crop, now=2000) is None)
        focus["exe"] = None
        check("not saved when focus unknown", log.record("mp", "x", crop, now=2000) is None)
        # ...and the skip doesn't use up the throttle once the game is back.
        focus["exe"] = "maplestory.exe"
        check("saved once game focused again", log.record("mp", "cut off", crop, now=2001) is not None)

        # 4. Only the newest 'keep' snapshots stay.
        for i in range(5):
            log.record("exp", f"reason {i}", crop, now=3000 + i)
        crops = [f for f in os.listdir(folder) if f.endswith("_crop.png")]
        screens = [f for f in os.listdir(folder) if f.endswith("_screen.png")]
        check("pruned to 3", len(crops) == 3 and len(screens) == 3, (crops, screens))

        # 5. Screen grab failing still saves the crop.
        def boom():
            raise RuntimeError("no screen")
        log2 = ProblemLog(os.path.join(folder, "b"), boom, focused=lambda: "maplestory.exe")
        check("crop saved without screen", log2.record("meso", "x", crop, now=1) is not None)

        # 6. The real focus check runs (returns a name or None, never raises).
        got = problemlog.foreground_process()
        check("foreground_process works", got is None or got.endswith(".exe"), got)
    finally:
        shutil.rmtree(folder)

    print("ALL PASS" if not fails else f"{fails} FAILURE(S)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
