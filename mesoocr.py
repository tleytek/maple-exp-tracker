"""Reader for the meso amount in the inventory window, e.g. ``69,776``.

The amount is drawn in black inside a white box at the bottom of the item
window, right-aligned, in a 5x8 pixel font (doubled at the sharp
resolution). It's a different font from the EXP line, so it has its own
digit rules, but the approach is the same as ``expocr``:

1. Find the white box. It fixes where the text is and lets us tell whether
   the selected region cut the number off.
2. Cut the dark text inside it into glyphs at blank columns.
3. Short glyphs are ``,`` separators; the rest are classified by shape.
4. Validate the format (``1,234,567``) before trusting it.

Digits seen in real captures so far: 6, 7, 9. The rules for the others
follow the same font structure but are unverified until a real capture of
them passes; an unknown shape is reported as unreadable, not guessed.
"""
import numpy as np

from expocr import _components, _holes, _segments

WHITE = 235  # min RGB channel of the box background
DARK = 115   # max RGB channel of text pixels
MIN_BOX_H = 8
MAX_BOX_H = 80


def classify_digit(ink):
    """Classify one glyph (2-D bool array, True = text). Returns '0'-'9' or None."""
    rows = np.where(ink.any(axis=1))[0]
    cols = np.where(ink.any(axis=0))[0]
    if len(rows) == 0:
        return None
    ink = ink[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    h, w = ink.shape
    if w <= 0.45 * h:
        return "1"

    def box(y0, y1, x0=0.0, x1=1.0):
        """Ink fraction inside the relative box [y0,y1) x [x0,x1)."""
        r0 = int(round(y0 * h)); r1 = max(r0 + 1, int(round(y1 * h)))
        c0 = int(round(x0 * w)); c1 = max(c0 + 1, int(round(x1 * w)))
        return float(ink[r0:r1, c0:c1].mean())

    row_fill = ink.mean(axis=1)
    # 4: a near-solid crossbar low down and nothing in the bottom-left
    # (checked before holes: a 4's closed triangle would look like a 9)
    crossbar = bool((row_fill[int(0.5 * h):int(0.85 * h) + 1] >= 0.9).any())
    if crossbar and box(0.85, 1.0, 0.0, 0.35) == 0:
        return "4"

    holes = _holes(ink)
    if len(holes) >= 2:
        return "8"
    if len(holes) == 1:
        cy, hh = holes[0]
        if hh >= 0.45:
            return "0"
        return "9" if cy < 0.5 else "6"

    # no enclosed holes: 2, 3, 5, 7
    top_bar = box(0.0, 1 / 8) >= 0.8
    bottom = box(7 / 8, 1.0)
    # 7: full top bar, nothing on the left under it, and only the tail of
    # the diagonal on the bottom row
    if top_bar and box(1 / 8, 0.5, 0.0, 0.4) == 0 and bottom <= 0.4:
        return "7"
    if bottom >= 0.8:
        return "2"
    upper_left = box(1 / 8, 3 / 8, 0.0, 0.3)
    upper_right = box(1 / 8, 3 / 8, 0.7, 1.0)
    if upper_left == 0 and upper_right > 0:
        return "3"
    if upper_left > 0 and upper_right == 0:
        return "5"
    return None


def _boxes(rgb):
    """White text boxes, widest first.

    The meso box is a solid white rectangle with only the text inside it.
    Other white areas of the inventory window (its frame, highlights) are
    irregular, so require straight white top/bottom edges and an almost
    fully white interior.
    """
    white = rgb.min(axis=2) >= WHITE
    found = []
    for c in _components(white, diagonal=False):
        if not (MIN_BOX_H <= c["height"] <= MAX_BOX_H and c["width"] >= 2 * c["height"]):
            continue
        area = white[c["top"]:c["top"] + c["height"], c["left"]:c["left"] + c["width"]]
        if area[0].mean() >= 0.95 and area[-1].mean() >= 0.95 and area.mean() >= 0.8:
            found.append(c)
    return sorted(found, key=lambda c: -c["width"])


def _read_box(rgb, b):
    y0, y1 = b["top"], b["top"] + b["height"]
    x0, x1 = b["left"], b["left"] + b["width"]
    ink = rgb[y0:y1, x0:x1].max(axis=2) <= DARK
    segs = _segments(ink.any(axis=0))
    if not segs:
        return None, "the meso box is empty (inventory covered?)"
    # Right-aligned text: if a glyph touches the box's edge, the region
    # (or something in front of the box) is cutting the number off.
    if segs[0][0] == 0:
        return None, "the left side of the meso amount is cut off"
    if segs[-1][1] == ink.shape[1]:
        return None, "the right side of the meso amount is cut off"
    heights = []
    for s, e in segs:
        rows = np.where(ink[:, s:e].any(axis=1))[0]
        heights.append(rows.max() - rows.min() + 1)
    digit_h = max(heights)
    # Shape of the meso text: digits about half the box's height, set flush
    # right. Other white boxes (e.g. the chat input bar) don't fit this.
    if not 0.35 <= digit_h / b["height"] <= 0.8:
        return None, "the text in the white box doesn't look like the meso amount"
    box_cut_right = b["left"] + b["width"] == rgb.shape[1]
    if not box_cut_right and ink.shape[1] - segs[-1][1] > 1.5 * digit_h:
        return None, "the text in the white box isn't right-aligned like the meso amount"
    text = []
    for (s, e), gh in zip(segs, heights):
        if gh < 0.6 * digit_h:
            text.append(",")
            continue
        d = classify_digit(ink[:, s:e])
        if d is None:
            return None, "unrecognised digit in the meso amount"
        text.append(d)
    s = "".join(text)
    groups = s.split(",")
    if not (all(g.isdigit() for g in groups) and 1 <= len(groups[0]) <= 3
            and all(len(g) == 3 for g in groups[1:])):
        return None, f"mesos read as {s!r}, which isn't a valid number"
    return int(s.replace(",", "")), "ok"


def read(rgb):
    """Return (mesos:int|None, status:str)."""
    rgb = np.asarray(rgb)[..., :3]
    boxes = _boxes(rgb)
    if not boxes:
        return None, "no white meso box in the region (inventory closed or moved?)"
    first_error = None
    for b in boxes:
        result = _read_box(rgb, b)
        if result[1] == "ok":
            return result
        first_error = first_error or result
    return first_error
