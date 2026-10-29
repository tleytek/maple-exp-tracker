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

import census
import main
import maplepackets
from exptable import to_next
from killparse import KillCounter, _mob_type
from mobnames import MOB_NAMES
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
    # characters: char id -> level / EXP / Use tab last seen, so EXP and potions work
    # on the next launch before the game resends the character info
    # players: other players' char id -> [level, name], so players already on the map
    # when the tracker starts are named (the server only sends names on arrival)
    extras = {"mob_names": {}, "potion_items": {}, "iface": "", "characters": {},
              "players": {}}
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


# ---- EXP ------------------------------------------------------------------ #
class PacketExpTracker(main.RateTracker):
    """EXP from packets: the values are exact, so no OCR cross-checks. Gains count
    from the first EXP packet whether or not the level is known yet; the level
    (self.full = EXP for a full bar) is only needed for %, the bar and time to level.
    """

    def reset_session(self):
        super().reset_session()
        self.full = None  # set by the app before each reading, None = level unknown
        self._prev_full = None

    @property
    def estimated_max(self):
        return self.full

    def feed(self, now, current, percent):
        self.last_seen = now
        if current is None:
            return None
        msg = None
        if self.prev is None:
            self.change_log.append((now, current))
        elif current != self.prev:
            if current > self.prev:
                self.session_gained += current - self.prev
            else:
                full_then = self._prev_full
                if full_then:
                    level_up = self.prev - current > 0.3 * full_then
                else:
                    level_up = current < 0.5 * self.prev
                self.change_log.clear()
                if level_up:
                    self.levels += 1
                    # rest of the old bar (unknown without the level) + the new EXP
                    self.session_gained += max(0, (full_then or self.prev) - self.prev) + current
                    msg = f"Level up! ({self.prev:,} → {current:,})"
                else:
                    msg = f"EXP dropped {self.prev:,} → {current:,} — rate restarted"
            self.change_log.append((now, current))
            self.rate = self._calculate_rate()
            self.gain_log.append((now, self.session_gained))
            self._prune_gain_log(now)
        self.prev = self.current = current
        self.percent = percent
        self._prev_full = self.full
        return msg


# ---- kills ---------------------------------------------------------------- #
def mob_label(mob, typed):
    """A name the user typed for this mob id, else the game's name, else 'Mob #id'."""
    if mob is None:
        return "Unknown mob"
    return typed.get(str(mob)) or MOB_NAMES.get(mob) or f"Mob #{mob}"


def fmt_kills(v):
    """Kills per minute: whole numbers as is, a median between two as x.5."""
    return f"{v:,.1f}".removesuffix(".0")


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


