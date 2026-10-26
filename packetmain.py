"""Maple Packet Tracker: the EXP tracker's window fed from network packets.

Same stats as MapleExpTracker (EXP, mesos, potions, net mesos) plus kills, but the
values come from the game's own server->client packets instead of reading the
screen, so there are no regions to select. Packets are only observed (through
Wireshark's tshark), never sent or changed. Sniffing tools are against the
game's ToS, which is why this is a separate exe from the OCR tracker.

Needs Wireshark (with Npcap) installed at the default path.
"""
import ctypes
import json
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from collections import Counter, deque

import main
import maplepackets
from exptable import to_next
from killparse import KillCounter
from statparse import PlayerState

APP_NAME = "MaplePacketTracker"
GAME_EXE = "MapleStory.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def use_data_dir(path):
    """Point main's settings/problem paths at this app's own folder."""
    main.DATA_DIR = path
    main.SETTINGS_FILE = os.path.join(path, "settings.json")
    main.PROBLEMS_DIR = os.path.join(path, "problems")


use_data_dir(os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME))


def extras_file():
    # main.save_settings keeps only its own keys, so this app's extras live apart
    return os.path.join(main.DATA_DIR, "packet.json")


def load_extras():
    extras = {"mob_names": {}, "potion_items": {}, "iface": ""}
    try:
        with open(extras_file(), encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k in extras and type(v) is type(extras[k]):
                extras[k] = v
    except (OSError, ValueError):
        pass
    return extras


def save_extras(extras):
    os.makedirs(main.DATA_DIR, exist_ok=True)
    tmp = extras_file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(extras, f, indent=2)
    os.replace(tmp, extras_file())


def first_run_prices():
    """Start with the OCR tracker's potion prices if this app has no settings yet."""
    if os.path.exists(main.SETTINGS_FILE):
        return
    ocr = os.path.join(os.path.dirname(main.DATA_DIR), "MapleExpTracker", "settings.json")
    try:
        with open(ocr, encoding="utf-8") as f:
            saved = json.load(f)
        os.makedirs(main.DATA_DIR, exist_ok=True)
        with open(main.SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({k: saved[k] for k in ("hp_price", "mp_price") if k in saved}, f)
    except (OSError, ValueError, KeyError):
        pass


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


# ---- kills ---------------------------------------------------------------- #
class KillTracker(main.RollingGains):
    """Kills this session, with the same rolling windows / session average as EXP."""

    def __init__(self):
        self.reset_session()

    def reset_session(self):
        self.current = 0  # kills this session
        self.session_gained = 0
        self.by_mob = Counter()
        self.gain_log = deque()
        self._start_clock()

    def add(self, now, mob):
        if self.paused_since is not None:
            return False
        self.current += 1
        self.session_gained += 1
        self.by_mob[mob] += 1
        self.gain_log.append((now, self.session_gained))
        self._prune_gain_log(now)
        return True


# ---- capture worker ------------------------------------------------------- #
class Capture:
    """Background thread: tshark -> decoder -> PlayerState / KillCounter -> UI queue."""

    def __init__(self, out, iface_setting, item_kind):
        self.out = out
        self.iface_setting = iface_setting
        self.state = PlayerState(to_next, item_kind)
        self.kills = KillCounter()
        self.proc = None
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _run(self):
        put = self.out.put
        while not self.stop_flag.is_set():
            if not maplepackets.tshark_available():
                put(("status", "Wireshark is not installed (tshark.exe not found at "
                     f"{maplepackets.TSHARK}). Install Wireshark with Npcap, then restart."))
                return
            iface = self.iface_setting
            if not iface:
                iface, why = find_game_iface()
                if not iface:
                    put(("status", f"Waiting: {why}. Retrying every 5 s."))
                    self.stop_flag.wait(5)
                    continue
            put(("status", f"Watching {iface}. EXP/mesos fill in on the next kill or pickup; "
                 "change channel once to load your level and potions."))
            put(("log", f"capturing on {iface}"))
            started = time.time()
            try:
                self.proc = maplepackets.open_tshark(iface=iface)
            except OSError as e:
                put(("status", f"Could not start tshark: {e}"))
                return
            for ts, _key, p in maplepackets.decode_lines(self.proc.stdout,
                                                         log=lambda m: put(("log", m))):
                had_char = self.state.char_id
                for ev in self.state.feed(p):
                    put(("state", ev))
                if self.state.char_id and self.state.char_id != had_char:
                    put(("char", (self.state.name, self.state.level)))
                    self.kills.self_id = self.state.char_id
                kill = self.kills.feed(ts, p)
                if kill:
                    put(("kill", kill))
            if self.stop_flag.is_set():
                return
            err = (self.proc.stderr.read() or "").strip().splitlines()
            reason = err[-1] if err else "tshark exited"
            put(("log", f"capture stopped: {reason}"))
            put(("status", f"Capture stopped ({reason}). Restarting in 5 s."))
            if time.time() - started < 5:
                self.stop_flag.wait(5)


# ---- window ------------------------------------------------------------- #
class PacketApp(main.App):
    TITLE = "Maple Packet Tracker"
    USES_REGIONS = False
    EXTRA_SECTION = "Kills by mob"
    SECTION_TITLES = main.App.SECTION_TITLES[:-1] + ("Kills", "Session")
    BIG_ROWS = main.App.BIG_ROWS + ("kills",)
    KILL_ROWS = [("kills", "Kills (session)"), ("kill_rate", "Kills / 1 min"),
                 ("kill_360", "Kills / 6 min"), ("kill_3600", "Kills / 60 min"),
                 ("kill_60", "Kills / min (rolling)")]

    def __init__(self):
        first_run_prices()
        self.extras = load_extras()
        self.kill_tracker = KillTracker()
        self.pot_kind = {int(k): v for k, v in self.extras["potion_items"].items()}
        self.packet_events = queue.Queue()
        self.capture = None
        super().__init__()
        self.capture = Capture(self.packet_events, self.extras["iface"], self.pot_kind)
        self.set_status("Starting packet capture…")
        self.root.after(int(main.POLL_RATE * 1000), self._feed_tick)

    def _row_layout(self):
        rows = super()._row_layout()
        i = len(rows) - 1 - rows[::-1].index(None)  # before the Session section
        return rows[:i] + [None] + self.KILL_ROWS + rows[i:]

    def _build_extra(self, pad):
        # Per-mob kills: editable name | kills | kills per hour. Hideable in Settings.
        box = tk.Frame(self.root, bg=self.BG)
        box.pack(fill=tk.X, **pad)
        box.columnconfigure(0, weight=1)
        self.mob_frame = tk.Frame(box, bg=self.BG)
        self.mob_frame.grid(row=0, column=0, sticky="ew")
        self.mob_frame.columnconfigure(0, weight=1)
        self.mob_rows = {}
        self.image_rows = [("mobs", "Per-mob kills")]
        self.preview_frames = (box,)
        self._add_hideable("mobs", (self.mob_frame,))

    def _worker_loop(self):
        pass  # values come from the capture thread and _feed_tick, not OCR

    # ---- packet events ----
    def _poll(self):
        try:
            while True:
                kind, val = self.packet_events.get_nowait()
                if kind == "status":
                    self.set_status(val)
                    self.log(val)
                elif kind == "log":
                    self.log(val)
                elif kind == "char":
                    name, level = val
                    self.set_status(f"Tracking {name} (Lv {level}) from packets.")
                    self.log(f"character {name}, level {level}")
                elif kind == "kill":
                    self._on_kill(val)
                elif kind == "state":
                    self._on_state(val)
        except queue.Empty:
            pass
        super()._poll()

    def _on_state(self, ev):
        if ev[0] == "level":
            self.log(f"level {ev[1]} → {ev[2]}")
        elif ev[0] == "pot_kind":
            _, iid, kind, before = ev
            self.pot_kind[iid] = kind
            self.extras["potion_items"][str(iid)] = kind
            try:
                save_extras(self.extras)
            except OSError as e:
                self.set_status(f"Couldn't save potion items: {e}")
            # The use that revealed the kind already happened: start this item from its
            # count before that use, so that first potion is counted too.
            t = self.potions[kind]
            if t.current is None:
                t.current = t.start = before
            else:
                t.current += before
                t.start = (t.start or 0) + before
            self.log(f"item {iid} heals {kind.upper()}: counted as {kind.upper()} potion")

    def _feed_tick(self):
        """Hand the latest packet values to the EXP / meso / potion trackers, the way the
        OCR worker hands them readings (they confirm values over repeated reads)."""
        if not self.running:
            return
        st = self.capture.state if self.capture else None
        if st and not self.paused:
            now = time.time()
            full = to_next(st.level)
            if st.exp is not None and full:
                pct = int(st.exp * 10000 / full) / 100  # game truncates to 2 decimals
                self.results.put(("reading", (now, st.exp, pct, "packets", None)))
            if st.mesos is not None:
                self.results.put(("meso", (now, st.mesos, "packets", None)))
            if st.use_known:
                totals = st.totals()
                for kind in self.potions:
                    items = [i for i, k in self.pot_kind.items() if k == kind]
                    if items:  # called directly, in step with the pot_kind baseline above
                        self._on_potion(kind, now, sum(totals.get(i, 0) for i in items),
                                        "packets", None)
        self.root.after(int(main.POLL_RATE * 1000), self._feed_tick)

    # ---- kills ----
    def _on_kill(self, ev):
        mob = ev["mob"]
        if not self.kill_tracker.add(time.time(), mob):
            return
        self.log(f"kill #{self.kill_tracker.current}: {self._mob_label(mob)}")
        self._refresh()

    def _mob_label(self, mob):
        if mob is None:
            return "Unknown mob"
        return self.extras["mob_names"].get(str(mob)) or f"Mob #{mob}"

    def _mob_row(self, mob):
        if mob in self.mob_rows:
            return self.mob_rows[mob]
        r = len(self.mob_rows)
        name = tk.StringVar(value=self._mob_label(mob))
        if mob is not None:
            name.trace_add("write", lambda *_: self._mob_named(mob, name.get()))
        kills, rate = tk.StringVar(value="0"), tk.StringVar(value="—")
        tk.Entry(self.mob_frame, textvariable=name, bg="#2a2d34", fg=self.FG,
                 insertbackground=self.FG, relief="flat", font=("Segoe UI", 10),
                 width=18).grid(row=r, column=0, sticky="ew", pady=1)
        tk.Label(self.mob_frame, textvariable=kills, bg=self.BG, fg=self.ACCENT,
                 font=("Consolas", 12, "bold"), width=6, anchor="e").grid(row=r, column=1, sticky="e")
        tk.Label(self.mob_frame, textvariable=rate, bg=self.BG, fg=self.DIM,
                 font=("Consolas", 10), width=10, anchor="e").grid(row=r, column=2, sticky="e")
        self.mob_rows[mob] = (name, kills, rate)
        self._fit_height()
        return self.mob_rows[mob]

    def _mob_named(self, mob, text):
        self.extras["mob_names"][str(mob)] = text.strip()
        try:
            save_extras(self.extras)
        except OSError as e:
            self.set_status(f"Couldn't save mob names: {e}")

    # ---- overrides ----
    def _trackers(self):
        return super()._trackers() + [self.kill_tracker]

    def reset(self):
        self.kill_tracker.reset_session()
        for w in self.mob_frame.grid_slaves():
            w.destroy()
        self.mob_rows = {}
        super().reset()  # also re-pauses every tracker in _trackers() when paused
        self._fit_height()

    def _refresh(self):
        super()._refresh()
        k = self.kill_tracker
        self.vars["kills"].set(f"{k.current:,}")
        now = time.time()
        secs = k.session_seconds(now)
        for mob, n in k.by_mob.most_common():
            _, kills, rate = self._mob_row(mob)
            kills.set(f"{n:,}")
            rate.set("—" if secs < main.ROLLING_MIN_SPAN else f"{n * 3600 / secs:,.0f}/h")

    def _refresh_recent(self):
        super()._refresh_recent()
        now = time.time()
        k = self.kill_tracker
        avg = k.session_rate(now)
        self.vars["kill_rate"].set("—" if avg is None else f"{avg:,.1f}")
        for w, times in self.AVERAGE_MULTIPLIERS.items():
            self.vars[f"kill_{w}"].set("—" if avg is None else f"{int(round(avg * times)):,}")
        self.vars["kill_60"].set(self._recent_text(k, now, 60))

    def _tick(self):
        super()._tick()
        if self.running:
            self._refresh()  # per-mob /h keeps updating between kills

    def quit(self):
        if self.capture:
            self.capture.stop()
        super().quit()


def run():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    PacketApp().run()


if __name__ == "__main__":
    run()
