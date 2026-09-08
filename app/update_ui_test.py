"""Integration test: the update flow as wired into the real app window.

Builds the actual App on a withdrawn root with the engine, Ollama probe and
VRAM poll stubbed out, then drives the two ways an update reaches the user
(the startup check and the Check for updates button) through the real queue
handler. This is the test that catches wiring mistakes the module's own
tests cannot see — a renamed widget, a queue key nothing handles, a handler
calling a method that moved.

Run: venv\\Scripts\\python.exe app\\update_ui_test.py
"""

import json
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
# the top-right bar: a GPU % badge next to the meter, which is named VRAM
check("the meter is named VRAM", "VRAM" in ui.vram_label_var.get()
      or "no NVIDIA" in ui.vram_label_var.get(), ui.vram_label_var.get())
check("a GPU badge exists", hasattr(ui, "gpu_badge") and ui.gpu_badge.winfo_exists())
ui.ui_queue.put(("vram_live", 1000, 2000, 37))
ui._poll_queue()
root.update()
check("a reading fills the VRAM meter", ui.vram_label_var.get() == "VRAM 1,000 / 2,000 MB",
      ui.vram_label_var.get())
check("…and the GPU badge", ui.gpu_var.get() == "GPU 37%", ui.gpu_var.get())
ui.ui_queue.put(("vram_live", 1000, 2000))
ui._poll_queue()
root.update()
check("a reading without utilisation leaves the badge blank, not broken",
      ui.gpu_var.get() == "GPU —%", ui.gpu_var.get())

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

# ---- RAG map parsing never blocks the UI --------------------------------
# a user's map is 925 MB of JSON + 1.97 GB of embeddings on an HDD; parsed
# on the UI thread it showed the app as "Not responding" at every launch
print("RAG map loads off the UI thread")
with tempfile.TemporaryDirectory() as td:
    from PIL import Image
    Image.new("RGB", (64, 64), "red").save(Path(td) / "0001.png")
    mp = Path(td) / "ui.ragmap.json"
    mp.write_text(json.dumps({
        "schema": "cbac-ragmap/1", "name": "UI test map",
        "entries": [{"image": "0001.png", "keywords": ["hero"],
                     "caption": "a hero"}]}), encoding="utf-8")
    app.filedialog.askopenfilename = lambda **k: str(mp)
    app.messagebox.askyesno = lambda *a, **k: False
    errors = []
    app.messagebox.showerror = lambda *a, **k: errors.append(a)
    ui._pick_ragmap()
    check("the label says loading right away",
          "loading" in ui.ragmap_var.get(), ui.ragmap_var.get())
    check("the pick returns before the parse lands", ui.ragmap is None)
    pump(root, lambda: ui.ragmap is not None, timeout=10)
    check("the parsed map arrives through the queue",
          ui.ragmap is not None and ui.ragmap.get("name") == "UI test map")
    check("the label shows the map", "UI test map" in ui.ragmap_var.get(),
          ui.ragmap_var.get())
    check("no error was shown", not errors, errors)
    check("retrieval words were precomputed at parse time",
          isinstance(ui.ragmap["entries"][0].get("_words"), frozenset))

    # the startup restore takes the same road
    ui.ragmap = None
    ui.ragmap_var.set("none")
    st = dict(ui._collect_ui_state())
    st["ragmap_path"] = str(mp)
    ui._apply_ui_state(st)
    check("restore remembers the path at once", ui.ragmap_path == str(mp))
    check("restore returns before the parse lands", ui.ragmap is None)
    pump(root, lambda: ui.ragmap is not None, timeout=10)
    check("the restored map arrives",
          ui.ragmap is not None and "UI test map" in ui.ragmap_var.get())

    # an unchanged map is not parsed again on a model refresh; a changed
    # file is — off the UI thread
    gen = ui._ragmap_load_gen
    ui._validate_ragmap()
    check("an unchanged map is not re-parsed on a refresh",
          ui._ragmap_load_gen == gen)
    mp.write_text(mp.read_text(encoding="utf-8").replace(
        "UI test map", "UI test map v2"), encoding="utf-8")
    ui._validate_ragmap()
    check("a changed map file is parsed again", ui._ragmap_load_gen == gen + 1)
    pump(root, lambda: ui.ragmap is not None
         and ui.ragmap.get("name") == "UI test map v2", timeout=10)
    check("…and the new contents land",
          ui.ragmap is not None and ui.ragmap.get("name") == "UI test map v2")
    ui._clear_ragmap()

# ---- the relaunch hand-over --------------------------------------------
# the engine shutdown runs off the UI thread now (it took seconds on the UI
# thread and showed "Not responding"), and the new copy is told our pid
print("relaunch after a successful update")
_started.clear()
destroyed = []
ui.root.destroy = lambda: destroyed.append(True)
ui._relaunch_after_update(Path("ComicArtCreator.exe"), "v2.0.0")
check("the status bar says what happened",
      "v2.0.0" in ui.status_var.get() and "restart" in ui.status_var.get(),
      ui.status_var.get())
pump(root, lambda: destroyed, timeout=10)
check("the new exe is launched", len(_started) == 1, str(_started))
check("the new exe is told which pid to wait for",
      bool(_started) and "--after-update" in _started[0][0]
      and str(app.os.getpid()) in _started[0][0], str(_started))
check("the window is closed once the hand-over is done", bool(destroyed))

# ---- closing ------------------------------------------------------------
print("closing")
destroyed.clear()
app.engine_ours_to_stop = lambda: False
ui._on_close()
check("closing returns before the engine shutdown is done", not destroyed)
pump(root, lambda: destroyed, timeout=10)
check("the app closes once the shutdown thread reports back",
      bool(destroyed))

print()
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