class OtherPlayers:
    """Other players' kills on our map, from the killer id in every kill packet.

    Kills per minute are over the time each player spent on the map with us (not
    counting paused time): we only see kills on the map we're on. Names and levels
    come from the user-enter packets, which the server sends on arrival for everyone
    already there; players who were there before the tracker started stay "#id"
    (with their time counted from the start) until the next map change.
    """

    def __init__(self, now=None, known=None):
        self.info = dict(known or {})  # char id -> (level, name); 'known' = remembered
        self.present = set()   # char ids on our map now
        self.paused = False
        self.reset_session(time.time() if now is None else now)

    def reset_session(self, now):
        self.kills = Counter()
        self.kill_times = {}       # char id -> deque of kill times in the last minute
        self.seconds = Counter()   # finished time on the map with us
        self.since = {} if self.paused else {c: now for c in self.present}
        self.origin = now          # earliest a player we never saw enter can have been here

    def _close(self, cid, now):
        start = self.since.pop(cid, None)
        if start is not None:
            self.seconds[cid] += max(0.0, now - start)

    def enter(self, ts, cid, level, name):
        self.info[cid] = (level, name)
        self.present.add(cid)
        if not self.paused:
            self.since.setdefault(cid, ts)

    def leave(self, ts, cid):
        self.present.discard(cid)
        self._close(cid, ts)

    def field(self, ts):
        """We changed map or channel: nobody from the old map is with us any more."""
        for cid in list(self.present):
            self._close(cid, ts)
        self.present.clear()
        self.origin = ts

    def kill(self, ts, cid):
        if self.paused:
            return False
        if cid not in self.present:  # here since before we started watching
            self.present.add(cid)
            self.since.setdefault(cid, self.origin)
        self.kills[cid] += 1
        self.kill_times.setdefault(cid, deque()).append(ts)
        return True

    def forget(self, cid):
        """Our own id (learned late): not another player."""
        for d in (self.info, self.kills, self.kill_times, self.seconds, self.since):
            d.pop(cid, None)
        self.present.discard(cid)

    def pause(self, now):
        for cid in list(self.since):
            self._close(cid, now)
        self.paused = True

    def resume(self, now):
        self.paused = False
        for cid in self.present:
            self.since.setdefault(cid, now)

    def seconds_with_us(self, cid, now):
        start = self.since.get(cid)
        return self.seconds[cid] + (max(0.0, now - start) if start is not None else 0.0)

    def rate(self, cid, now):
        """Kills per minute while on the map with us; None while that's too short to say."""
        secs = self.seconds_with_us(cid, now)
        return None if secs < main.ROLLING_MIN_SPAN else self.kills[cid] / (secs / 60)

    def last_minute(self, cid, now):
        times = self.kill_times.get(cid, ())
        while times and times[0] <= now - 60:
            times.popleft()
        return len(times)

    def name(self, cid):
        level, name = self.info.get(cid, (None, None))
        return f"{name} ({level})" if name else f"#{cid}"

    def rows(self, now):
        """(char id, label, kills, kills/min or None, kills in the last minute, on our
        map now) for everyone who killed something this session, most kills first."""
        return [(cid, self.name(cid), n, self.rate(cid, now), self.last_minute(cid, now),
                 cid in self.present) for cid, n in self.kills.most_common()]


# ---- spawn cycle ---------------------------------------------------------- #
CYCLE = 7.13  # s between refills: median of 846 gaps over 41 map visits in census.db


class SpawnCycle:
    """When the map's next refill is due, from the monster spawn packets.

    The server refills every map on a fixed timer (CYCLE). A refill arrives as a
    burst of spawns within census.BURST_GAP; the spawns sent on entering a map
    (within census.ARRIVAL) are the monsters already there, not a refill. The
    countdown runs from the latest burst, in CYCLE steps when a cycle has nothing
    to spawn. The alive count is only known after we saw a map arrival: monsters
    already there when the tracker started were never announced to it.
    """

    def __init__(self):
        self.alive = set()
        self.alive_known = False
        self.arrival_until = None
        self.last_ts = None     # time of the latest refill burst's first spawn
        self.last = None        # (spawned in it, alive after it or None)

    @property
    def alive_count(self):
        return len(self.alive) if self.alive_known else None

    def field(self, ts):
        self.alive.clear()
        self.alive_known = True
        self.arrival_until = ts + census.ARRIVAL
        self.last_ts = self.last = None

    def spawn(self, ts, oid):
        if oid in self.alive:
            return
        self.alive.add(oid)
        if self.arrival_until is not None and ts < self.arrival_until:
            return
        if self.last_ts is not None and ts - self.last_ts <= census.BURST_GAP:
            n = self.last[0] + 1
        else:
            self.last_ts, n = ts, 1
        self.last = (n, self.alive_count)

    def remove(self, ts, oid):
        self.alive.discard(oid)

    def next_in(self, now):
        """Seconds until the next refill is due; None before the first refill seen."""
        if self.last_ts is None:
            return None
        return CYCLE - (now - self.last_ts) % CYCLE


