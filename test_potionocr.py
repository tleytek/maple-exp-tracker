"""Tests for the potion stack reader. Run: .venv\\Scripts\\python.exe test_potionocr.py"""
import os
import sys

import numpy as np
from PIL import Image

import potionocr

HERE = os.path.dirname(os.path.abspath(__file__))
CAP = os.path.join(HERE, "tests", "potions", "use_tab_100_73_25_34_60_12.png")

# Real capture of the inventory's Use tab at 2x scale (5 columns x 6 rows,
# slots every 36 x 34 game pixels, first slot's count at game x=6, y=26).
# Slot (col, row) -> its stack count:
#   row 1: 500 500 500 500 482      row 2: 100 73 (empty) 25 34
#   row 3: 60 12 2 60 29            row 4: 10 28 1 1 1
#   row 5: 1 2 3 627                row 0: 1000 x4, 500 (selected, damaged)


def slots(col0, row0, cols=1, rows=1):
    """Pixel box (left, top, right, bottom) covering a block of slots."""
    x0, y0 = 2 * (36 * col0), 2 * (34 * row0 + 4)
    return x0, y0, x0 + 2 * (36 * cols - 1), y0 + 2 * (34 * rows - 1)


def main():
    failures = 0
    full = np.array(Image.open(CAP).convert("RGB"))

    def check(name, got, want, reason=None):
        nonlocal failures
        ok = got[0] == want and (reason is None or reason in got[1])
        if not ok:
            failures += 1
        print(("ok   " if ok else "FAIL ") + f"{name}: {got}")

    def crop(box, img=full):
        l, t, r, b = box
        return img[t:b, l:r]

    # 1. every digit from the capture, read as whole stacks (rows 1 and 5 hold
    #    shuriken/arrow stacks; their counts are within the potion limit)
    check("row 1 digits (500 x4, 482)", potionocr.read_stacks(crop(slots(0, 1, 5))),
          [500, 500, 500, 500, 482])
    check("row 5 digits (1, 2, 3, 627)", potionocr.read_stacks(crop(slots(0, 5, 4))),
          [1, 2, 3, 627])
    check("row 2 HP/MP pots (100, 73, empty, 25, 34)", potionocr.read(crop(slots(0, 2, 5))), 232)
    check("row 3 (60, 12, 2, 60, 29)", potionocr.read(crop(slots(0, 3, 5))), 163)
    check("row 4 (10, 28 with icon pixels in the 8, 1, 1, 1)",
          potionocr.read(crop(slots(0, 4, 5))), 41)
    check("stacks up to 9999 accepted", potionocr.read(crop(slots(0, 1, 5))), 2482)
    # the limit still refuses bigger stacks (lowered here: no 5-digit capture)
    limit, potionocr.MAX_STACK = potionocr.MAX_STACK, 499
    check("stacks over the limit refused", potionocr.read(crop(slots(0, 1, 5))), None,
          "isn't a potion stack")
    potionocr.MAX_STACK = limit

    # 2. typical potion selections, including a selected (highlighted) slot
    check("100 in the selected slot", potionocr.read(crop(slots(0, 2))), 100)
    check("red pot 25", potionocr.read(crop(slots(3, 2))), 25)
    check("two rows of pots", potionocr.read(crop(slots(3, 2, 2, 2))), 25 + 34 + 60 + 29)
    check("slot + empty slot", potionocr.read(crop(slots(1, 2, 2))), 73)

    # 3. other integer scales
    game = full[::2, ::2]
    for s in (1, 3):
        img = np.kron(game, np.ones((s, s, 1), np.uint8)) if s > 1 else game
        box = tuple(v * s // 2 for v in slots(0, 2, 5))
        check(f"row 2 at {s}x", potionocr.read(crop(box, img)), 232)

    # 4. regions that would silently drop part of a count must fail instead
    l, t, r, b = slots(0, 2)
    check("cut through the number (left)", potionocr.read(full[t:b, l + 22:r]), None, "cut off")
    check("cut through the number (bottom)", potionocr.read(full[t:t + 52, l:r]), None, "cut off")
    check("damaged 5 in a selected slot", potionocr.read(crop(slots(4, 0))), None, "unrecognised")
    check("empty slot", potionocr.read(crop(slots(2, 2))), None, "no stack counts")
    check("nothing", potionocr.read(np.zeros((60, 60, 3), np.uint8)), None, "no stack counts")

    # 5. live quickslot captures (2x): a stack over 100, and slots whose
    #    edges catch a few off-grid pixels (must still unscale)
    for f, want in (("quick_161.png", 161), ("quick_49_offgrid.png", 49),
                    ("quick_51_offgrid.png", 51)):
        img = np.array(Image.open(os.path.join(HERE, "tests", "potions", f)).convert("RGB"))
        check(f"capture {f}", potionocr.read(img), want)

    print("ALL PASS" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
