# Maple EXP Tracker

A small always-on-top window for MapleStory that reads the EXP text on your
screen and shows:

- current EXP and %
- EXP gained in the last 1, 5 and 60 minutes (rolling), plus the average EXP per minute
- estimated time to level
- EXP gained and levels this session
- mesos: current amount, mesos picked up in the last 1, 5 and 60 minutes,
  and mesos gained / spent this session (optional, needs the inventory open)
- potions: HP and MP potions used, what they cost, and **net mesos** (mesos
  picked up minus potion cost) for the last 1, 5 and 60 minutes and the
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
| F1 | Reset the rate |
| F2 | Hide / show the window |

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
- **EXP / 1 min**, **5 min** and **60 min** are the EXP you actually gained in
  that much time up to now, so they drop as soon as you slow down. Until a
  window has filled, the time it covers so far is shown in brackets, e.g.
  `13,750 (23m)`. Time to level uses your pace over the last 5 minutes.
- Nothing resets when you're idle: idle time counts against the numbers. The
  average restarts on level-up, death, Reset and F1.
- Settings are saved in `%APPDATA%\MapleExpTracker`.

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
.venv\Scripts\python.exe test_tracker.py
```

Real captures named `<exp>_<percent>[_note].png` in `tests/` are checked too.
