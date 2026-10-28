# Maple EXP Tracker

A small always-on-top window for MapleStory that reads the EXP text on your
screen and shows:

- current EXP and %
- EXP per minute (session average), plus a rolling EXP per minute
- estimated time to level
- EXP gained and levels this session
- mesos: current amount, mesos per minute plus a rolling rate,
  and mesos gained / spent this session (optional, needs the inventory open)
- potions: HP and MP potions used, what they cost, and **net mesos** (mesos
  picked up minus potion cost) per minute, rolling, and for the
  session (optional, needs the inventory open on the Use tab)

It only looks at the screen (like a screenshot); it doesn't touch the game.

## Download (easiest)

Grab **`MapleExpTracker.exe`** from the
[latest release](https://github.com/tleytek/maple-exp-tracker/releases/latest)
and double-click it. Nothing else to install.

Windows SmartScreen may say "Windows protected your PC" because the exe isn't
code-signed. Click **More info → Run anyway**.

## Run from source

1. Install **Python 3** from <https://www.python.org/downloads/>.
   In the installer, tick **"Add python.exe to PATH"**.
2. Download this repo (green **Code** button → **Download ZIP**, then unzip),
   or `git clone` it.
3. Double-click **`run.bat`**.

The first run takes about a minute while it installs its packages. After that
it opens straight away.

To build the exe yourself, run **`build.bat`**. It writes
`dist\MapleExpTracker.exe`.

## Usage

1. Click **Select EXP**, then drag a box around the EXP text at the bottom
   of the game, e.g. `EXP. 11,886 [41.01%]`. Include both green brackets
   `[ ]`.
2. Check the **EXP** preview: it should show the EXP text.
3. Optional: open your inventory, click **Select Mesos** and drag a box
   around the meso amount (the white box at the bottom of the inventory).
   Keep the inventory open while you play; the **Mesos** preview shows what
   is being read.
4. Optional: with the inventory on the **Use** tab, click **Select HP pots**
   and drag a box over your HP potion slots (one or more slots, only
   potions). Do the same with **Select MP pots**. Type what one potion costs
   into **HP pot price** / **MP pot price**.
5. Play. The numbers update every half second.

| Control | Action |
|---|---|
| Select EXP | Pick where the EXP text is |
| Select Mesos | Pick where the inventory's meso amount is |
| Select HP pots / Select MP pots | Pick the inventory slots holding your potions |
| HP pot price / MP pot price | Mesos per potion, used for the potion cost |
| Reset | Start a new session |
| Pause | Stop reading the screen |
| Always on top | Keep the window above the game |
| Settings | Tick / untick which stats and region images are shown, overlay opacity, and the problem snapshots folder (saved) |
| Overlay (F3) | Borderless, see-through window with just the stats, on top of the game. Drag to move; F3 or double-click to go back |
| F1 | Reset the rate |
| F2 | Hide / show the window |
| F3 | Overlay mode on / off |

The window is responsive: make it wide and the stats and buttons spread
out into columns; make it narrow and they stack.

Tips:

- Hovering the mouse over the EXP bar shows a tooltip that covers the text;
  reading pauses until it's gone.
- If you change resolution or move the game window, use **Select EXP**
  again. If you move the inventory, use **Select Mesos** again.
- **Mesos gained** only counts increases, so buying potions doesn't lower
  your meso rate; spending is shown separately. Selling to an NPC counts as
  gained.
- **Potions used** counts the stacks going down. Buying or looting potions
  doesn't offset it. A drop of more than 10 at once (e.g. the inventory was
  closed for a while) is confirmed for ~10 seconds first, so a tooltip
  hiding a stack isn't counted as potions used. The last potion of a region
  (1 → empty) isn't counted, because an empty slot looks like a closed
  inventory.
