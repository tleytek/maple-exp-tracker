"""Reader for the MapleStory EXP text, e.g. ``11,548[39.85%]``.

The game draws this line in a tiny pixel font where ``,`` and ``.`` are
detached dots spaced like full characters. Whole-line Tesseract handles that
badly (39.85 -> 39.95, 5 <-> 8, dropped separators), so this module doesn't
use OCR at all:

1. Find the two green brackets ``[`` ``]``. They fix the text's vertical band
   and split the line into the EXP part and the percent part.
2. Cut the band into glyphs at blank columns.
3. Short glyphs are separators: ``,`` in the EXP part, ``.`` in the percent
   part. The last glyph inside the brackets is ``%``.
4. Classify each digit by shape (holes and stroke positions), which works at
   any resolution / scale (see ``classify_digit``).
5. Validate the format (``1,234,567`` / ``12.34``) before trusting it.
"""
import numpy as np

THRESH = 140  # brightness (max RGB channel) that counts as "ink"


def _components(mask, diagonal):
    """Connected blobs of True pixels in a small 2-D mask.

    Returns a list of dicts with left, top, width, height, cy (center row),
    and touches_edge. Plain flood fill: the EXP crops are only a few thousand
    pixels, so this is fast and avoids shipping OpenCV.
    """
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    steps = [(-1, 0), (1, 0), (0, -1), (0, 1)]
    if diagonal:
        steps += [(-1, -1), (-1, 1), (1, -1), (1, 1)]
    out = []
    for y, x in zip(*np.nonzero(mask)):
        if seen[y, x]:
            continue
        seen[y, x] = True
        stack, ys, xs = [(y, x)], [], []
        while stack:
            cy, cx = stack.pop()
            ys.append(cy); xs.append(cx)
            for dy, dx in steps:
                ny, nx = cy + dy, cx + dx
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        top, left = min(ys), min(xs)
        out.append({
            "left": left, "top": top,
            "width": max(xs) - left + 1, "height": max(ys) - top + 1,
            "cy": sum(ys) / len(ys),
            "touches_edge": top == 0 or left == 0 or max(ys) == h - 1 or max(xs) == w - 1,
        })
    return out


def _green_mask(rgb):
    r, g, b = (rgb[..., i].astype(int) for i in range(3))
    return (g > 120) & (g - r > 25) & (g - b > 60)


def _segments(cols):
    segs, x, n = [], 0, len(cols)
    while x < n:
        if cols[x]:
            s = x
            while x < n and cols[x]:
                x += 1
            segs.append((s, x))
        else:
            x += 1
    return segs


# --------------------------------------------------------------------------- #
# Digit classification
# --------------------------------------------------------------------------- #
def _holes(ink):
    """Enclosed background regions as (y_center, height), relative to glyph height."""
    h = ink.shape[0]
    return [((c["cy"] + 0.5) / h, c["height"] / h)
            for c in _components(~ink, diagonal=False) if not c["touches_edge"]]


def classify_digit(gray):
    """Classify one digit glyph (2-D brightness array). Returns '0'-'9' or None."""
    ink = gray >= THRESH
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
    # 4: a near-solid crossbar low down, and nothing in the bottom-left
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
    # (rows 0.2-0.4 sit between the top bar and the middle bar)
    upper_left = box(0.2, 0.4, 0.0, 0.3)
    upper_right = box(0.2, 0.4, 0.7, 1.0)
    if box(0.0, 0.15) >= 0.6 and box(0.5, 1.0, 0.5, 1.0) < 0.15:
        return "7"
    if box(0.85, 1.0) >= 0.7:
        return "2"
    if upper_right >= 0.3:
        return "3"
    if upper_left >= 0.3:
        return "5"
    return None


# --------------------------------------------------------------------------- #
# Line reading
# --------------------------------------------------------------------------- #
def segment(rgb):
    """Split the EXP line into glyphs.

    Returns (exp_glyphs, pct_glyphs); each glyph is ('sep', None) or
    ('digit', gray_image). Returns None if the layout isn't recognised.
    """
    # Brackets are tall, narrow green blobs. (The EXP bar below is also green
    # but wide, so it's filtered out by shape.)
    brackets = sorted(
        (c for c in _components(_green_mask(rgb), diagonal=True)
         if 6 <= c["height"] <= 80 and c["width"] <= c["height"] // 2),
        key=lambda c: c["left"])
    if len(brackets) < 2:
        return None
    lbs, rbs = brackets[0], brackets[-1]
    if abs(lbs["top"] - rbs["top"]) > 2:
        return None
    lb_l = lbs["left"]
    lb_r = lb_l + lbs["width"]
    rb_l = rbs["left"]
    top = lbs["top"]
    height = lbs["height"]
    y0, y1 = top, min(rgb.shape[0], top + height + max(2, height // 5))

    gray = rgb.max(axis=2)
    ink = gray >= THRESH
    band = ink[y0:y1]
    above = ink[max(0, top - 3):top]  # taller things (the "EXP" logo) poke above

    def glyphs(x0, x1, drop_tall_prefix):
        out = []
        for s, e in _segments(band[:, x0:x1].any(axis=0)):
            s, e = s + x0, e + x0
            if above.size and above[:, s:e].any():
                if drop_tall_prefix:
                    out = []  # "EXP" logo / junk: discard everything left of it
                    continue
                return None
            rows = np.where(band[:, s:e].any(axis=1))[0]
            if rows.max() - rows.min() + 1 < height * 0.5:
                out.append(("sep", None))
            else:
                out.append(("digit", gray[y0:y1, s:e]))
        return out

    exp_g = glyphs(0, lb_l, True)
    pct_g = glyphs(lb_r, rb_l, False)
    if not exp_g or not pct_g or pct_g[-1][0] != "digit":
        return None
    return exp_g, pct_g[:-1]  # drop '%'


def _to_text(glyphs, sep_char):
    out = []
    for kind, img in glyphs:
        if kind == "sep":
            out.append(sep_char)
        else:
            d = classify_digit(img)
            if d is None:
                return None
            out.append(d)
    return "".join(out)


def _valid_exp(s):
    groups = s.split(",")
    return (all(g.isdigit() for g in groups) and 1 <= len(groups[0]) <= 3
            and all(len(g) == 3 for g in groups[1:]))


def _valid_pct(s):
    whole, _, frac = s.partition(".")
    return whole.isdigit() and len(frac) == 2 and frac.isdigit() and float(s) <= 100


def read(rgb):
    """Return (exp:int|None, percent:float|None, status:str)."""
    rgb = np.asarray(rgb)
    seg = segment(rgb)
    if seg is None:
        return None, None, "layout"
    exp_s = _to_text(seg[0], ",")
    pct_s = _to_text(seg[1], ".")
    if exp_s is None or pct_s is None:
        return None, None, "glyph"
    if not _valid_exp(exp_s):
        return None, None, f"bad exp {exp_s!r}"
    if not _valid_pct(pct_s):
        return None, None, f"bad pct {pct_s!r}"
    return int(exp_s.replace(",", "")), float(pct_s), "ok"
