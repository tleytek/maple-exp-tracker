"""MapleStory EXP / rate tracker.

A small always-available window that OCRs the EXP text on screen and shows
current EXP, %, EXP per minute/hour and time to level.

Hotkeys (global): F1 = reset rate, F2 = hide/show window.
"""
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk

import mss
import numpy as np
from PIL import Image, ImageTk

import expocr
import mesoocr
import potionocr
from problemlog import ProblemLog

try:
    import keyboard  # global hotkeys; optional
except Exception:  # pragma: no cover
    keyboard = None

# Settings live in %APPDATA% so the app works the same whether it's run from
# source or as a single .exe (which unpacks to a new temp folder every run).
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "MapleExpTracker")
REGION_FILE = os.path.join(DATA_DIR, "region.json")
LEGACY_REGION_FILE = os.path.join(APP_DIR, "region.json")  # pre-exe location
MESO_REGION_FILE = os.path.join(DATA_DIR, "meso_region.json")
REGION_FILES = {
    "meso": MESO_REGION_FILE,
    "hp": os.path.join(DATA_DIR, "hp_region.json"),
    "mp": os.path.join(DATA_DIR, "mp_region.json"),
}
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
PROBLEMS_DIR = os.path.join(DATA_DIR, "problems")  # snapshots of failed reads

POLL_RATE = 0.5  # seconds between screen reads
CONFIRM_READS = 3  # matching reads needed to accept a level-up
ADOPT_READS = 20  # matching reads (~10s) needed to replace a wrong level-max estimate
MESO_CONFIRM_READS = 2  # identical reads needed to accept a new meso amount
POTION_BIG_DROP = 10  # potions; a bigger drop at once may be a covered stack
POTION_BIG_DROP_READS = 20  # (~10s) identical reads needed to accept a big drop
ROLLING_WINDOWS = (60, 300, 3600)  # seconds: rolling "EXP gained in the last …"
ROLLING_WINDOW = 300  # window whose pace drives the time-to-level estimate
ROLLING_MIN_SPAN = 30  # don't estimate a pace from less history than this
MIN_REGION_SIZE = 5  # pixels


# --------------------------------------------------------------------------- #
# Region persistence
# --------------------------------------------------------------------------- #
def region_is_valid(region):
    return (
        isinstance(region, dict)
        and all(k in region for k in ("left", "top", "width", "height"))
        and region["width"] >= MIN_REGION_SIZE
        and region["height"] >= MIN_REGION_SIZE
    )


def load_region(kind="exp"):
    paths = (REGION_FILE, LEGACY_REGION_FILE) if kind == "exp" else (REGION_FILES[kind],)
    for path in paths:
        try:
            with open(path, "r") as f:
                region = json.load(f)
        except (OSError, ValueError):
            continue
        if region_is_valid(region):
            return region
    return None


def save_region(region, kind="exp"):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(REGION_FILE if kind == "exp" else REGION_FILES[kind], "w") as f:
        json.dump(region, f)


DEFAULT_SETTINGS = {
    "hp_price": 0,  # mesos per potion
    "mp_price": 0,
    "hidden_rows": [],  # stat rows the user unticked in the Rows menu
}


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return settings
    if isinstance(saved, dict):
        for key in ("hp_price", "mp_price"):
            if isinstance(saved.get(key), (int, float)) and saved[key] >= 0:
                settings[key] = saved[key]
        hidden = saved.get("hidden_rows")
        if isinstance(hidden, list):
            settings["hidden_rows"] = [k for k in hidden if isinstance(k, str)]
    return settings


def save_settings(settings):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)


# --------------------------------------------------------------------------- #
# OCR
# --------------------------------------------------------------------------- #
def grab(sct, region):
    shot = sct.grab(region)
    return Image.frombytes("RGB", shot.size, shot.rgb)


