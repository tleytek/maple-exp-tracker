"""Tests for the meso reader. Run: .venv\\Scripts\\python.exe test_mesoocr.py"""
import os
import sys

import numpy as np
from PIL import Image

import mesoocr
from main import padded

HERE = os.path.dirname(os.path.abspath(__file__))
CAP_DIR = os.path.join(HERE, "tests", "meso")

# The meso font (5 wide x 8 tall). Only digits copied from real captures are
# listed; add the rest as they're captured (tests/meso/<mesos>_<tag>.png).
FONT = {
    "3": [".###.", "#...#", "....#", "..##.", "....#", "....#", "#...#", ".###."],
    "6": ["..##.", ".#...", "#....", "####.", "#...#", "#...#", "#...#", ".###."],
    "7": ["#####", "....#", "...#.", "...#.", "..#..", "..#..", ".#...", ".#..."],
    "9": [".###.", "#...#", "#...#", "#...#", ".####", "....#", "...#.", ".##.."],
}
COMMA = [".#", ".#", ".#", "#."]  # sits on the baseline and hangs below it


def render(rows, scale):
    a = np.array([[c == "#" for c in r] for r in rows], bool)
    return np.kron(a, np.ones((scale, scale), bool))


def render_box(text, scale=2, width=150):
    """White box with right-aligned black text, like the inventory's."""
    h = 14 * scale
    ink = np.zeros((h, width * scale), bool)
    x = ink.shape[1] - 5 * scale
    for ch in reversed(text):
        g = render(COMMA if ch == "," else FONT[ch], scale)
        y = 3 * scale + (6 * scale if ch == "," else 0)
        x -= g.shape[1]
        ink[y:y + g.shape[0], x:x + g.shape[1]] = g
        x -= scale
    rgb = np.full(ink.shape + (3,), 255, np.uint8)
    rgb[ink] = 0
    # blue-grey border around the box, on a grey-blue window background
    rgb = np.pad(rgb, ((2, 2), (2, 2), (0, 0)), constant_values=0)
    rgb[:2], rgb[-2:], rgb[:, :2], rgb[:, -2:] = (49, 85, 115), (49, 85, 115), (49, 85, 115), (49, 85, 115)
    out = np.empty((rgb.shape[0] + 12, rgb.shape[1] + 12, 3), np.uint8)
    out[:] = (156, 177, 199)
    out[6:-6, 6:-6] = rgb
    return out


def main():
    failures = 0

    def check(name, got, want):
        nonlocal failures
        if got[0] != want:
            failures += 1
            print(f"FAIL {name}: got {got}")
        else:
            print(f"ok   {name}")

    # 1. digits from real captures, at several scales
    for scale in (1, 2, 3):
        for d, rows in FONT.items():
            got = mesoocr.classify_digit(render(rows, scale))
            if got != d:
                failures += 1
                print(f"FAIL digit {d} @x{scale}: got {got}")

    # 2. whole boxes
    for text in ("69,776", "7", "999,666,777", "6"):
        check(f"box {text}", mesoocr.read(render_box(text)), int(text.replace(",", "")))

    # 3. real captures: tests/meso/<mesos>_<tag>.png
    for f in sorted(os.listdir(CAP_DIR)):
        if f.endswith(".png") and not f.startswith("full_"):
            got = mesoocr.read(np.array(Image.open(os.path.join(CAP_DIR, f)).convert("RGB")))
            check(f"capture {f}", got, int(f.split("_")[0]))

    # 4. region placement on a real capture of the inventory's bottom edge
    #    (box at left 50, top 72, 286x28 in this strip). The app grabs a
    #    padded box, so loose selections work and cut-off numbers are refused.
    full = np.array(Image.open(os.path.join(CAP_DIR, "full_69776.png")).convert("RGB"))
    screen = {"left": 0, "top": 0, "width": full.shape[1], "height": full.shape[0]}
    cases = {
        "just the digits": ({"left": 250, "top": 76, "width": 85, "height": 20}, 69776),
        "whole box": ({"left": 45, "top": 68, "width": 300, "height": 36}, 69776),
        "includes inventory frame": ({"left": 0, "top": 0, "width": 430, "height": 130}, 69776),
        "cuts off leading digits": ({"left": 292, "top": 76, "width": 44, "height": 20}, None),
    }
    for name, (r, want) in cases.items():
        p = padded(r, screen)
        got = mesoocr.read(full[p["top"]:p["top"] + p["height"], p["left"]:p["left"] + p["width"]])
        check(f"region '{name}'", got, want)
        if want is None and "cut off" not in got[1]:
            failures += 1
            print(f"FAIL region '{name}': reason should say cut off, got {got[1]!r}")

    # 5. no box at all (inventory closed), and the white chat input bar
    #    (seen live with the inventory closed) must not read as mesos
    check("no box", mesoocr.read(np.zeros((40, 200, 3), np.uint8)), None)
    chat = np.array(Image.open(os.path.join(CAP_DIR, "full_none_chatbar.png")).convert("RGB"))
    check("chat input bar", mesoocr.read(chat), None)

    print("ALL PASS" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
