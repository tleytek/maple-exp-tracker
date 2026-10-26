"""MapleStory kill tracker (packet based).

A separate exe from the EXP tracker: it passively watches the game's network
traffic through Wireshark's tshark and counts the mobs you kill. Read-only,
nothing is ever sent to the game, but sniffing tools are against the game's
ToS, which is why it lives in its own exe.

Needs Wireshark (with Npcap) installed at the default path.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import Counter, deque
from tkinter import ttk

import maplepackets
from killparse import KillCounter

DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "MapleKillTracker")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
GAME_EXE = "MapleStory.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_SETTINGS = {
    "iface": "",  # "" = find the game's network adapter automatically
    "mob_names": {},  # mob type number (as text) -> name typed by the user
    "hidden_rows": [],
    "on_top": True,
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k in s and type(v) is type(s[k]):
                s[k] = v
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


# ---- finding the game's network adapter ---------------------------------- #
def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          creationflags=NO_WINDOW).stdout


def game_local_ip():
    """(local IP of the game's server connection, reason if not found)."""
    out = _run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}", "/FO", "CSV", "/NH"])
    pids = set(re.findall(r'^"[^"]+","(\d+)"', out, re.M))
    if not pids:
        return None, f"{GAME_EXE} is not running"
    for line in _run(["netstat", "-ano", "-p", "TCP"]).splitlines():
        f = line.split()
        if len(f) == 5 and f[3] == "ESTABLISHED" and f[4] in pids:
            local, remote = f[1], f[2]
            if remote.rsplit(":", 1)[1] not in ("443", "80"):
                return local.rsplit(":", 1)[0], None
    return None, "the game has no server connection yet (log in first)"


def adapter_for_ip(ip):
    """Windows adapter name ("Ethernet 7") holding this IPv4 address, from ipconfig."""
    name = None
    for line in _run(["ipconfig"]).splitlines():
        m = re.match(r"^\S.* adapter (.+):\s*$", line)
        if m:
            name = m.group(1)
        elif name and "IPv4" in line and line.strip().endswith(ip):
            return name
    return None


def find_game_iface():
    ip, why = game_local_ip()
    if not ip:
        return None, why
    name = adapter_for_ip(ip)
    return (name, None) if name else (None, f"no adapter found holding {ip}")


# ---- session stats -------------------------------------------------------- #
class KillStats:
    """Kill counts and rates for one session. Paused time is not session time."""

    def __init__(self, now=None):
        self.reset(now)

    def reset(self, now=None):
        now = time.time() if now is None else now
        self.session_start = now
        self.paused_total = 0.0
        self.paused_since = None
        self.total = 0
        self.by_mob = Counter()
        self.recent = deque()  # kill times in the last 60 s

    def pause(self, now):
        if self.paused_since is None:
            self.paused_since = now

    def resume(self, now):
        if self.paused_since is not None:
            self.paused_total += now - self.paused_since
            self.paused_since = None

    def session_seconds(self, now):
        paused = self.paused_total + (now - self.paused_since if self.paused_since is not None else 0)
        return max(0.0, now - self.session_start - paused)

    def add(self, now, mob):
        if self.paused_since is not None:
            return False
        self.total += 1
        self.by_mob[mob] += 1
        self.recent.append(now)
        return True

    def per_minute(self, now, count=None):
        """Session average kills per minute (None for the first 10 s of session time)."""
        secs = self.session_seconds(now)
        if secs < 10:
            return None
        return (self.total if count is None else count) * 60 / secs

    def last_minute(self, now):
        while self.recent and self.recent[0] < now - 60:
            self.recent.popleft()
        return len(self.recent)