def padded(region, screen):
    """Grow the selected box a bit so a slightly-too-tight selection still
    contains both brackets. Anything extra around the text is ignored by the
    reader (it anchors on the brackets and drops the "EXP" logo)."""
    px = max(16, region["width"] // 4)
    py = max(6, region["height"] // 3)
    left = max(screen["left"], region["left"] - px)
    top = max(screen["top"], region["top"] - py)
    right = min(screen["left"] + screen["width"], region["left"] + region["width"] + px)
    bottom = min(screen["top"] + screen["height"], region["top"] + region["height"] + py)
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def screen_capturer():
    """mss renamed mss.mss -> mss.MSS in 10.x; support both."""
    if hasattr(mss, "MSS"):
        return mss.MSS()
    return mss.mss()


def grab_full_screen():
    """Screenshot of every monitor (for problem snapshots)."""
    with screen_capturer() as sct:
        return grab(sct, sct.monitors[0])


def extract_number(pil_img):
    """Return (exp, percent, status) read from an image of the EXP text."""
    return expocr.read(np.asarray(pil_img))


def extract_mesos(pil_img):
    """Return (mesos, status) read from an image of the inventory's meso box."""
    return mesoocr.read(np.asarray(pil_img))


def extract_potions(pil_img):
    """Return (total, status) read from an image of potion slots."""
    return potionocr.read(np.asarray(pil_img))


# --------------------------------------------------------------------------- #
# Rate logic (no UI, so it can be tested on its own)
# --------------------------------------------------------------------------- #
class RollingGains:
    """Rolling 'gained in the last N seconds' totals.

    Subclasses keep 'session_gained' (a running total that only goes up) and
    append (time, session_gained) to 'gain_log' whenever it changes.
    """

    def _prune_gain_log(self, now):
        # keep one sample at/before the longest window's start: it's the
        # total at that moment
        oldest = now - max(ROLLING_WINDOWS)
        while len(self.gain_log) >= 2 and self.gain_log[1][0] <= oldest:
            self.gain_log.popleft()

    def gained_in(self, now, window):
        """Amount gained in the last 'window' seconds up to 'now'.

        Returns (gained, span): span is less than 'window' while there isn't
        that much history yet. None when nothing has been read. Measured up to
        'now', so it keeps falling while nothing is being gained.
        """
        self._prune_gain_log(now)
        if not self.gain_log:
            return None
        start = now - window
        base_t, base_g = self.gain_log[0]
        for t, g in reversed(self.gain_log):
            if t <= start:  # total at the window start
                base_t, base_g = start, g
                break
        return max(0, self.session_gained - base_g), min(now - base_t, window)

    def rolling_rate(self, now, window=ROLLING_WINDOW):
        """Amount per minute over the last 'window' seconds, up to 'now'."""
        got = self.gained_in(now, window)
        if got is None or got[1] < ROLLING_MIN_SPAN:
            return None
        gained, span = got
        return gained / (span / 60)


class RateTracker(RollingGains):
    def __init__(self):
        self.reset_session()

    def reset_session(self):
        self.prev = None
        self.current = None
        self.percent = None
        self.rate = None  # EXP per minute
        self.change_log = deque(maxlen=1000)
        # EXP needed for the current level, as an interval narrowed by every
        # accepted (exp, percent) reading. Used to reject inconsistent reads.
        self.max_range = None
        self.pending_kind = None
        self.pending_range = None
        self.pending_count = 0
        self.session_start = time.time()
        self.session_gained = 0
        self.levels = 0
        # (time, session_gained) on every accepted change. Uses total gained
        # rather than raw EXP so the rolling rate runs straight through
        # level-ups and deaths.
        self.gain_log = deque()
        self.last_seen = None  # time of the latest reading

    def reset_rate(self):
        self.change_log.clear()
        self.gain_log.clear()
        self.rate = None

    def _calculate_rate(self):
        if len(self.change_log) < 2:
            return None
        t0, v0 = self.change_log[0]
        t1, v1 = self.change_log[-1]
        elapsed, delta = t1 - t0, v1 - v0
        if elapsed <= 0 or delta <= 0:
            return None
        return delta / (elapsed / 60)

    @staticmethod
    def _implied_max(current, percent):
        """Range of level-max EXP consistent with 'current' at 'percent' (2-dp rounded).

        Returns None when the percent is too small to say anything useful.
        """
        if percent < 0.5:
            return None
        lo = current * 100 / (percent + 0.005)
        hi = current * 100 / (percent - 0.005)
        slack = 0.002 * hi + 1  # tolerate truncation vs rounding in the game
        return (lo - slack, hi + slack)

    @staticmethod
    def _overlap(a, b):
        return a[0] <= b[1] and b[0] <= a[1]

    def _accept(self, now, current, percent, implied):
        if implied is not None:
            if self.max_range and self._overlap(self.max_range, implied):
                self.max_range = (max(self.max_range[0], implied[0]),
                                  min(self.max_range[1], implied[1]))
            else:
                self.max_range = implied
        self.pending_kind, self.pending_range, self.pending_count = None, None, 0
        msg = None
        if self.prev is not None and current < self.prev:
            # same level but less EXP: died (EXP penalty). Restart the rate.
            self.change_log.clear()
            msg = f"EXP dropped {self.prev:,} → {current:,} — rate restarted"
        elif self.prev is not None:
            self.session_gained += current - self.prev
        if current != self.prev:
            self.change_log.append((now, current))
            self.rate = self._calculate_rate()
            self.gain_log.append((now, self.session_gained))
            self._prune_gain_log(now)
        self.prev = self.current = current
        self.percent = percent
        return msg

    def feed(self, now, current, percent):
        """Process one OCR reading. Returns a log message or None.

        A reading is trusted when its EXP and % agree with the level's max EXP
        learned so far, so a misread digit in either number is rejected. A
        reading that disagrees is only accepted once it's been seen
        CONFIRM_READS times in a row (level-up), or ADOPT_READS times (the
        earlier max was itself wrong).
        """
        msg = None
        self.last_seen = now
        if current is not None and percent is not None:
            implied = self._implied_max(current, percent)
            mx = self.estimated_max
            # A big drop can only be a level-up (death costs ~10% at most).
            level_up = (self.prev is not None and current < self.prev and mx
                        and self.prev - current > 0.3 * mx)
            if not level_up and (implied is None or self.max_range is None
                                 or self._overlap(self.max_range, implied)):
                msg = self._accept(now, current, percent, implied)
            else:
                kind = "lvl" if level_up else "max"
                same = (self.pending_kind == kind and (
                    level_up or (self.pending_range and implied
                                 and self._overlap(self.pending_range, implied))))
                if same:
                    self.pending_count += 1
                    if implied and self.pending_range:
                        self.pending_range = (max(self.pending_range[0], implied[0]),
                                              min(self.pending_range[1], implied[1]))
                    elif implied:
                        self.pending_range = implied
                else:
                    self.pending_kind, self.pending_range, self.pending_count = kind, implied, 1
                need = CONFIRM_READS if level_up else ADOPT_READS
                if self.pending_count >= need:
                    pending = self.pending_range
                    self.max_range, self.change_log = None, deque(maxlen=1000)
                    if level_up:
                        self.levels += 1
                        self.session_gained += max(0, int(round(mx)) - self.prev) + current
                        msg = f"Level up! ({self.prev:,} → {current:,})"
                    else:
                        msg = "Level EXP re-synced"
                    self.prev = None  # gain already counted
                    self._accept(now, current, percent, pending)
                elif self.pending_count == 1 and not level_up:
                    msg = f"Ignoring inconsistent read {current:,} [{percent:.2f}%]"

        # No reset when idle: time without gains counts against both rates.
        return msg

    @property
    def estimated_max(self):
        if self.max_range:
            return sum(self.max_range) / 2
        if self.current and self.percent:
            return self.current / (self.percent / 100)
        return None

    @property
    def eta_minutes(self):
        """Time to level at the recent (rolling) pace, falling back to the
        average while there isn't enough recent history yet."""
        mx = self.estimated_max
        rate = None
        if self.last_seen is not None:
            rate = self.rolling_rate(self.last_seen)
        rate = rate or self.rate
        if mx and rate:
            return (mx - self.current) / rate
        return None


class ConfirmedAmount(RollingGains):
    """A single on-screen amount with no second number to cross-check it
    against (as EXP has its %), so a new value is only accepted after
    MESO_CONFIRM_READS identical reads in a row."""

    def __init__(self):
        self.reset_session()

    def reset_session(self):
        self.current = None
        self.start = None  # first amount read this session
        self.session_gained = 0
        self.gain_log = deque()
        self.pending = None
        self.pending_count = 0
        self.session_start = time.time()
        self.last_seen = None

    def _confirmed(self, now, value, needed=MESO_CONFIRM_READS):
        """True when 'value' is a new amount seen 'needed' times in a row."""
        self.last_seen = now
        if value is None:
            return False  # an unreadable frame neither confirms nor breaks a run
        if value == self.current:
            self.pending, self.pending_count = None, 0
            return False
        if value == self.pending:
            self.pending_count += 1
        else:
            self.pending, self.pending_count = value, 1
        if self.pending_count < needed:
            return False
        self.pending, self.pending_count = None, 0
        return True


class MesoTracker(ConfirmedAmount):
    """Mesos picked up / spent this session, from the inventory's meso amount.

    'session_gained' counts only increases, so buying potions doesn't eat
    into the earning rate; spending is tracked separately.
    """

    def reset_session(self):
        super().reset_session()
        self.spent = 0

    @property
    def net(self):
        return None if self.current is None else self.current - self.start

    def feed(self, now, mesos):
        """Process one reading (None = unreadable). Returns a log message or None."""
        if not self._confirmed(now, mesos):
            return None
        msg = None
        if self.current is None:
            self.start = mesos
        elif mesos > self.current:
            self.session_gained += mesos - self.current
        else:
            self.spent += self.current - mesos
            msg = f"Spent {self.current - mesos:,} mesos"
        self.current = mesos
        self.gain_log.append((now, self.session_gained))
        self._prune_gain_log(now)
        return msg


class PotionTracker(ConfirmedAmount):
    """Potions used this session, from the total count of the watched
    potion stacks in the inventory.

    'session_gained' is potions *used* (count went down), so the rolling
    windows give potions used in the last 1/5/60 minutes. Increases (buying
    or looting potions) are counted in 'restocked' and never offset usage.

    A tooltip or the cursor can hide a whole stack, which reads like dozens
    of potions used at once, so a drop of more than POTION_BIG_DROP needs
    POTION_BIG_DROP_READS identical reads (a real one, e.g. after the
    inventory was closed for a while, is just accepted a few seconds late).
    """

    def __init__(self, name):
        self.name = name  # "HP" / "MP", for log messages
        super().__init__()

    def reset_session(self):
        super().reset_session()
        self.restocked = 0

    def feed(self, now, count):
        """Process one reading (None = unreadable). Returns a log message or None."""
        big = (count is not None and self.current is not None
               and self.current - count > POTION_BIG_DROP)
        if not self._confirmed(now, count, POTION_BIG_DROP_READS if big else MESO_CONFIRM_READS):
            return None
        msg = None
        if self.current is None:
            self.start = count
        elif count < self.current:
            self.session_gained += self.current - count
        else:
            self.restocked += count - self.current
            msg = f"{self.name} potions restocked: +{count - self.current:,}"
        self.current = count
        self.gain_log.append((now, self.session_gained))
        self._prune_gain_log(now)
        return msg


def net_mesos(meso, hp, mp, hp_price, mp_price, now=None, window=None):
    """Mesos picked up minus the cost of potions used.

    For the whole session when 'window' is None, else for the last 'window'
    seconds up to 'now'. Returns None until mesos have been read; a potion
    tracker with nothing read yet costs 0 (that region may be unused).
    """
    def amount(tracker):
        if window is None:
            return tracker.session_gained if tracker.current is not None else None
        got = tracker.gained_in(now, window)
        return None if got is None else got[0]

    picked_up = amount(meso)
    if picked_up is None:
        return None
    hp_used = amount(hp) or 0
    mp_used = amount(mp) or 0
    return picked_up - hp_used * hp_price - mp_used * mp_price


# --------------------------------------------------------------------------- #
# Region selector (drag a box over a frozen screenshot)
# --------------------------------------------------------------------------- #
def select_region(master, on_done, what="the EXP text"):
    """Open a fullscreen drag-to-select overlay. Calls on_done(region_or_None)."""
    with screen_capturer() as sct:
        virtual = sct.monitors[0]  # bounding box of all monitors
        screenshot = grab(sct, virtual)

    top = tk.Toplevel(master)
    top.overrideredirect(True)
    top.attributes("-topmost", True)
    top.geometry(f"{virtual['width']}x{virtual['height']}+{virtual['left']}+{virtual['top']}")

    canvas = tk.Canvas(top, highlightthickness=0, cursor="crosshair")
    canvas.pack(fill=tk.BOTH, expand=True)
    photo = ImageTk.PhotoImage(screenshot)
    canvas.photo = photo  # keep a reference
    canvas.create_image(0, 0, image=photo, anchor="nw")
    hint = canvas.create_text(
        virtual["width"] // 2, 40,
        text=f"Drag a box around {what}  —  Esc / right-click to cancel",
        fill="yellow", font=("Consolas", 20, "bold"),
    )
    state = {"start": None, "rect": None, "done": False}

    def finish(region):
        if state["done"]:
            return
        state["done"] = True
        top.destroy()
        on_done(region)

    def on_press(e):
        state["start"] = (e.x, e.y)
        if state["rect"]:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

    def on_drag(e):
        if state["start"]:
            x0, y0 = state["start"]
            canvas.coords(state["rect"], x0, y0, e.x, e.y)

    def on_release(e):
        if not state["start"]:
            return
        x0, y0 = state["start"]
        region = {
            "left": min(x0, e.x) + virtual["left"],
            "top": min(y0, e.y) + virtual["top"],
            "width": abs(e.x - x0),
            "height": abs(e.y - y0),
        }
        state["start"] = None
        if region_is_valid(region):
            finish(region)
        else:
            canvas.itemconfig(hint, text="Box too small — drag again (Esc to cancel)")

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<ButtonPress-3>", lambda e: finish(None))
    top.bind("<Escape>", lambda e: finish(None))
    top.focus_force()
    top.grab_set()
    return top


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
def fmt_int(v):
    return f"{int(v):,}" if v is not None else "—"


def fmt_minutes(m):
    if m is None:
        return "—"
    m = int(round(m))
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


class App:
    BG = "#15171c"
    FG = "#e6e6e6"
    ACCENT = "#7CFC00"
    DIM = "#8a8f98"

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Maple EXP Tracker")
        # Meso coin icon for the title bar and taskbar (bundled into the exe;
        # next to main.py when run from source)
        icon = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
                            "meso.ico")
        try:
            self.root.iconbitmap(default=icon)
        except tk.TclError:
            pass
        self.root.configure(bg=self.BG)
        self.root.minsize(360, 300)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.tracker = RateTracker()
        self.meso = MesoTracker()
        self.potions = {"hp": PotionTracker("HP"), "mp": PotionTracker("MP")}
        self.region = load_region()
        self.meso_region = load_region("meso")
        self.potion_regions = {k: load_region(k) for k in self.potions}
        self.settings = load_settings()
        self.potion_misses = {k: 0 for k in self.potions}
        self.potion_preview_photos = {}
        self.problems = ProblemLog(PROBLEMS_DIR, grab_full_screen)
        self.paused = False
        self.misses = 0
        self.meso_misses = 0
        self.running = True
        self.results = queue.Queue()  # from OCR worker / hotkeys -> UI thread
        self.preview_photo = None
        self.meso_preview_photo = None

        self._build_ui()
        self._bind_hotkeys()

        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()
        self.root.after(100, self._poll)
        self.root.after(1000, self._tick)

        if not self.region:
            self.set_status("No region set — click “Select EXP”.")

    # ---- UI ------------------------------------------------------------- #
    def _build_ui(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TButton", padding=(8, 4))
        style.configure("TCheckbutton", background=self.BG, foreground=self.FG)
        style.map("TCheckbutton", background=[("active", self.BG)])
        style.configure("Exp.Horizontal.TProgressbar", background=self.ACCENT,
                        troughcolor="#2a2d34", bordercolor=self.BG,
                        lightcolor=self.ACCENT, darkcolor=self.ACCENT)

        pad = {"padx": 10}
        stats = tk.Frame(self.root, bg=self.BG)
        stats.pack(fill=tk.X, pady=(10, 4), **pad)

        self.vars = {}
        rows = [
            ("exp", "EXP"),
            ("percent", "Progress"),
            ("gain_60", "EXP / 1 min"),
            ("gain_300", "EXP / 5 min"),
            ("gain_3600", "EXP / 60 min"),
            ("rate_min", "EXP / min (average)"),
            ("eta", "Time to level"),
            ("gained", "Gained (session)"),
            None,
            ("mesos", "Mesos"),
            ("meso_60", "Mesos / 1 min"),
            ("meso_300", "Mesos / 5 min"),
            ("meso_3600", "Mesos / 60 min"),
            ("meso_gained", "Mesos gained (session)"),
            ("meso_spent", "Mesos spent (session)"),
            None,
            ("pots_used", "HP / MP pots used"),
            ("pot_cost", "Potion cost (session)"),
            ("net_60", "Net mesos / 1 min"),
            ("net_300", "Net mesos / 5 min"),
            ("net_3600", "Net mesos / 60 min"),
            ("net_session", "Net mesos (session)"),
            None,
            ("session", "Session"),
        ]  # None = section break in the Rows menu
        self.row_layout = rows
        self.row_widgets = {}
        self.row_shown = {}
        for i, row in enumerate(rows):
            if row is None:
                continue
            key, label = row
            name = tk.Label(stats, text=label, bg=self.BG, fg=self.DIM, font=("Segoe UI", 10))
            name.grid(row=i, column=0, sticky="w", pady=1)
            var = tk.StringVar(value="—")
            self.vars[key] = var
            big = key in ("exp", "gain_60", "mesos", "meso_60", "net_60")
            value = tk.Label(stats, textvariable=var, bg=self.BG,
                             fg=self.ACCENT if big else self.FG,
                             font=("Consolas", 14 if big else 12, "bold" if big else "normal"))
            value.grid(row=i, column=1, sticky="e", pady=1)
            self._add_hideable(key, (name, value))
        stats.columnconfigure(1, weight=1)

        self.progress = ttk.Progressbar(self.root, maximum=100, style="Exp.Horizontal.TProgressbar")
        self.progress.pack(fill=tk.X, pady=(2, 8), **pad)

        # Previews of what each region sees, on a grid so each one can be
        # hidden and shown again in place (Rows menu).
        previews = tk.Frame(self.root, bg=self.BG)
        previews.pack(fill=tk.X, **pad)

        def preview(key, label, row, col, parent=previews):
            name = tk.Label(parent, text=label, bg=self.BG, fg=self.DIM,
                            font=("Segoe UI", 9))
            name.grid(row=row, column=col, sticky="w", pady=(0, 4))
            img = tk.Label(parent, bg="black", text="(no region)", fg=self.DIM,
                           font=("Segoe UI", 9))
            img.grid(row=row, column=col + 1, sticky="w", padx=(6, 12), pady=(0, 4))
            self._add_hideable(key, (name, img))
            return img

        self.image_rows = [("img_exp", "EXP image"), ("img_meso", "Mesos image"),
                           ("img_hp", "HP pots image"), ("img_mp", "MP pots image")]
        self.preview = preview("img_exp", "EXP:", 0, 0)
        self.meso_preview = preview("img_meso", "Mesos:", 1, 0)
        # HP and MP side by side in their own grid, so hiding one doesn't
        # shift the images above
        pots = tk.Frame(previews, bg=self.BG)
        pots.grid(row=2, column=0, columnspan=2, sticky="w")
        self.potion_previews = {"hp": preview("img_hp", "HP pots:", 0, 0, pots),
                                "mp": preview("img_mp", "MP pots:", 0, 2, pots)}

        btns = tk.Frame(self.root, bg=self.BG)
        btns.pack(fill=tk.X, pady=8, **pad)
        ttk.Button(btns, text="Select EXP", command=self.select_region).pack(side=tk.LEFT)
        ttk.Button(btns, text="Select Mesos", command=self.select_meso_region,
                   ).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(btns, text="Reset", command=self.reset).pack(side=tk.LEFT, padx=4)
        self.pause_btn = ttk.Button(btns, text="Pause", command=self.toggle_pause)
        self.pause_btn.pack(side=tk.LEFT)

        pot_btns = tk.Frame(self.root, bg=self.BG)
        pot_btns.pack(fill=tk.X, pady=(0, 8), **pad)
        self.price_vars = {}
        for kind in self.potions:
            ttk.Button(pot_btns, text=f"Select {kind.upper()} pots",
                       command=lambda k=kind: self.select_potion_region(k),
                       ).pack(side=tk.LEFT, padx=(0, 4))
        for kind in self.potions:
            tk.Label(pot_btns, text=f"{kind.upper()} pot price", bg=self.BG, fg=self.DIM,
                     font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(8, 2))
            var = tk.StringVar(value=f"{self.settings[kind + '_price']:g}")
            var.trace_add("write", lambda *_, k=kind: self._price_changed(k))
            self.price_vars[kind] = var
            tk.Entry(pot_btns, textvariable=var, width=6, bg="#2a2d34", fg=self.FG,
                     insertbackground=self.FG, relief="flat",
                     font=("Consolas", 10)).pack(side=tk.LEFT)

        opts = tk.Frame(self.root, bg=self.BG)
        opts.pack(fill=tk.X, **pad)
        self.on_top = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Always on top", variable=self.on_top,
                        command=self._apply_on_top).pack(side=tk.LEFT)
        self._apply_on_top()
        self.show_log = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Show log", variable=self.show_log,
                        command=self._toggle_log).pack(side=tk.LEFT, padx=(12, 0))
        rows_btn = ttk.Menubutton(opts, text="Rows")
        rows_menu = tk.Menu(rows_btn, tearoff=0)
        for row in self.row_layout:
            if row is None:
                rows_menu.add_separator()
                continue
            key, label = row
            rows_menu.add_checkbutton(label=label, variable=self.row_shown[key],
                                      command=lambda k=key: self._toggle_row(k))
        rows_menu.add_separator()
        for key, label in self.image_rows:
            rows_menu.add_checkbutton(label=label, variable=self.row_shown[key],
                                      command=lambda k=key: self._toggle_row(k))
        rows_menu.add_separator()
        rows_menu.add_command(label="Show all", command=self._show_all_rows)
        rows_btn["menu"] = rows_menu
        rows_btn.pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(opts, text="Problem snapshots", command=self.open_problems_folder,
                   ).pack(side=tk.LEFT, padx=(8, 0))

        self.status = tk.StringVar(value="Starting…")
        tk.Label(self.root, textvariable=self.status, bg=self.BG, fg=self.DIM,
                 font=("Segoe UI", 9), anchor="w", wraplength=380, justify="left",
                 ).pack(fill=tk.X, pady=(6, 8), **pad)

        # Log panel (hidden until "Show log" is ticked)
        self.log_frame = tk.Frame(self.root, bg=self.BG)
        self.log_text = tk.Text(self.log_frame, height=10, bg="#0d0e11", fg=self.FG,
                                font=("Consolas", 9), relief="flat", wrap="word",
                                state="disabled")
        scroll = ttk.Scrollbar(self.log_frame, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._last_log_key = None
        self._repeat = 0

        tk.Label(self.root, text="F1 reset · F2 hide/show", bg=self.BG, fg="#555",
                 font=("Segoe UI", 8)).pack(side=tk.BOTTOM, pady=(0, 4))

    def _add_hideable(self, key, widgets):
        """Register gridded widgets that the Rows menu shows / hides as one."""
        self.row_widgets[key] = widgets
        shown = key not in self.settings["hidden_rows"]
        self.row_shown[key] = tk.BooleanVar(value=shown)
        if not shown:
            for w in widgets:
                w.grid_remove()

    def _toggle_row(self, key):
        for w in self.row_widgets[key]:
            if self.row_shown[key].get():
                w.grid()
            else:
                w.grid_remove()
        self.settings["hidden_rows"] = [k for k, v in self.row_shown.items() if not v.get()]
        try:
            save_settings(self.settings)
        except OSError as e:
            self.set_status(f"Couldn't save row choices: {e}")
        self.root.geometry("")  # shrink / grow to fit

    def _show_all_rows(self):
        for key, shown in self.row_shown.items():
            if not shown.get():
                shown.set(True)
                self._toggle_row(key)

    def _toggle_log(self):
        if self.show_log.get():
            self.log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))
        else:
            self.log_frame.pack_forget()
            self.root.geometry("")  # shrink back

    def log(self, text, key=None):
        """Append a line to the log. Lines with the same key collapse into a
        single updating line, so a repeated failure doesn't flood the log."""
        stamp = time.strftime("%H:%M:%S")
        t = self.log_text
        t.config(state="normal")
        if key is not None and key == self._last_log_key:
            self._repeat += 1
            t.delete("end-2l", "end-1l")
            t.insert("end", f"{stamp}  {text}  (×{self._repeat})\n")
        else:
            self._last_log_key, self._repeat = key, 1
            t.insert("end", f"{stamp}  {text}\n")
            lines = int(t.index("end-1c").split(".")[0])
            if lines > 500:
                t.delete("1.0", f"{lines - 500}.0")
        t.see("end")
        t.config(state="disabled")

    def _apply_on_top(self):
        self.root.attributes("-topmost", self.on_top.get())

    def _bind_hotkeys(self):
        if keyboard is None:
            return
        try:
            keyboard.add_hotkey("f1", lambda: self.results.put(("hotkey", "reset")))
            keyboard.add_hotkey("f2", lambda: self.results.put(("hotkey", "toggle")))
        except Exception as e:
            self.set_status(f"Hotkeys unavailable: {e}")

    def set_status(self, text):
        self.status.set(f"{time.strftime('%H:%M:%S')}  {text}")

    # ---- actions -------------------------------------------------------- #
    def select_region(self):
        self.root.withdraw()
        # give the window time to disappear before the screenshot is taken
        self.root.after(250, lambda: select_region(self.root, self._region_chosen))

    def select_meso_region(self):
        self.root.withdraw()
        self.root.after(250, lambda: select_region(
            self.root, self._meso_region_chosen,
            "the meso amount in your inventory (keep the inventory open)"))

    def select_potion_region(self, kind):
        self.root.withdraw()
        self.root.after(250, lambda: select_region(
            self.root, lambda region: self._potion_region_chosen(kind, region),
            f"your {kind.upper()} potion slots (only potions; inventory open on the Use tab)"))

    def _potion_region_chosen(self, kind, region):
        self.root.deiconify()
        self._apply_on_top()
        if region:
            self.potion_regions[kind] = region
            save_region(region, kind)
            self.potions[kind].reset_session()
            self.set_status(f"{kind.upper()} potion region set: {region['width']}×"
                            f"{region['height']} at ({region['left']}, {region['top']})")
            self._refresh()
        else:
            self.set_status(f"{kind.upper()} potion region selection cancelled.")

    def _price_changed(self, kind):
        text = self.price_vars[kind].get().strip().replace(",", "")
        try:
            price = float(text) if text else 0
        except ValueError:
            return  # keep the last valid price while typing
        if price < 0:
            return
        self.settings[kind + "_price"] = price
        try:
            save_settings(self.settings)
        except OSError as e:
            self.set_status(f"Couldn't save prices: {e}")
        self._refresh()

    def _meso_region_chosen(self, region):
        self.root.deiconify()
        self._apply_on_top()
        if region:
            self.meso_region = region
            save_region(region, "meso")
            self.meso.reset_session()
            self.set_status(f"Meso region set: {region['width']}×{region['height']} "
                            f"at ({region['left']}, {region['top']})")
            self._refresh()
        else:
            self.set_status("Meso region selection cancelled.")

    def _region_chosen(self, region):
        self.root.deiconify()
        self._apply_on_top()
        if region:
            self.region = region
            save_region(region)
            self.tracker.reset_session()
            self.set_status(f"Region set: {region['width']}×{region['height']} "
                            f"at ({region['left']}, {region['top']})")
        else:
            self.set_status("Region selection cancelled.")

    def reset(self):
        self.tracker.reset_session()
        self.meso.reset_session()
        for p in self.potions.values():
            p.reset_session()
        self.set_status("Session reset.")
        self._refresh()

    def toggle_pause(self):
        self.paused = not self.paused
        self.pause_btn.config(text="Resume" if self.paused else "Pause")
        self.set_status("Paused." if self.paused else "Resumed.")

    def toggle_visibility(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
        else:
            self.root.withdraw()

    def quit(self):
        self.running = False
        if keyboard is not None:
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass
        self.root.destroy()

    # ---- background OCR ------------------------------------------------- #
    def _worker_loop(self):
        with screen_capturer() as sct:  # capture objects are per-thread
            screen = sct.monitors[0]
            while self.running:
                start = time.time()
                region = self.region
                if region and not self.paused:
                    try:
                        img = grab(sct, padded(region, screen))
                        current, percent, how = extract_number(img)
                        self.results.put(("reading", (time.time(), current, percent, how, img)))
                    except Exception as e:
                        self.results.put(("error", str(e)))
                meso_region = self.meso_region
                if meso_region and not self.paused:
                    try:
                        img = grab(sct, padded(meso_region, screen))
                        mesos, how = extract_mesos(img)
                        self.results.put(("meso", (time.time(), mesos, how, img)))
                    except Exception as e:
                        self.results.put(("error", str(e)))
                for kind, pot_region in list(self.potion_regions.items()):
                    if pot_region and not self.paused:
                        try:
                            # exact region: padding would pull in neighbouring slots
                            img = grab(sct, pot_region)
                            total, how = extract_potions(img)
                            self.results.put(("potion", (kind, time.time(), total, how, img)))
                        except Exception as e:
                            self.results.put(("error", str(e)))
                time.sleep(max(0.05, POLL_RATE - (time.time() - start)))

    def _poll(self):
        try:
            while True:
                kind, payload = self.results.get_nowait()
                if kind == "reading":
                    now, current, percent, how, img = payload
                    before = (self.tracker.current, self.tracker.percent)
                    msg = self.tracker.feed(now, current, percent)
                    if current is None:
                        self.misses += 1
                        self.log(f"can't read: {how}", key=how)
                        self._snapshot("exp", how, img, self.region)
                        if self.misses == 6:  # ~3s of failed reads
                            self.set_status(f"Can't read EXP — {how}. "
                                            "Use Select Region to fix.")
                    else:
                        if self.misses >= 6:
                            self.set_status("Reading EXP.")
                        self.misses = 0
                        after = (self.tracker.current, self.tracker.percent)
                        if after != before:
                            rate = self.tracker.rate
                            parts = []
                            for w, name in ((60, "1m"), (300, "5m")):
                                got = self.tracker.gained_in(now, w)
                                if got:
                                    parts.append(f"{name} +{got[0]:,}")
                            self.log(f"read {current:,} [{percent:.2f}%]"
                                     + "".join(f"  {p}" for p in parts)
                                     + (f"  avg {rate:,.0f}/min" if rate else ""))
                        elif (current, percent) != after:
                            self.log(f"ignored {current:,} [{percent:.2f}%] "
                                     f"(doesn't match level EXP)", key="ignored")
                            self._snapshot("exp", "reading doesn't match level EXP", img,
                                           self.region,
                                           f"read {current:,} [{percent:.2f}%], "
                                           f"showing {fmt_int(after[0])} [{after[1] or 0:.2f}%]")
                    if msg:
                        self.set_status(msg)
                        self.log(msg)
                    self._show_preview(img)
                    self._refresh()
                elif kind == "meso":
                    self._on_meso(*payload)
                elif kind == "potion":
                    self._on_potion(*payload)
                elif kind == "error":
                    self.set_status(f"Error: {payload}")
                    self.log(f"error: {payload}", key="error")
                elif kind == "hotkey":
                    if payload == "reset":
                        self.tracker.reset_rate()
                        self.set_status("Rate reset (F1).")
                        self._refresh()
                    elif payload == "toggle":
                        self.toggle_visibility()
        except queue.Empty:
            pass
        if self.running:
            self.root.after(100, self._poll)

    def _tick(self):
        if not self.running:
            return
        elapsed = (time.time() - self.tracker.session_start) / 60
        self.vars["session"].set(fmt_minutes(elapsed) +
                                 (f" · {self.tracker.levels} lvl" if self.tracker.levels else ""))
        self._refresh_recent()  # keeps decaying even if readings stop
        self.root.after(1000, self._tick)

    @staticmethod
    def _preview_image(img, max_w=300):
        scale = min(2.0, max_w / img.width)
        shown = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
        return ImageTk.PhotoImage(shown)

    def _snapshot(self, kind, reason, img, region, detail=None):
        """Save the problem's image for later diagnosis (only while the game
        is the focused window; throttled per problem)."""
        try:
            path = self.problems.record(kind, reason, img, region, detail)
        except Exception as e:
            self.log(f"couldn't save problem snapshot: {e}", key="snapshot-error")
            return
        if path:
            self.log(f"saved problem snapshot {os.path.basename(path)}")

    def open_problems_folder(self):
        os.makedirs(PROBLEMS_DIR, exist_ok=True)
        try:
            os.startfile(PROBLEMS_DIR)
        except Exception as e:
            self.set_status(f"Couldn't open {PROBLEMS_DIR}: {e}")

    def _show_preview(self, img):
        self.preview_photo = self._preview_image(img)
        self.preview.config(image=self.preview_photo, text="")

    def _on_meso(self, now, mesos, how, img):
        m = self.meso
        before = m.current
        msg = m.feed(now, mesos)
        if mesos is None:
            self.meso_misses += 1
            self.log(f"can't read mesos: {how}", key="meso:" + how)
            self._snapshot("meso", how, img, self.meso_region)
            if self.meso_misses == 6:  # ~3s of failed reads
                self.set_status(f"Can't read mesos — {how}. Keep the inventory "
                                "open, or use Select Mesos to fix.")
        else:
            if self.meso_misses >= 6:
                self.set_status("Reading mesos.")
            self.meso_misses = 0
            if m.current != before and before is not None and m.current > before:
                got = m.gained_in(now, 60)
                self.log(f"mesos {m.current:,} (+{m.current - before:,})"
                         + (f"  1m +{got[0]:,}" if got else ""))
            elif m.current != before and before is None:
                self.log(f"mesos {m.current:,}")
        if msg:
            self.set_status(msg)
            self.log(msg)
        self.meso_preview_photo = self._preview_image(img)
        self.meso_preview.config(image=self.meso_preview_photo, text="")
        self._refresh()

    def _on_potion(self, kind, now, total, how, img):
        p = self.potions[kind]
        name = kind.upper()
        before = p.current
        msg = p.feed(now, total)
        if total is None:
            self.potion_misses[kind] += 1
            self.log(f"can't read {name} pots: {how}", key=f"{kind}:{how}")
            self._snapshot(kind, how, img, self.potion_regions[kind])
            if self.potion_misses[kind] == 6:  # ~3s of failed reads
                self.set_status(f"Can't read {name} potions — {how}. Keep the inventory "
                                f"open on the Use tab, or use Select {name} pots to fix.")
        else:
            if self.potion_misses[kind] >= 6:
                self.set_status(f"Reading {name} potions.")
            self.potion_misses[kind] = 0
            if before is None and p.current is not None:
                self.log(f"{name} pots {p.current:,}")
            elif p.current is not None and p.current < before:
                self.log(f"{name} pots {p.current:,} (used {before - p.current:,}, "
                         f"session {p.session_gained:,})")
            elif p.pending is not None and p.pending_count == 1 and before is not None \
                    and before - p.pending > POTION_BIG_DROP:
                self.log(f"{name} pots read {p.pending:,} — waiting to confirm a big drop",
                         key=f"{kind}:bigdrop")
                self._snapshot(kind, "big drop in potion count", img, self.potion_regions[kind],
                               f"{before:,} -> {p.pending:,}")
        if msg:
            self.set_status(msg)
            self.log(msg)
        self.potion_preview_photos[kind] = self._preview_image(img, max_w=140)
        self.potion_previews[kind].config(image=self.potion_preview_photos[kind], text="")
        self._refresh()

    def _potion_cost(self, used_hp, used_mp):
        return used_hp * self.settings["hp_price"] + used_mp * self.settings["mp_price"]

    def _refresh(self):
        t = self.tracker
        self.vars["exp"].set(fmt_int(t.current))
        self.vars["percent"].set(f"{t.percent:.2f}%" if t.percent is not None else "—")
        self.progress["value"] = t.percent or 0
        self.vars["rate_min"].set(fmt_int(t.rate))
        self._refresh_recent()
        self.vars["eta"].set(fmt_minutes(t.eta_minutes))
        self.vars["gained"].set(fmt_int(t.session_gained))
        m = self.meso
        self.vars["mesos"].set(fmt_int(m.current))
        self.vars["meso_gained"].set(fmt_int(m.session_gained) if m.current is not None else "—")
        self.vars["meso_spent"].set(fmt_int(m.spent) if m.current is not None else "—")
        hp, mp = self.potions["hp"], self.potions["mp"]
        if hp.current is None and mp.current is None:
            self.vars["pots_used"].set("—")
            self.vars["pot_cost"].set("—")
        else:
            self.vars["pots_used"].set(
                f"{fmt_int(hp.session_gained if hp.current is not None else None)} / "
                f"{fmt_int(mp.session_gained if mp.current is not None else None)}")
            self.vars["pot_cost"].set(fmt_int(self._potion_cost(hp.session_gained,
                                                                mp.session_gained)))
        self.vars["net_session"].set(fmt_int(net_mesos(
            m, hp, mp, self.settings["hp_price"], self.settings["mp_price"])))

    def _refresh_recent(self):
        now = time.time()
        for prefix, tracker in (("gain", self.tracker), ("meso", self.meso)):
            for w in ROLLING_WINDOWS:
                self.vars[f"{prefix}_{w}"].set(self._recent_text(tracker, now, w))
        for w in ROLLING_WINDOWS:
            self.vars[f"net_{w}"].set(self._net_text(now, w))

    def _net_text(self, now, w):
        got = None if self.paused else self.meso.gained_in(now, w)
        if got is None:
            return "—"
        net = net_mesos(self.meso, self.potions["hp"], self.potions["mp"],
                        self.settings["hp_price"], self.settings["mp_price"], now, w)
        text = f"{int(net):,}"
        span = got[1]
        if span < w - 1:
            text += f" ({int(span)}s)" if span < 60 else f" ({fmt_minutes(span // 60)})"
        return text

    def _recent_text(self, tracker, now, w):
        got = None if self.paused else tracker.gained_in(now, w)
        if got is None:
            return "—"
        gained, span = got
        text = fmt_int(gained)
        if span < w - 1:  # window not full yet: say how much it covers
            covered = (f"{int(span)}s" if span < 60
                       else fmt_minutes(span // 60))
            text += f" ({covered})"
        return text

    def run(self):
        self.root.mainloop()


def main():
    # Make Tk and mss agree on physical pixel coordinates under Windows scaling.
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    App().run()


if __name__ == "__main__":
    main()
