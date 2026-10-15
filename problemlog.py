"""Save snapshots of reading problems so they can be looked at later.

When a read fails (or a reading is rejected), the app calls
``ProblemLog.record`` with the image the reader saw. If MapleStory is the
focused window, that crop is saved together with a screenshot of the whole
screen, and a line is added to ``problems.log`` in the same folder:

    2026-10-14 20:15:03  hp  a stack count is cut off ...  region=...  files=...

Nothing is saved while another window is focused (a browser or the tracker
itself covering the game would only produce false alarms), and the same
problem is saved at most once every few minutes so the folder stays small.
"""
import ctypes
import os
import sys
import time

GAME_PROCESS = "maplestory.exe"
MIN_INTERVAL = 300  # seconds between snapshots of the same problem
KEEP = 200          # newest snapshots kept; older files are deleted


def foreground_process():
    """Lower-case exe name of the focused window's process, or None."""
    if sys.platform != "win32":
        return None
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            return os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


class ProblemLog:
    def __init__(self, folder, grab_screen, focused=foreground_process,
                 min_interval=MIN_INTERVAL, keep=KEEP):
        """grab_screen() -> PIL image of the whole screen (or None)."""
        self.folder = folder
        self.grab_screen = grab_screen
        self.focused = focused
        self.min_interval = min_interval
        self.keep = keep
        self.last_saved = {}  # (kind, reason) -> time

    def record(self, kind, reason, img, region=None, detail=None, now=None):
        """Save a snapshot of a problem. Returns the crop's path, or None
        when skipped (game not focused, or saved recently).

        'reason' identifies the problem for throttling, so keep it fixed;
        put changing values (the numbers read) in 'detail'."""
        now = time.time() if now is None else now
        key = (kind, reason)
        if now - self.last_saved.get(key, -1e18) < self.min_interval:
            return None
        if self.focused() != GAME_PROCESS:
            return None
        self.last_saved[key] = now
        os.makedirs(self.folder, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        base = f"{stamp}_{kind}"
        n = 1
        while os.path.exists(os.path.join(self.folder, base + "_crop.png")):
            n += 1
            base = f"{stamp}_{kind}_{n}"
        files = []
        crop_path = os.path.join(self.folder, base + "_crop.png")
        img.save(crop_path)
        files.append(os.path.basename(crop_path))
        try:
            screen = self.grab_screen()
        except Exception:
            screen = None
        if screen is not None:
            screen_path = os.path.join(self.folder, base + "_screen.png")
            screen.save(screen_path)
            files.append(os.path.basename(screen_path))
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        where = ("region={left},{top} {width}x{height}".format(**region)
                 if region else "region=-")
        with open(os.path.join(self.folder, "problems.log"), "a", encoding="utf-8") as f:
            f.write(f"{when}  {kind}  {reason}"
                    + (f" ({detail})" if detail else "")
                    + f"  {where}  files={' '.join(files)}\n")
        self._prune()
        return crop_path

    def _prune(self):
        crops = sorted(f for f in os.listdir(self.folder) if f.endswith("_crop.png"))
        for old in crops[:-self.keep] if len(crops) > self.keep else []:
            base = old[:-len("_crop.png")]
            for suffix in ("_crop.png", "_screen.png"):
                try:
                    os.remove(os.path.join(self.folder, base + suffix))
                except OSError:
                    pass
