"""Reader for the item counts on potion stacks in the inventory, e.g. ``73``.

Every inventory slot shows its stack size in its bottom-left corner, in a
6x9 pixel font: a 1-pixel stroke filled white-to-light-blue, outlined in
black, drawn over the item icon. The region covers one or more potion
slots and the result is the total of all the stacks in it.

1. Undo the game's integer upscaling (2x at the sharp resolution) so the
   font is matched pixel for pixel.
2. Keep the digit-fill colours that are boxed in by fill or black outline
   (icon pixels mostly aren't).
3. Each row of slots has its numbers on one text line; find those lines
   from glyph-sized blobs and cut each line into glyphs at blank columns.
4. Match glyphs against the font, tolerating a couple of stray pixels
   (icon colours showing through a digit's hole, a selected slot's frame).
5. Group neighbouring digits into numbers; a group with any unrecognised
   glyph fails the whole read rather than dropping a digit.

All ten digits come from one real capture (``tests/potions``); 9 only
appeared with an icon pixel in its loop, so its clean shape is inferred.
"""
import numpy as np

from expocr import _components, _segments

GLYPH_H = 9
MAX_STACK = 100  # potions per slot; anything bigger isn't a potion stack
MAX_SCALE = 4
MAX_ERRORS = 3   # stray / missing pixels tolerated when matching a digit
MIN_GLYPH_PIXELS = 5  # fewer lit pixels than this is a stray icon pixel ("1" has 10)
DIGIT_GAP = 3    # max blank columns between digits of one number

_FONT = {
    "0": [".####.", "#....#", "#....#", "#....#", "#....#", "#....#", "#....#", "#....#", ".####."],
    "1": ["..#", "###", "..#", "..#", "..#", "..#", "..#", "..#", "..#"],
    "2": [".####.", "#....#", ".....#", ".....#", "....#.", "..##..", ".#....", "#.....", "######"],
    "3": [".####.", "#....#", ".....#", ".....#", "..###.", ".....#", ".....#", "#....#", ".####."],
    "4": ["....#.", "...##.", "..#.#.", ".#..#.", "#...#.", "######", "....#.", "....#.", "....#."],
    "5": ["######", "#.....", "#.....", "#####.", ".....#", ".....#", ".....#", "#....#", ".####."],
    "6": ["..###.", ".#....", "#.....", "#####.", "#....#", "#....#", "#....#", "#....#", ".####."],
    "7": ["######", ".....#", "....#.", "....#.", "...#..", "...#..", "..#...", "..#...", ".#...."],
    "8": [".####.", "#....#", "#....#", "#....#", ".####.", "#....#", "#....#", "#....#", ".####."],
    "9": [".####.", "#....#", "#....#", "#....#", "#....#", ".#####", ".....#", ".....#", ".####."],
}
FONT = {d: np.array([[c == "#" for c in r] for r in rows], bool) for d, rows in _FONT.items()}


def _aligned(changes, s):
    """Offset at which (almost) every colour change falls on a multiple of s."""
    if len(changes) == 0:
        return None
    counts = np.bincount(changes % s, minlength=s)
    off = int(counts.argmax())
    return off if counts[off] >= 0.99 * len(changes) else None


def _unscale(rgb):
    """Undo integer upscaling: return (game_pixels, scale).

    In an image upscaled s times, colour changes only happen between the
    s x s blocks, i.e. at positions that agree modulo s.
    """
    rgb = rgb.astype(np.int16)
    xs = np.nonzero((rgb[:, 1:] != rgb[:, :-1]).any(axis=2))[1] + 1
    ys = np.nonzero((rgb[1:] != rgb[:-1]).any(axis=2))[0] + 1
    for s in range(MAX_SCALE, 1, -1):
        ox, oy = _aligned(xs, s), _aligned(ys, s)
        if ox is not None and oy is not None:
            return rgb[oy::s, ox::s], s
    return rgb, 1


def _fill_mask(g):
    r, gg, b = (g[..., i].astype(int) for i in range(3))
    fill = (b == 255) & (r >= 150) & (gg >= 200)
    boxed = fill | (g.max(axis=2) < 40)
    padded = np.pad(boxed, 1, constant_values=True)  # region edge: can't tell
    ok = fill.copy()
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        ok &= padded[1 + dy:1 + dy + fill.shape[0], 1 + dx:1 + dx + fill.shape[1]]
    return ok


def classify(glyph):
    """Best-matching digit for a glyph (2-D bool, GLYPH_H rows), or None."""
    pad = np.pad(glyph, ((0, 0), (1, 1)))
    scores = []
    for d, t in FONT.items():
        best = None
        for x in range(0, max(1, pad.shape[1] - t.shape[1] + 1)):
            window = np.zeros_like(pad)
            tw = min(t.shape[1], pad.shape[1] - x)
            window[:, x:x + tw] = t[:, :tw]
            err = int((window != pad).sum())
            best = err if best is None else min(best, err)
        scores.append((best, d))
    scores.sort()
    (err, d), (err2, _) = scores[0], scores[1]
    if err <= MAX_ERRORS and err2 >= err + 3:
        return d
    return None


def _line_tops(mask):
    tops = sorted({c["top"] for c in _components(mask, diagonal=True)
                   if c["height"] == GLYPH_H and c["width"] <= 8})
    out = []
    for t in tops:  # neighbouring rows are the same line
        if not out or t - out[-1] > 2:
            out.append(t)
    return out


def _read_line(mask, top):
    """Numbers on one text line: list of (x, count). Raises ValueError."""
    band = mask[top:top + GLYPH_H]
    numbers, run, last_end = [], [], None

    def close():
        if not run:
            return
        if any(d is None for _, d in run):
            if any(d is not None for _, d in run):
                raise ValueError("unrecognised digit in a stack count (covered by the cursor?)")
            return  # icon pixels, not text
        s = "".join(d for _, d in run)
        if s.startswith("0"):
            raise ValueError(f"stack count read as {s!r}, which isn't valid")
        numbers.append((run[0][0], int(s)))

    for s, e in _segments(band.any(axis=0)):
        if band[:, s:e].sum() < MIN_GLYPH_PIXELS:
            continue
        if s == 0 or e == band.shape[1]:
            raise ValueError("a stack count is cut off at the side of the region")
        if last_end is not None and s - last_end > DIGIT_GAP:
            close()
            run = []
        run.append((s, classify(band[:, s:e]) if e - s <= 8 else None))
        last_end = e
    close()
    return numbers


def read_stacks(rgb):
    """Return (stacks:list[int]|None, status:str)."""
    rgb = np.asarray(rgb)[..., :3]
    g, _ = _unscale(rgb)
    mask = _fill_mask(g)
    h = mask.shape[0]
    for c in _components(mask, diagonal=True):
        if (c["top"] == 0 or c["top"] + c["height"] == h) and 3 <= c["height"] < GLYPH_H \
                and c["width"] <= 8:
            return None, "a stack count is cut off at the top or bottom of the region"
    stacks = []
    try:
        for top in _line_tops(mask):
            stacks += [n for _, n in _read_line(mask, top)]
    except ValueError as e:
        return None, str(e)
    if not stacks:
        return None, "no stack counts in the region (inventory closed or on another tab?)"
    big = [n for n in stacks if n > MAX_STACK]
    if big:
        return None, f"a stack of {big[0]:,} isn't a potion stack; select only the potion slots"
    return stacks, "ok"


def read(rgb):
    """Return (total:int|None, status:str)."""
    stacks, status = read_stacks(rgb)
    return (sum(stacks) if stacks else None), status