def fmt_duration(secs):
    m = int(secs // 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {int(secs) % 60:02d}s"


def fmt_rate(v):
    return "—" if v is None else (f"{v:,.1f}" if v < 100 else f"{v:,.0f}")


# ---- capture worker ------------------------------------------------------- #
class Capture:
    """Background thread: tshark -> decoder -> KillCounter -> UI queue."""

    def __init__(self, out, iface_setting):
        self.out = out
        self.iface_setting = iface_setting
        self.counter = KillCounter()
        self.proc = None
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def log(self, msg):
        self.out.put(("log", msg))

    def stop(self):
        self.stop_flag.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _run(self):
        while not self.stop_flag.is_set():
            if not maplepackets.tshark_available():
                self.out.put(("status", "Wireshark is not installed (tshark.exe not found at "
                              f"{maplepackets.TSHARK}). Install Wireshark with Npcap, then restart."))
                return
            iface = self.iface_setting
            if not iface:
                iface, why = find_game_iface()
                if not iface:
                    self.out.put(("status", f"Waiting: {why}. Retrying every 5 s."))
                    self.stop_flag.wait(5)
                    continue
            self.out.put(("status", f"Watching {iface}. Kill something: the first kill confirms "
                          "which character is yours."))
            self.log(f"capturing on {iface}")
            started = time.time()
            try:
                self.proc = maplepackets.open_tshark(iface=iface)
            except OSError as e:
                self.out.put(("status", f"Could not start tshark: {e}"))
                return
            for ts, _key, p in maplepackets.decode_lines(self.proc.stdout, log=self.log):
                ev = self.counter.feed(ts, p)
                if ev:
                    self.out.put(("kill", ev))
            if self.stop_flag.is_set():
                return
            err = (self.proc.stderr.read() or "").strip().splitlines()
            reason = err[-1] if err else "tshark exited"
            self.log(f"capture stopped: {reason}")
            self.out.put(("status", f"Capture stopped ({reason}). Restarting in 5 s."))
            if time.time() - started < 5:
                self.stop_flag.wait(5)


# ---- window ------------------------------------------------------------- #
class App:
    BG = "#15171c"
    FG = "#e6e6e6"
    ACCENT = "#7CFC00"
    DIM = "#8a8f98"

    ROWS = [("kills", "Kills"), ("per_min", "Kills / min"), ("per_hour", "Kills / hour"),
            ("last_min", "Kills last 60 s"), ("session", "Session time")]

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Maple Kill Tracker")
        icon = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
                            "meso.ico")
        try:
            self.root.iconbitmap(default=icon)
        except tk.TclError:
            pass
        self.root.configure(bg=self.BG)
        self.root.minsize(300, 150)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.settings = load_settings()
        self.stats = KillStats()
        self.paused = False
        self.events = queue.Queue()
        self._build_ui()
        self.capture = Capture(self.events, self.settings["iface"])
        self.root.after(100, self._poll)
        self.root.after(1000, self._tick)

    def _build_ui(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TButton", padding=(8, 4))
        style.configure("TCheckbutton", background=self.BG, foreground=self.FG)
        style.map("TCheckbutton", background=[("active", self.BG)])
        style.configure("TMenubutton", padding=(8, 4))
        pad = {"padx": 10}

        stats = tk.Frame(self.root, bg=self.BG)
        stats.pack(fill=tk.X, pady=(10, 4), **pad)
        stats.columnconfigure(1, weight=1)
        self.vars, self.row_widgets = {}, {}
        hidden = set(self.settings["hidden_rows"])
        self.row_shown = {}
        for i, (key, label) in enumerate(self.ROWS):
            var = self.vars[key] = tk.StringVar(value="—")
            a = tk.Label(stats, text=label, bg=self.BG, fg=self.DIM, font=("Segoe UI", 10), anchor="w")
            b = tk.Label(stats, textvariable=var, bg=self.BG, fg=self.ACCENT if key == "kills" else self.FG,
                         font=("Consolas", 13 if key == "kills" else 11, "bold"), anchor="e")
            a.grid(row=i, column=0, sticky="w")
            b.grid(row=i, column=1, sticky="e")
            self.row_widgets[key] = (a, b)
            self.row_shown[key] = tk.BooleanVar(value=key not in hidden)

        # Per-mob breakdown: name (editable) | kills | kills/hour
        self.mob_frame = tk.Frame(self.root, bg=self.BG)
        self.mob_frame.pack(fill=tk.X, pady=(6, 0), **pad)
        self.mob_frame.columnconfigure(0, weight=1)
        self.mob_rows = {}  # mob type -> (name var, kills var, rate var)
        self.mob_hint = tk.Label(self.mob_frame, text="Per-mob kills appear here. Type a name over "
                                 "“Mob #N” to label it.", bg=self.BG, fg=self.DIM,
                                 font=("Segoe UI", 9), anchor="w", wraplength=360, justify="left")
        self.mob_hint.grid(row=0, column=0, columnspan=3, sticky="w")
        self.row_shown["mobs"] = tk.BooleanVar(value="mobs" not in hidden)

        ctl = self.ctl_frame = tk.Frame(self.root, bg=self.BG)
        ctl.pack(fill=tk.X, pady=8, **pad)
        for c in range(2):
            ctl.columnconfigure(c, weight=1, uniform="ctl")
        g = {"sticky": "ew", "padx": 2, "pady": 2}
        ttk.Button(ctl, text="Reset", command=self.reset).grid(row=0, column=0, **g)
        self.pause_btn = ttk.Button(ctl, text="Pause", command=self.toggle_pause)
        self.pause_btn.grid(row=0, column=1, **g)

        rows_btn = ttk.Menubutton(ctl, text="Rows")
        menu = tk.Menu(rows_btn, tearoff=0)
        for key, label in self.ROWS + [("mobs", "Per-mob breakdown")]:
            menu.add_checkbutton(label=label, variable=self.row_shown[key], command=self._apply_rows)
        menu.add_separator()
        menu.add_command(label="Show all", command=self._show_all_rows)
        rows_btn["menu"] = menu
        rows_btn.grid(row=1, column=0, **g)

        cell = tk.Frame(ctl, bg=self.BG)
        tk.Label(cell, text="Adapter", bg=self.BG, fg=self.DIM, font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.iface_var = tk.StringVar(value=self.settings["iface"] or "auto")
        self.iface_menu = ttk.Combobox(cell, textvariable=self.iface_var, width=16, state="readonly",
                                       values=["auto"] + self._interfaces())
        self.iface_menu.bind("<<ComboboxSelected>>", self._iface_changed)
        self.iface_menu.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(4, 0))
        cell.grid(row=1, column=1, **g)

        self.on_top = tk.BooleanVar(value=self.settings["on_top"])
        ttk.Checkbutton(ctl, text="Always on top", variable=self.on_top,
                        command=self._apply_on_top).grid(row=2, column=0, sticky="w")
        self._apply_on_top()
        self.show_log = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Show log", variable=self.show_log,
                        command=self._toggle_log).grid(row=2, column=1, sticky="w")

        self.status = tk.StringVar(value="Starting…")
        tk.Label(self.root, textvariable=self.status, bg=self.BG, fg=self.DIM, font=("Segoe UI", 9),
                 anchor="w", wraplength=380, justify="left").pack(fill=tk.X, pady=(6, 8), **pad)

        self.log_frame = tk.Frame(self.root, bg=self.BG)
        self.log_text = tk.Text(self.log_frame, height=10, bg="#0d0e11", fg=self.FG,
                                font=("Consolas", 9), relief="flat", wrap="word", state="disabled")
        scroll = ttk.Scrollbar(self.log_frame, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._last_log = None
        self._repeat = 0
        self._apply_rows()

    def _interfaces(self):
        if not maplepackets.tshark_available():
            return []
        out = _run([maplepackets.TSHARK, "-D"])
        return re.findall(r"^\d+\. \S+ \((.+)\)\s*$", out, re.M)

    # ---- rows ----
    def _apply_rows(self):
        for key, (a, b) in self.row_widgets.items():
            for w in (a, b):
                w.grid() if self.row_shown[key].get() else w.grid_remove()
        if self.row_shown["mobs"].get():
            self.mob_frame.pack(before=self.ctl_frame, fill=tk.X, pady=(6, 0), padx=10)
        else:
            self.mob_frame.pack_forget()
        self.settings["hidden_rows"] = [k for k, v in self.row_shown.items() if not v.get()]
        save_settings(self.settings)
        self.root.geometry("")

    def _show_all_rows(self):
        for v in self.row_shown.values():
            v.set(True)
        self._apply_rows()

    def _mob_row(self, mob):
        if mob in self.mob_rows:
            return self.mob_rows[mob]
        self.mob_hint.grid_remove()
        r = len(self.mob_rows) + 1
        key = "?" if mob is None else str(mob)
        default = "Unknown mob" if mob is None else f"Mob #{mob}"
        name = tk.StringVar(value=self.settings["mob_names"].get(key, default))
        if mob is not None:
            name.trace_add("write", lambda *_: self._mob_named(key, name.get()))
        kills, rate = tk.StringVar(value="0"), tk.StringVar(value="—")
        tk.Entry(self.mob_frame, textvariable=name, bg="#2a2d34", fg=self.FG, insertbackground=self.FG,
                 relief="flat", font=("Segoe UI", 10), width=18).grid(row=r, column=0, sticky="ew", pady=1)
        tk.Label(self.mob_frame, textvariable=kills, bg=self.BG, fg=self.ACCENT, font=("Consolas", 11, "bold"),
                 width=6, anchor="e").grid(row=r, column=1, sticky="e")
        tk.Label(self.mob_frame, textvariable=rate, bg=self.BG, fg=self.DIM, font=("Consolas", 9),
                 width=10, anchor="e").grid(row=r, column=2, sticky="e")
        self.mob_rows[mob] = (name, kills, rate)
        self.root.geometry("")
        return self.mob_rows[mob]

    def _mob_named(self, key, text):
        self.settings["mob_names"][key] = text.strip()
        save_settings(self.settings)

    # ---- controls ----
    def reset(self):
        self.stats.reset()
        for w in self.mob_frame.grid_slaves():
            if w is not self.mob_hint:
                w.destroy()
        self.mob_rows = {}
        self.mob_hint.grid()
        if self.paused:
            self.stats.pause(time.time())
        self.root.geometry("")
        self._log("session reset")
        self._refresh()

    def toggle_pause(self):
        now = time.time()
        self.paused = not self.paused
        (self.stats.pause if self.paused else self.stats.resume)(now)
        self.pause_btn.config(text="Resume" if self.paused else "Pause")
        self.status.set("Paused: kills are not counted." if self.paused else "Resumed.")

    def _iface_changed(self, _=None):
        v = self.iface_var.get()
        self.settings["iface"] = "" if v == "auto" else v
        save_settings(self.settings)
        self.capture.stop()
        self.capture = Capture(self.events, self.settings["iface"])
        self._log(f"adapter set to {v}")

    def _apply_on_top(self):
        self.root.attributes("-topmost", self.on_top.get())
        self.settings["on_top"] = self.on_top.get()
        save_settings(self.settings)

    def _toggle_log(self):
        if self.show_log.get():
            self.log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        else:
            self.log_frame.pack_forget()
            self.root.geometry("")

    def _log(self, msg):
        t = time.strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        if msg == self._last_log:  # collapse repeats into one line with a count
            self._repeat += 1
            self.log_text.delete("end-2l", "end-1l")
            self.log_text.insert("end", f"{t} {msg} (×{self._repeat})\n")
        else:
            self._last_log, self._repeat = msg, 1
            self.log_text.insert("end", f"{t} {msg}\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 500:
            self.log_text.delete("1.0", "100.0")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    # ---- event loop ----
    def _poll(self):
        try:
            while True:
                kind, val = self.events.get_nowait()
                if kind == "kill":
                    self._on_kill(val)
                elif kind == "status":
                    self.status.set(val)
                    self._log(val)
                elif kind == "log":
                    self._log(val)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _on_kill(self, ev):
        now = time.time()
        mob = ev["mob"]
        label = self.settings["mob_names"].get(str(mob)) or (f"Mob #{mob}" if mob is not None else "unknown mob")
        if not self.stats.add(now, mob):
            self._log(f"kill ignored (paused): {label}")
            return
        if self.stats.total == 1:
            self.status.set("Counting your kills.")
        self._log(f"kill #{self.stats.total}: {label}")
        self._refresh()

    def _refresh(self):
        now = time.time()
        s = self.stats
        pm = s.per_minute(now)
        self.vars["kills"].set(f"{s.total:,}")
        self.vars["per_min"].set(fmt_rate(pm))
        self.vars["per_hour"].set(fmt_rate(pm * 60 if pm is not None else None))
        self.vars["last_min"].set(f"{s.last_minute(now)}")
        self.vars["session"].set(fmt_duration(s.session_seconds(now)))
        for mob, n in s.by_mob.items():
            _, kills, rate = self._mob_row(mob)
            kills.set(f"{n:,}")
            r = s.per_minute(now, n)
            rate.set("—" if r is None else f"{fmt_rate(r * 60)}/h")

    def _tick(self):
        self._refresh()
        self.root.after(1000, self._tick)

    def quit(self):
        self.capture.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()
