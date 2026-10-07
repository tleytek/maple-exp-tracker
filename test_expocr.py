"""Tests for the EXP-line reader. Run: env\\Scripts\\python.exe test_expocr.py"""
import os
import sys

import numpy as np

import expocr

HERE = os.path.dirname(os.path.abspath(__file__))

# Crisp pixel font (5 wide x 7 tall, rendered at 2x) in the style of the
# in-game glyphs seen at the sharper resolution. 0,1,2,4,6,7,8 are copied from
# real captures; 3,5,9 follow the same style.
FONT = {
    "0": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "1": [".#", "##", ".#", ".#", ".#", ".#", ".#"],
    "2": [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"],
    "3": [".###.", "#...#", "....#", "..##.", "....#", "#...#", ".###."],
    "4": ["...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
    "5": ["#####", "#....", "#....", "####.", "....#", "#...#", ".###."],
    "6": [".###.", "#...#", "#....", "####.", "#...#", "#...#", ".###."],
    "7": ["#####", "....#", "....#", "....#", "...#.", "..#..", "..#.."],
    "8": [".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."],
    "9": [".###.", "#...#", "#...#", ".####", "....#", "#...#", ".###."],
}


def render(rows, scale=2):
    a = np.array([[255 if c == "#" else 0 for c in r] for r in rows], np.uint8)
    a = np.kron(a, np.ones((scale, scale), np.uint8))
    return np.pad(a, ((0, scale * 1), (0, 0)))  # blank row below, like the game


def render_line(text, scale=2):
    """Render e.g. '12,345[67.89%]' as an RGB image with green brackets."""
    h = 7 * scale + scale
    cols = [np.zeros((h, 4 * scale), np.uint8)]
    colors = []  # per-column-block: 'w' or 'g'
    blocks = []
    for ch in text:
        if ch.isdigit():
            g = render(FONT[ch], scale)
        elif ch in ",.":
            g = np.zeros((h, scale), np.uint8)
            g[6 * scale:7 * scale] = 255
        elif ch in "[]":
            g = np.full((h - scale, scale), 255, np.uint8)
            g = np.pad(g, ((0, scale), (0, 0)))
        elif ch == "%":
            g = render(["#...#", "#..#.", "..#..", ".#...", "#..##", "...##", "....."], scale)
            g[0:scale * 2, 0:scale] = 255
        blocks.append((g, "g" if ch in "[]" else "w"))
    rgb_parts = [np.zeros((h, 4 * scale, 3), np.uint8)]
    for g, col in blocks:
        rgb = np.zeros(g.shape + (3,), np.uint8)
        if col == "g":
            rgb[g > 0] = (150, 200, 50)
        else:
            rgb[g > 0] = (238, 238, 238)
        rgb_parts += [rgb, np.zeros((h, scale, 3), np.uint8)]
    line = np.hstack(rgb_parts)
    return np.pad(line, ((6, 6), (0, 6), (0, 0)))


def main():
    failures = 0

    # 1. every digit of the crisp font, at 1x, 2x and 3x scale
    for scale in (1, 2, 3):
        for d, rows in FONT.items():
            if scale == 1 and d != "1":
                pass
            got = expocr.classify_digit(render(rows, scale))
            if got != d:
                failures += 1
                print(f"FAIL digit {d} @x{scale}: got {got}")

    # 2. whole lines
    for text, want in [
        ("11,886[41.01%]", (11886, 41.01)),
        ("1,234,567[89.23%]", (1234567, 89.23)),
        ("975[5.70%]", (975, 5.70)),
        ("0[0.00%]", (0, 0.0)),
        ("23,450[100.00%]", (23450, 100.0)),
    ]:
        got = expocr.read(render_line(text))
        if got[:2] != want:
            failures += 1
            print(f"FAIL line {text}: got {got}")

    # 3. real captures, if present (tests/<exp>_<pct>.png)
    cap_dir = os.path.join(HERE, "tests")
    if os.path.isdir(cap_dir):
        for f in sorted(os.listdir(cap_dir)):
            if not f.endswith(".png") or f.startswith("fullbar_"):
                continue
            exp_s, pct_s = f[:-4].split("_")[:2]
            from PIL import Image
            got = expocr.read(np.array(Image.open(os.path.join(cap_dir, f)).convert("RGB")))
            want = (int(exp_s), float(pct_s))
            if got[:2] != want:
                failures += 1
                print(f"FAIL capture {f}: got {got}")
            else:
                print(f"ok   capture {f}")

    # 4. Region-placement mistakes, on a real full-screen capture: the app grabs
    #    a padded box, so tight / misaligned / generous selections still work.
    full_path = os.path.join(cap_dir, "fullbar_22726_78.42.png")
    if os.path.exists(full_path):
        from PIL import Image
        from main import padded
        full = np.array(Image.open(full_path).convert("RGB"))
        screen = {"left": 0, "top": 0, "width": full.shape[1], "height": full.shape[0]}
        # crop is the bottom strip of a 2560x1440 screen starting at y=1340
        cases = {
            "cuts off ]": {"left": 1352, "top": 32, "width": 209, "height": 34},
            "cuts off EXP digits": {"left": 1400, "top": 32, "width": 200, "height": 34},
            "cuts top of text": {"left": 1340, "top": 45, "width": 260, "height": 20},
            "includes HP/MP text": {"left": 1300, "top": 10, "width": 340, "height": 70},
        }
        for name, r in cases.items():
            p = padded(r, screen)
            got = expocr.read(full[p["top"]:p["top"] + p["height"], p["left"]:p["left"] + p["width"]])
            if got[:2] != (22726, 78.42):
                failures += 1
                print(f"FAIL region '{name}': got {got}")
            else:
                print(f"ok   region '{name}'")

    print("ALL PASS" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
