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

POLL_RATE = 0.5  # seconds between screen reads
CONFIRM_READS = 3  # matching reads needed to accept a level-up
ADOPT_READS = 20  # matching reads (~10s) needed to replace a wrong level-max estimate
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


def load_region():
    for path in (REGION_FILE, LEGACY_REGION_FILE):
        try:
            with open(path, "r") as f:
                region = json.load(f)
        except (OSError, ValueError):
            continue
        if region_is_valid(region):
            return region
    return None


def save_region(region):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(REGION_FILE, "w") as f:
        json.dump(region, f)


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


def extract_number(pil_img):
    """Return (exp, percent, status) read from an image of the EXP text."""
    return expocr.read(np.asarray(pil_img))


# --------------------------------------------------------------------------- #
# Rate logic (no UI, so it can be tested on its own)
# --------------------------------------------------------------------------- #
class RateTracker:
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

    def _prune_gain_log(self, now):
        # keep one sample at/before the longest window's start: it's the EXP
        # total at that moment
        oldest = now - max(ROLLING_WINDOWS)
        while len(self.gain_log) >= 2 and self.gain_log[1][0] <= oldest:
            self.gain_log.popleft()

    def gained_in(self, now, window):
        """EXP gained in the last 'window' seconds up to 'now'.

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
            if t <= start:  # EXP total at the window start
                base_t, base_g = start, g
                break
        return max(0, self.session_gained - base_g), min(now - base_t, window)

    def rolling_rate(self, now, window=ROLLING_WINDOW):
        """EXP per minute over the last 'window' seconds, up to 'now'."""
        got = self.gained_in(now, window)
        if got is None or got[1] < ROLLING_MIN_SPAN:
            return None
        gained, span = got
        return gained / (span / 60)

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


# --------------------------------------------------------------------------- #
# Region selector (drag a box over a frozen screenshot)
# --------------------------------------------------------------------------- #
def select_region(master, on_done):
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
        text="Drag a box around the EXP text  —  Esc / right-click to cancel",
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
        self.root.configure(bg=self.BG)
        self.root.minsize(320, 300)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.tracker = RateTracker()
        self.region = load_region()
        self.paused = False
        self.misses = 0
        self.running = True
        self.results = queue.Queue()  # from OCR worker / hotkeys -> UI thread
        self.preview_photo = None

        self._build_ui()
        self._bind_hotkeys()

        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()
        self.root.after(100, self._poll)
        self.root.after(1000, self._tick)

        if not self.region:
            self.set_status("No region set — click “Select Region”.")

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
            ("session", "Session"),
        ]
        for i, (key, label) in enumerate(rows):
            tk.Label(stats, text=label, bg=self.BG, fg=self.DIM,
                     font=("Segoe UI", 10)).grid(row=i, column=0, sticky="w", pady=1)
            var = tk.StringVar(value="—")
            self.vars[key] = var
            big = key in ("exp", "gain_60")
            tk.Label(stats, textvariable=var, bg=self.BG,
                     fg=self.ACCENT if big else self.FG,
                     font=("Consolas", 14 if big else 12, "bold" if big else "normal"),
                     ).grid(row=i, column=1, sticky="e", pady=1)
        stats.columnconfigure(1, weight=1)

        self.progress = ttk.Progressbar(self.root, maximum=100, style="Exp.Horizontal.TProgressbar")
        self.progress.pack(fill=tk.X, pady=(2, 8), **pad)

        prev_frame = tk.Frame(self.root, bg=self.BG)
        prev_frame.pack(fill=tk.X, **pad)
        tk.Label(prev_frame, text="Watching:", bg=self.BG, fg=self.DIM,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.preview = tk.Label(prev_frame, bg="black", text="(no region)",
                                fg=self.DIM, font=("Segoe UI", 9))
        self.preview.pack(side=tk.LEFT, padx=6)

        btns = tk.Frame(self.root, bg=self.BG)
        btns.pack(fill=tk.X, pady=8, **pad)
        ttk.Button(btns, text="Select Region", command=self.select_region).pack(side=tk.LEFT)
        ttk.Button(btns, text="Reset", command=self.reset).pack(side=tk.LEFT, padx=4)
        self.pause_btn = ttk.Button(btns, text="Pause", command=self.toggle_pause)
        self.pause_btn.pack(side=tk.LEFT)

        opts = tk.Frame(self.root, bg=self.BG)
        opts.pack(fill=tk.X, **pad)
        self.on_top = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Always on top", variable=self.on_top,
                        command=self._apply_on_top).pack(side=tk.LEFT)
        self._apply_on_top()
        self.show_log = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Show log", variable=self.show_log,
                        command=self._toggle_log).pack(side=tk.LEFT, padx=(12, 0))

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
                    if msg:
                        self.set_status(msg)
                        self.log(msg)
                    self._show_preview(img)
                    self._refresh()
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

    def _show_preview(self, img):
        max_w = 300
        scale = min(2.0, max_w / img.width)
        shown = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
        self.preview_photo = ImageTk.PhotoImage(shown)
        self.preview.config(image=self.preview_photo, text="")

    def _refresh(self):
        t = self.tracker
        self.vars["exp"].set(fmt_int(t.current))
        self.vars["percent"].set(f"{t.percent:.2f}%" if t.percent is not None else "—")
        self.progress["value"] = t.percent or 0
        self.vars["rate_min"].set(fmt_int(t.rate))
        self._refresh_recent()
        self.vars["eta"].set(fmt_minutes(t.eta_minutes))
        self.vars["gained"].set(fmt_int(t.session_gained))

    def _refresh_recent(self):
        now = time.time()
        for w in ROLLING_WINDOWS:
            got = None if self.paused else self.tracker.gained_in(now, w)
            if got is None:
                text = "—"
            else:
                gained, span = got
                text = fmt_int(gained)
                if span < w - 1:  # window not full yet: say how much it covers
                    covered = (f"{int(span)}s" if span < 60
                               else fmt_minutes(span // 60))
                    text += f" ({covered})"
            self.vars[f"gain_{w}"].set(text)

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
