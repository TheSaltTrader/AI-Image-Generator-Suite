"""Integration test: the update flow as wired into the real app window.

Builds the actual App on a withdrawn root with the engine, Ollama probe and
VRAM poll stubbed out, then drives the two ways an update reaches the user
(the startup check and the Check for updates button) through the real queue
handler. This is the test that catches wiring mistakes the module's own
tests cannot see — a renamed widget, a queue key nothing handles, a handler
calling a method that moved.

Run: venv\\Scripts\\python.exe app\\update_ui_test.py
"""

import sys
import threading
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("  " + detail) if detail and not cond else ""))


def pump(root, cond, timeout=5.0):
    """Run the REAL Tk event loop until cond() holds (or timeout): the
    update window's worker thread reports back through after(), which
    needs a running loop exactly as in the app — an update() polling loop
    makes every cross-thread after() raise 'main thread is not in main
    loop' (see KNOWLEDGE_BASE §7)."""
    deadline = time.time() + timeout

    def tick():
        if cond() or time.time() > deadline:
            root.quit()
        else:
            root.after(20, tick)

    root.after(0, tick)
    root.mainloop()


import comic_art_creator as app
import self_update as su

# --- keep the test off the network, the GPU and the engine ---------------
app.App._boot_engine = lambda self: None
app.App._probe_ollama = lambda self: None
app.App._vram_poll = lambda self: None
app.App._first_run_check = lambda self: None
app.App._check_updates_bg = lambda self: None      # driven by hand below

_started = []
app.subprocess.Popen = lambda *a, **k: _started.append(a)
app.kill_engine = lambda: None

from tkinter import Tk

root = Tk()
root.withdraw()
ui = app.App(root)
root.update()

print("the app window")
check("app builds with the updater wired in", ui.root.winfo_exists())
check("Check for updates button exists", hasattr(ui, "upd_btn"))
check("button is enabled at rest",
      "disabled" not in ui.upd_btn.state())
check("button says what it does",
      "Check for updates" in ui.upd_btn.cget("text"))
check("the version is shown next to it",
      any(app.APP_VERSION in str(w.cget("text"))
          for w in ui.upd_btn.master.winfo_children()
          if "text" in w.keys()))

# ---- the startup path: a queued app_update opens the window -------------
# automatic mode (the default): the download starts on its own, the app
# keeps running, and only Restart now hands over to the new exe
print("startup check — automatic")
with tempfile.TemporaryDirectory() as td:
    su.configure(td, td)
    upd = su.Update("v1.99.0", "http://x/a.zip", 75 * 1024 * 1024,
                    "- something new", "2026-08-20")
    applied = []
    real_apply = su.apply_update

    def fake_apply(upd_, cur, status, progress, cancel):
        applied.append(upd_.tag)
        return Path(td) / "ComicArtCreator.exe"

    su.apply_update = fake_apply
    _started.clear()          # the app shells out to nvidia-smi on start;
    #                           only launches AFTER this point are ours
    check("automatic updates are on by default", su.auto_update())
    ui.ui_queue.put(("app_update", upd))
    ui._poll_queue()
    root.update()
    win = getattr(ui, "_upd_win", None)
    check("a queued update opens the window", win is not None
          and win.winfo_exists())
    check("the startup window is in automatic mode", win is not None and win.auto)
    pump(root, lambda: win._newexe is not None)
    check("the update downloaded and installed on its own",
          applied == ["v1.99.0"] and win._newexe is not None)
    check("the app was NOT restarted without approval",
          not _started and ui.root.winfo_exists())
    check("Restart now is offered", win.update_btn.cget("text") == "Restart now"
          and "disabled" not in win.update_btn.state())

    # a second notification must not stack a second window
    ui.ui_queue.put(("app_update", upd))
    ui._poll_queue()
    root.update()
    check("a second notification reuses the open window",
          getattr(ui, "_upd_win") is win)

    # Later leaves the app running; the new exe starts next launch
    win._continue()
    root.update()
    check("Later closes the window", not win.winfo_exists())
    check("Later leaves the app running", ui.root.winfo_exists())
    check("Later starts no new process", not _started)
    check("Later says the new version starts next time",
          "next time" in ui.status_var.get(), ui.status_var.get())
    su.apply_update = real_apply

# manual mode (automatic turned off): nothing downloads until Update now
print("startup check — manual")
with tempfile.TemporaryDirectory() as td:
    su.configure(td, td)
    su.set_auto_update(False)
    upd = su.Update("v1.99.0", "http://x/a.zip", 75 * 1024 * 1024,
                    "- something new", "2026-08-20")
    _started.clear()
    ui.ui_queue.put(("app_update", upd))
    ui._poll_queue()
    root.update()
    win = getattr(ui, "_upd_win", None)
    check("a queued update opens the window", win is not None
          and win.winfo_exists())
    check("the window is in manual mode", win is not None and not win.auto)
    check("the window names the new version",
          win is not None and "v1.99.0" in win.title() + str(
              win.upd.tag))
    check("nothing downloads on open", not win._busy)

    # Continue leaves the app running and untouched
    win._continue()
    root.update()
    check("Continue closes the window", not win.winfo_exists())
    check("Continue leaves the app running", ui.root.winfo_exists())
    check("Continue starts no new process", not _started)

# ---- the button path ----------------------------------------------------
print("Check for updates button")
with tempfile.TemporaryDirectory() as td:
    su.configure(td, td)

    # nothing newer -> the button must say so, and re-enable
    done = threading.Event()
    su.check = lambda *a, **k: None
    ui._check_updates_now()
    check("button disables while checking",
          "disabled" in ui.upd_btn.state())
    for _ in range(200):                    # let the worker thread land
        ui._poll_queue()
        root.update()
        if "disabled" not in ui.upd_btn.state():
            break
    check("button re-enables after the check",
          "disabled" not in ui.upd_btn.state())
    check("up-to-date is reported, not silence",
          "up to date" in ui.status_var.get(), ui.status_var.get())

    # something newer -> the window opens
    upd = su.Update("v2.0.0", "http://x/a.zip", 10 * 1024 * 1024,
                    "- a big one", "2026-08-20")
    su.check = lambda *a, **k: upd
    ui._check_updates_now()
    for _ in range(200):
        ui._poll_queue()
        root.update()
        if getattr(ui, "_upd_win", None) is not None \
                and ui._upd_win.winfo_exists():
            break
    win = ui._upd_win
    check("the button opens the update window",
          win is not None and win.winfo_exists())
    check("button re-enabled once the window is up",
          "disabled" not in ui.upd_btn.state())

    # Skip is remembered and reported
    win._skip()
    root.update()
    check("Skip is remembered", su.skipped_version() == "v2.0.0")
    check("Skip is reported in the status bar",
          "skipped" in ui.status_var.get(), ui.status_var.get())

# ---- the relaunch hand-over --------------------------------------------
print("relaunch after a successful update")
_started.clear()
destroyed = []
ui.root.destroy = lambda: destroyed.append(True)
ui._relaunch_after_update(Path("ComicArtCreator.exe"), "v2.0.0")
root.update()
check("the new exe is launched", len(_started) == 1, str(_started))
check("the status bar says what happened",
      "v2.0.0" in ui.status_var.get() and "restart" in ui.status_var.get(),
      ui.status_var.get())
root.after(0, lambda: None)
root.update()

print()
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
