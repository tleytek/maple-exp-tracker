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
CONFIRM_READS = 3  # identical reads needed to accept a big jump (e.g. level-up)
IDLE_RESET_SECONDS = 60  # rate window resets after this long with no EXP change
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
        self.last_change_time = time.time()
        self.pending = None
        self.pending_count = 0
        self.session_start = time.time()
        self.session_gained = 0
        self.levels = 0

    def reset_rate(self):
        self.change_log.clear()
        self.rate = None
        self.last_change_time = time.time()

    def _calculate_rate(self):
        if len(self.change_log) < 2:
            return None
        t0, v0 = self.change_log[0]
        t1, v1 = self.change_log[-1]
        elapsed, delta = t1 - t0, v1 - v0
        if elapsed <= 0 or delta <= 0:
            return None
        return delta / (elapsed / 60)

    def feed(self, now, current, percent):
        """Process one OCR reading. Returns a log message or None."""
        msg = None
        if current is not None and current != self.prev:
            accept = True
            if self.prev is not None and abs(current - self.prev) > 0.2 * max(self.prev, 1):
                # OCR misread or real level-up: require CONFIRM_READS identical reads.
                if current == self.pending:
                    self.pending_count += 1
                else:
                    self.pending, self.pending_count = current, 1
                if self.pending_count < CONFIRM_READS:
                    accept = False
                    msg = f"Ignoring jump {self.prev:,} → {current:,} (unconfirmed)"
                else:
                    if current < self.prev:
                        self.levels += 1
                        self.session_gained += current
                        msg = f"Level up! ({self.prev:,} → {current:,})"
                    else:
                        self.session_gained += current - self.prev
                    self.change_log.clear()
            elif self.prev is not None and current > self.prev:
                self.session_gained += current - self.prev
            if accept:
                self.pending, self.pending_count = None, 0
                self.change_log.append((now, current))
                self.rate = self._calculate_rate()
                self.last_change_time = now
                self.prev = self.current = current
                self.percent = percent
        elif current is not None and percent is not None:
            self.percent = percent

        if now - self.last_change_time > IDLE_RESET_SECONDS and self.change_log:
            self.change_log.clear()
            self.rate = None
            self.last_change_time = now
            msg = f"No EXP change for {IDLE_RESET_SECONDS}s — rate reset"
        return msg

    @property
    def estimated_max(self):
        if self.current and self.percent:
            return self.current / (self.percent / 100)
        return None

    @property
    def eta_minutes(self):
        mx = self.estimated_max
        if mx and self.rate:
            return (mx - self.current) / self.rate
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
            ("rate_min", "EXP / min"),
            ("rate_hour", "EXP / hour"),
            ("eta", "Time to level"),
            ("gained", "Gained (session)"),
            ("session", "Session"),
        ]
        for i, (key, label) in enumerate(rows):
            tk.Label(stats, text=label, bg=self.BG, fg=self.DIM,
                     font=("Segoe UI", 10)).grid(row=i, column=0, sticky="w", pady=1)
            var = tk.StringVar(value="—")
            self.vars[key] = var
            big = key in ("exp", "rate_min")
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

        self.status = tk.StringVar(value="Starting…")
        tk.Label(self.root, textvariable=self.status, bg=self.BG, fg=self.DIM,
                 font=("Segoe UI", 9), anchor="w", wraplength=380, justify="left",
                 ).pack(fill=tk.X, pady=(6, 8), **pad)
        tk.Label(self.root, text="F1 reset · F2 hide/show", bg=self.BG, fg="#555",
                 font=("Segoe UI", 8)).pack(side=tk.BOTTOM, pady=(0, 4))

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
            while self.running:
                start = time.time()
                region = self.region
                if region and not self.paused:
                    try:
                        img = grab(sct, region)
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
                    msg = self.tracker.feed(now, current, percent)
                    if current is None:
                        self.misses += 1
                        if self.misses == 6:  # ~3s of failed reads
                            self.set_status("Can't read EXP — check the region "
                                            "(include both green [ ] brackets).")
                    else:
                        if self.misses >= 6:
                            self.set_status("Reading EXP.")
                        self.misses = 0
                    if msg:
                        self.set_status(msg)
                    self._show_preview(img)
                    self._refresh()
                elif kind == "error":
                    self.set_status(f"Error: {payload}")
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
        self.vars["rate_hour"].set(fmt_int(t.rate * 60) if t.rate else "—")
        self.vars["eta"].set(fmt_minutes(t.eta_minutes))
        self.vars["gained"].set(fmt_int(t.session_gained))

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