- **EXP / 1 min** is your session average: session total divided by session
  time (paused time doesn't count).
- **EXP / min (rolling)** is the EXP you actually gained in the last 60
  seconds, so it drops as soon as you slow down (until a minute has passed,
  the time it covers is shown in brackets, e.g. `750 (23s)`).
- Mesos and net mesos work the same way.
- **Time to level** uses your typical minute: the median of the EXP gained in
  each of the last 10 minutes, so one big burst or a town trip doesn't swing
  it. For the first 3 minutes it uses the pace over the last 5 minutes.
- Nothing resets when you're idle: idle time counts against the numbers. The
  averages restart on Reset; F1 restarts the rolling numbers.
- Settings are saved in `%APPDATA%\MapleExpTracker`.
- When a read fails or a reading is rejected while MapleStory is the focused
  window, the app saves what it saw (the region's image and a full
  screenshot) to `%APPDATA%\MapleExpTracker\problems`, with a line in
  `problems.log` saying what went wrong. Nothing is saved while another
  window is focused. The same problem is saved at most every 5 minutes,
  and only the newest 200 snapshots are kept.

## How the reading works

The game's EXP line uses a tiny pixel font whose `,` and `.` are detached
dots, which general OCR (e.g. Tesseract) misreads. `expocr.py` reads it
directly instead: it finds the green brackets, cuts the line into
characters, and recognises each digit by its shape (holes and stroke
positions). Results that don't look like `1,234 [12.34%]` are discarded rather
than shown wrong.

The meso amount uses a different font (black on white); `mesoocr.py`
reads it the same way, anchored on the white meso box.

Potion stack counts use the game's outlined slot-count font; `potionocr.py`
matches each digit against that font pixel for pixel and adds up every stack
in the region. A count it can't read fully (cut off by the region, or under
the cursor) fails the whole read instead of losing a digit.

Run the tests with:

```
.venv\Scripts\python.exe test_expocr.py
.venv\Scripts\python.exe test_mesoocr.py
.venv\Scripts\python.exe test_potionocr.py
.venv\Scripts\python.exe test_problemlog.py
.venv\Scripts\python.exe test_tracker.py
.venv\Scripts\python.exe test_killparse.py
.venv\Scripts\python.exe test_statparse.py
```

Real captures named `<exp>_<percent>[_note].png` in `tests/` are checked too.

## Maple Packet Tracker (second exe)

`MaplePacketTracker.exe` (`packetmain.py`) shows the same stats plus **kills**
(total, per minute/hour, rolling, and per mob; type a name over "Mob #N" to
label it), but reads everything from the game's network packets instead of the
screen, so there are no regions to select. It needs
[Wireshark](https://www.wireshark.org/) (with Npcap) installed and finds the
game's network adapter by itself.

It also lists **other players' kills** on your map (the game names the killer
of every monster): kills this session, kills per minute while they were on the
map with you, and kills in the last minute (`left` once they leave the map).
Names and levels show after you change map or channel once; players who were
already there when the tracker started show as `#<character id>` until then.

- It only watches packets; nothing is sent to the game. Packet sniffing is
  still against the game's terms of service, so it is a separate exe and the
  risk is yours.
- Level and potions load when the game sends your character info: change
  channel (or map) once after starting it. EXP/mesos/kills start on the next
  kill or pickup. HP/MP potions are recognised automatically the first time
  you drink one (by which bar it fills).
- Settings live in `%APPDATA%\MaplePacketTracker` (potion prices are copied
  from the OCR tracker on first run).
- Protocol notes: `maplepackets.py` (decoder), `killparse.py`, `statparse.py`.
  Opcodes can change when the game is patched.

### Map census (`census.py`)

Logs, for every map you visit, the most monsters alive at once, how many
players were on the map, how full each refill tops the map up, and the time
between refills. It answers map-capacity questions from the server's own
packets, with no counting by hand. Same Wireshark requirement and same
watch-only rule as the packet tracker.

    .venv\Scripts\python.exe census.py --names path\to\osms\maps.json

Start it, then change map or channel once (the census needs to see you
arrive). It prints `ENTERED map ...` when you arrive, a line for every refill
while you hunt (how many it added, how many alive after it, deaths since the
last one, players on the map), and a `LEFT map ...` summary for the map you
just left.

Everything goes into **`census.db`** (SQLite; tables described in
`censusdb.py`), written as it happens, so it can run for months: double-click
**`census.bat`** and leave it open. It waits while the game is closed and
restarts the capture if it stops. `--quiet` (what `census.bat` uses) prints only
arrivals and departures. `--import-jsonl census.jsonl` adds visits logged by the
older version. Roughly 30 MB per day of non-stop hunting.
`--pcap file.pcapng` replays a saved capture instead.