# ---- capture worker ------------------------------------------------------- #
class Capture:
    """Background thread: tshark -> decoder -> PlayerState / KillCounter -> UI queue."""

    def __init__(self, out, iface_setting, item_kind, characters=None):
        self.out = out
        self.iface_setting = iface_setting
        self.characters = dict(characters or {})  # remembered characters (read-only copy)
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

    def handle(self, ts, p):
        """One decoded server packet -> UI events."""
        put = self.out.put
        st = self.state
        had_char, was_restored = st.char_id, st.restored
        for ev in st.feed(p):
            put(("state", ev))
        if st.char_id and (st.char_id != had_char or was_restored and not st.restored):
            put(("char", (st.name, st.level, was_restored)))
            self.kills.self_id = st.char_id
        kill = self.kills.feed(ts, p)
        if kill:
            put(("kill", kill))
        op = maplepackets.opcode(p)
        if op == census.OP_SET_FIELD and census.field_map_id(p) is not None:
            put(("field", ts))
        elif op == census.OP_SPAWN:
            r = _mob_type(p)
            if r:
                put(("mob_spawn", (ts, r[0])))
        elif op == census.OP_REMOVE and len(p) >= 6:
            put(("mob_gone", (ts, int.from_bytes(p[2:6], "little"))))
        elif op == census.OP_USER_ENTER:
            u = census.user_enter(p)
            if u:
                put(("player_enter", (ts, *u)))
        elif op == census.OP_USER_LEAVE and len(p) >= 6:
            put(("player_leave", (ts, int.from_bytes(p[2:6], "little"))))
        # Our id is known from the first kill; pick up the level remembered for it
        if st.level is None and st.exp is not None and self.kills.self_id:
            snap = self.characters.get(str(self.kills.self_id))
            if st.restore(self.kills.self_id, snap):
                put(("restored", (st.name, st.level)))

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
            put(("status", f"Watching {iface}. Stats fill in on the next kill or pickup."))
            put(("log", f"capturing on {iface}"))
            started = time.time()
            try:
                self.proc = maplepackets.open_tshark(iface=iface)
            except OSError as e:
                put(("status", f"Could not start tshark: {e}"))
                return
            for ts, _key, p in maplepackets.decode_lines(self.proc.stdout,
                                                         log=lambda m: put(("log", m))):
                self.handle(ts, p)
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
    EXTRA_SECTION = "Kill lists"
    SECTION_TITLES = main.App.SECTION_TITLES[:-1] + ("Kills", "Session")
    BIG_ROWS = main.App.BIG_ROWS + ("kills",)
    KILL_ROWS = [("kills", "Kills (session)"), ("kill_med", "Kills / min (median)"),
                 ("kill_60", "Kills / min (rolling)"),
                 ("spawn_next", "Next spawn in"), ("spawn_last", "Last spawn")]

    def __init__(self):
        first_run_prices()
        self.extras = load_extras()
        self.kill_tracker = KillTracker()
        self.spawns = SpawnCycle()
        self.others = OtherPlayers(known={int(c): tuple(v) for c, v in
                                          self.extras["players"].items()})
        self.pot_kind = {int(k): v for k, v in self.extras["potion_items"].items()}
        self.packet_events = queue.Queue()
        self.capture = None
        self._asked_channel = False
        self._session_started = False  # the clock starts at the first kill / EXP gain
        super().__init__()
        self.capture = Capture(self.packet_events, self.extras["iface"], self.pot_kind,
                               self.extras["characters"])
        self.set_status("Starting packet capture…")
        self.root.after(int(main.POLL_RATE * 1000), self._feed_tick)
        self._spawn_tick()

    def _make_exp_tracker(self):
        return PacketExpTracker()

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
        # Other players' kills: name | kills | kills/min on the map with us | last minute
        self.player_frame = tk.Frame(box, bg=self.BG)
        self.player_frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.player_frame.columnconfigure(0, weight=1)
        self.player_rows = {}
        self.image_rows = [("mobs", "Per-mob kills"), ("players", "Other players' kills")]
        self.preview_frames = (box,)
        self._add_hideable("mobs", (self.mob_frame,))
        self._add_hideable("players", (self.player_frame,))

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
                    name, level, was_restored = val
                    self.set_status(f"Tracking {name} (Lv {level}) from packets.")
                    self.log(f"character {name}, level {level}")
                    if was_restored:
                        self._rebase_potions()
                    self._remember_character()
                elif kind == "restored":
                    name, level = val
                    self.set_status(f"Tracking {name} (Lv {level}, remembered) from packets.")
                    self.log(f"remembered {name}: level {level} (updates on the next channel change)")
                elif kind == "kill":
                    self._on_kill(val)
                elif kind == "mob_spawn":
                    self.spawns.spawn(*val)
                elif kind == "mob_gone":
                    self.spawns.remove(*val)
                elif kind == "field":
                    self.others.field(val)
                    self.spawns.field(val)
                elif kind == "player_enter":
                    self.others.enter(*val)
                    self._remember_player(*val[1:])
                elif kind == "player_leave":
                    self.others.leave(*val)
                elif kind == "state":
                    self._on_state(val)
        except queue.Empty:
            pass
        super()._poll()

    def _on_state(self, ev):
        if ev[0] == "exp":
            if self.tracker.current is None and not self.paused:
                # baseline from the EXP before this change, so the first gain counts
                self.tracker.feed(time.time(), ev[1], None)
            if ev[2] > ev[1]:
                self._start_session("first EXP gain")
        if ev[0] == "level":
            self.log(f"level {ev[1]} → {ev[2]}")
            self._remember_character()
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
            if st.exp is not None:
                full = to_next(st.level)
                pct = int(st.exp * 10000 / full) / 100 if full else None  # truncated, like the game
                self.tracker.full = full
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

    def _remember_character(self):
        snap = self.capture.state.snapshot() if self.capture else None
        if snap:
            self.extras["characters"][str(self.capture.state.char_id)] = snap
            try:
                save_extras(self.extras)
            except OSError as e:
                self.set_status(f"Couldn't save character: {e}")

    def _rebase_potions(self):
        """The real Use tab replaced a remembered one: take its counts as the new
        baseline instead of counting the difference as potions used."""
        totals = self.capture.state.totals()
        for kind, t in self.potions.items():
            items = [i for i, k in self.pot_kind.items() if k == kind]
            if items and t.current is not None:
                t.current = sum(totals.get(i, 0) for i in items)
                t.pending, t.pending_count = None, 0

    # ---- kills ----
    def _on_kill(self, ev):
        mob = ev["mob"]
        if self.paused:
            return
        if not ev["own"]:
            self.others.kill(ev["ts"], ev["killer"])
            return
        self.others.forget(ev["killer"])  # in case our id was learned after this one's kills
        self.extras["players"].pop(str(ev["killer"]), None)
        self._start_session("first kill")
        if not self.kill_tracker.add(time.time(), mob):
            return
        self.log(f"kill #{self.kill_tracker.current}: {self._mob_label(mob)}")
        self._refresh()

    def _start_session(self, why):
        """Time before the first kill or EXP gain (your own or a party member's kill)
        isn't session time: start every tracker's clock then."""
        if self._session_started or self.paused:
            return
        self._session_started = True
        for t in self._trackers():
            t._start_clock()
        self.log(f"{why}: session clock started")

    def _mob_label(self, mob):
        return mob_label(mob, self.extras["mob_names"])

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

    def _player_row(self, cid):
        if cid in self.player_rows:
            return self.player_rows[cid]
        if not self.player_frame.grid_slaves(row=0):  # headings, once someone has a kill
            for c, (text, anchor) in enumerate((("Other players (Lv)", "w"), ("kills", "e"),
                                                ("/min", "e"), ("1 min", "e"))):
                tk.Label(self.player_frame, text=text, bg=self.BG, fg=self.DIM,
                         font=("Segoe UI", 9), anchor=anchor).grid(row=0, column=c, sticky="ew")
        cells = []
        # the name takes whatever width is left; the numbers keep theirs
        for c, (font, fg, width) in enumerate(((("Segoe UI", 10), self.FG, 1),
                                               (("Consolas", 12, "bold"), self.ACCENT, 5),
                                               (("Consolas", 10), self.DIM, 6),
                                               (("Consolas", 10), self.DIM, 5))):
            var = tk.StringVar()
            label = tk.Label(self.player_frame, textvariable=var, bg=self.BG, fg=fg, font=font,
                             width=width, anchor="w" if c == 0 else "e")
            cells.append((var, label))
        self.player_rows[cid] = cells
        self._fit_height()
        return cells

    def _refresh_players(self, now):
        rows = self.others.rows(now)
        for cid in set(self.player_rows) - {r[0] for r in rows}:  # our own id, learned late
            for _, label in self.player_rows.pop(cid):
                label.destroy()
        for i, (cid, label, kills, rate, last, here) in enumerate(rows, start=1):
            cells = self._player_row(cid)
            texts = (label, f"{kills:,}", "—" if rate is None else f"{rate:,.1f}",
                     f"{last:,}" if here else "left")
            for c, ((var, widget), text) in enumerate(zip(cells, texts)):
                var.set(text)
                if c == 0:
                    widget.config(fg=self.FG if here else self.DIM)
                widget.grid(row=i, column=c, sticky="ew" if c == 0 else "e")

    def _remember_player(self, cid, level, name):
        if self.extras["players"].get(str(cid)) == [level, name]:
            return
        self.extras["players"][str(cid)] = [level, name]
        try:
            save_extras(self.extras)
        except OSError as e:
            self.log(f"couldn't save player names: {e}")

    def _mob_named(self, mob, text):
        self.extras["mob_names"][str(mob)] = text.strip()
        try:
            save_extras(self.extras)
        except OSError as e:
            self.set_status(f"Couldn't save mob names: {e}")

    # ---- overrides ----
    def _trackers(self):
        return super()._trackers() + [self.kill_tracker]

    def toggle_pause(self):
        super().toggle_pause()
        (self.others.pause if self.paused else self.others.resume)(time.time())

    def reset(self):
        self._session_started = False
        self.kill_tracker.reset_session()
        self.others.reset_session(time.time())
        for frame in (self.mob_frame, self.player_frame):
            for w in frame.grid_slaves():
                w.destroy()
        self.mob_rows = {}
        self.player_rows = {}
        super().reset()  # also re-pauses every tracker in _trackers() when paused
        self._fit_height()

    def _refresh(self):
        super()._refresh()
        if self.tracker.current is not None and self.tracker.full is None:
            # EXP counts already; only % / time to level wait for the level
            self.vars["percent"].set("change ch.")
            if not self._asked_channel:
                self._asked_channel = True
                self.set_status("EXP is counting. For EXP % and time to level, change channel "
                                "(or map) once so the game sends your level; it's remembered after that.")
        k = self.kill_tracker
        self.vars["kills"].set(f"{k.current:,}")
        now = time.time()
        secs = k.session_seconds(now)
        for mob, n in k.by_mob.most_common():
            _, kills, rate = self._mob_row(mob)
            kills.set(f"{n:,}")
            rate.set("—" if secs < main.ROLLING_MIN_SPAN else f"{n * 3600 / secs:,.0f}/h")
        self._refresh_players(now)

    def _refresh_recent(self):
        super()._refresh_recent()
        now = time.time()
        k = self.kill_tracker
        self.vars["kill_med"].set(self._median_text(k, now, fmt=fmt_kills))
        self.vars["kill_60"].set(self._recent_text(k, now, 60))

    def _spawn_tick(self):
        """Refill countdown, a few times a second."""
        if not self.running:
            return
        s = self.spawns
        nxt = s.next_in(time.time())
        self.vars["spawn_next"].set("— (waiting for a refill)" if nxt is None else f"{nxt:.1f} s")
        if s.last is None:
            self.vars["spawn_last"].set("—")
        else:
            alive = s.alive_count
            self.vars["spawn_last"].set(f"+{s.last[0]}" + (f" · {alive} alive" if alive is not None
                                                           else " · alive: change map once"))
        self.root.after(100, self._spawn_tick)

    def _tick(self):
        super()._tick()
        if self.running:
            self._refresh()  # per-mob /h keeps updating between kills
            if not self._session_started:
                self.vars["session"].set("starts at first kill")

    def quit(self):
        if self.capture:
            self.capture.stop()
            self._remember_character()
        super().quit()


def run():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    PacketApp().run()


if __name__ == "__main__":
    run()
