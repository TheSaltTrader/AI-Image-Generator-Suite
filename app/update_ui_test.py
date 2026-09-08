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
check("…and the blue GPU bar", hasattr(ui, "gpu_bar") and int(ui.gpu_bar["value"]) == 37
      and "Gpu" in str(ui.gpu_bar.cget("style")), (ui.gpu_bar["value"], ui.gpu_bar.cget("style")))
ui.ui_queue.put(("vram_live", 1000, 2000))
ui._poll_queue()
root.update()
check("a reading without utilisation leaves the badge blank, not broken",
      ui.gpu_var.get() == "GPU —%", ui.gpu_var.get())

# ---- the left panel is three tabs ---------------------------------------
print("three tabs")
tabs = [ui.left_tabs.tab(t, "text") for t in ui.left_tabs.tabs()]
check("three tabs, in order", tabs == ["Image generation", "Animation", "Borders"], tabs)


def page_of(widget):
    """The tab page a widget sits on, or None when it is outside the tabs."""
    w = widget
    while w is not None and w.master is not ui.left_tabs:
        w = w.master
    return str(w) if w is not None else None


pages = list(ui.left_tabs.tabs())
check("the prompt and Generate live on Image generation",
      page_of(ui.prompt_box) == pages[0] and page_of(ui.go_btn) == pages[0])
check("the LoRA list lives on Image generation", page_of(ui.lora_list) == pages[0])
check("the animator lives on Animation", page_of(ui.anim_prompt_box) == pages[1])
check("the border maker lives on Borders", page_of(ui.border_prompt_box) == pages[2])
check("the batch queue stays under the tabs, on every tab",
      page_of(ui.queue_list) is None and ui.queue_list.winfo_toplevel() is ui.root)
check("the version row stays under the tabs", page_of(ui.upd_btn) is None)
check("every page scrolls with the wheel", len(ui._scroll_canvases) == 3)
ui.left_tabs.select(2)
root.update()
check("the chosen tab is remembered", ui._collect_ui_state().get("tab") == 2,
      ui._collect_ui_state().get("tab"))
st = dict(ui._collect_ui_state())
st["tab"] = 1
ui._apply_ui_state(st)
root.update()
check("…and restored", ui.left_tabs.index("current") == 1)
ui.left_tabs.select(0)
root.update()

# ---- the RAG map section and its loading bar ----------------------------
print("RAG section + loading bar")


def _walk(w):
    yield w
    for c in w.winfo_children():
        yield from _walk(c)


from tkinter import ttk as _ttkm
heads = [w for w in _walk(ui._page_gen)
         if isinstance(w, _ttkm.Label) and str(w.cget("text")).startswith("RAG MAP")]
check("a RAG MAP heading exists on the Image generation tab", len(heads) == 1)
lora_head = [w for w in _walk(ui._page_gen)
             if isinstance(w, _ttkm.Label) and str(w.cget("text")).startswith("LORAS")]
check("…below the LoRA heading",
      lora_head and int(lora_head[0].grid_info().get("row", -1))
      < int(heads[0].grid_info().get("row", 99)))
check("the loading bar is hidden at rest", not ui.rag_prog.grid_info())
gen = ui._ragmap_load_gen
ui._ragmap_loading = ("big.ragmap.json", 925.0)
ui.ui_queue.put(("ragmap_progress", gen, "reading", 0, 0))
ui._poll_queue()
root.update()
check("reading: the bar shows, indeterminate, with the size",
      bool(ui.rag_prog.grid_info()) and str(ui.rag_prog.cget("mode")) == "indeterminate"
      and "925 MB" in ui.rag_prog_var.get(), ui.rag_prog_var.get())
ui.ui_queue.put(("ragmap_progress", gen, "resolving", 5000, 10000))
ui._poll_queue()
root.update()
check("checking references: a real percentage",
      str(ui.rag_prog.cget("mode")) == "determinate" and int(ui.rag_prog["value"]) == 5000
      and "50%" in ui.rag_prog_var.get(), ui.rag_prog_var.get())
ui.ui_queue.put(("ragmap_progress", gen - 1, "resolving", 9000, 10000))
ui._poll_queue()
root.update()
check("progress from a superseded load is ignored", int(ui.rag_prog["value"]) == 5000)
ui.ui_queue.put(("ragmap_loaded", gen, "x", None, RuntimeError("no"), lambda *a: None))
ui._poll_queue()
root.update()
check("the bar hides when the load ends", not ui.rag_prog.grid_info()
      and ui.rag_prog_var.get() == "")

# ---- the wheel over the panel scrolls the panel; the readiness strip ----
print("wheel + readiness strip")
check("every widget on the pages carries the PanelWheel tag first",
      all(w.bindtags()[0] == "PanelWheel" for w in _walk(ui._page_gen))
      and ui.lora_list.bindtags()[0] == "PanelWheel"
      and ui.prompt_box.bindtags()[0] == "PanelWheel")
check("the PanelWheel binding exists",
      bool(root.bind_class("PanelWheel", "<MouseWheel>")))
check("widgets outside the pages are untouched",
      ui.queue_list.bindtags()[0] != "PanelWheel")
check("the readiness strip is hidden when nothing is loading",
      not ui.ready_frame.grid_info())
ui.ui_queue.put(("pending", "engine", "engine starting"))
ui._poll_queue()
root.update()
check("a pending item shows the strip with its text",
      bool(ui.ready_frame.grid_info()) and "engine starting" in ui.ready_var.get(),
      ui.ready_var.get())
ui._ragmap_loading = ("big.ragmap.json", 925.0)
ui._rag_progress(ui._ragmap_load_gen, "resolving", 2500, 10000)
root.update()
check("the RAG map load joins the list with its percentage",
      "RAG map big.ragmap.json (25%)" in ui.ready_var.get(), ui.ready_var.get())
ui.ui_queue.put(("pending", "engine", None))
ui._poll_queue()
root.update()
check("the engine leaving the list keeps the RAG map",
      "engine" not in ui.ready_var.get() and "RAG map" in ui.ready_var.get(),
      ui.ready_var.get())
ui._rag_progress_done()
root.update()
check("the strip hides once everything is ready",
      not ui.ready_frame.grid_info() and ui.ready_var.get() == "")

# ---- tagging images for deletion ---------------------------------------
print("tag and delete")
from PIL import Image as _Img
from tkinter import Toplevel as _Top
with tempfile.TemporaryDirectory() as td:
    paths = []
    for i in range(3):
        p = Path(td) / f"img{i}.png"
        _Img.new("RGB", (64, 64), ("red", "green", "blue")[i]).save(p)
        paths.append(p)
    ui.session = [(_Img.open(p).convert("RGB"),
                   {"model": "m.safetensors", "seed": i}, p)
                  for i, p in enumerate(paths)]
    ui.current = 2
    ui._rebuild_gallery()
    root.update()
    check("one thumbnail per image", len(ui._thumb_btns) == 3)
    check("nothing tagged: the button deletes the selected image",
          ui.delimg_btn.cget("text") == "🗑 Delete image")
    ui._toggle_tag(0)
    ui._toggle_tag(1)
    root.update()
    check("two tagged, by path", ui.tagged == {str(paths[0]), str(paths[1])},
          ui.tagged)
    check("the button says how many it will delete",
          ui.delimg_btn.cget("text") == "🗑 Delete 2 tagged",
          ui.delimg_btn.cget("text"))
    ui._toggle_tag(1)
    root.update()
    check("toggling again untags", ui.tagged == {str(paths[0])})
    ui._tag_current()
    root.update()
    check("the Tag button tags the selected image, and offers to untag",
          str(paths[2]) in ui.tagged and ui.tag_btn.cget("text") == "☐ Untag",
          ui.tag_btn.cget("text"))

    def find_box():
        for w in ui.root.winfo_children():
            if isinstance(w, _Top) and w.winfo_exists() \
                    and w.title() == "Delete tagged images":
                return w
        return None

    from tkinter import ttk as _ttk

    def walk(w):
        yield w
        for c in w.winfo_children():
            yield from walk(c)

    def click(label_start, tries=100):
        """Press the box's button whose text starts with label_start —
        polling until the box exists, since it is modal."""
        w = find_box()
        if w is None:
            if tries:
                root.after(30, lambda: click(label_start, tries - 1))
            return
        for b in walk(w):
            if isinstance(b, _ttk.Button) \
                    and str(b.cget("text")).startswith(label_start):
                b.invoke()
                return

    seen = {}

    def inspect():
        w = find_box()
        if w is None:
            root.after(30, inspect)
            return
        seen["geom"] = w.geometry()
        seen["title"] = w.title()

    def watchdog():          # never let a modal box hang the suite
        w = find_box()
        if w is not None:
            w.destroy()

    root.after(40, inspect)
    root.after(200, lambda: click("Cancel"))
    root.after(6000, watchdog)
    ui._delete_current()
    root.update()
    check("Cancel: nothing deleted",
          all(p.exists() for p in paths) and len(ui.session) == 3)
    check("the box was placed (it has a position, not Windows' default)",
          "+" in seen.get("geom", ""), seen)
    # the editor, border maker and animator point at two of the images —
    # a deleted gallery image used to stay as the swap's face
    ui.ref_paths = [str(paths[0])]
    ui.ref_var.set("selection")
    ui.border_ref_paths = [str(paths[2]), str(paths[1])]
    ui.anim_image_path = str(paths[2])
    ui.anim_img_var.set(paths[2].name)
    root.after(120, lambda: click("Delete"))
    root.after(6000, watchdog)
    ui._delete_current()
    root.update()
    check("a deleted image is dropped from the editor",
          ui.ref_paths == [] and ui.ref_var.get() == "none — text only",
          (ui.ref_paths, ui.ref_var.get()))
    check("…and from the border references, keeping the survivors",
          ui.border_ref_paths == [str(paths[1])], ui.border_ref_paths)
    check("…and from the animator", ui.anim_image_path is None
          and ui.anim_img_var.get() == "none")
    check("the status says what was dropped",
          "editor" in ui.status_var.get() and "animator" in ui.status_var.get(),
          ui.status_var.get())
    check("Delete removes exactly the tagged images from disk",
          not paths[0].exists() and paths[1].exists() and not paths[2].exists())
    check("…and from the gallery",
          [str(t[2]) for t in ui.session] == [str(paths[1])]
          and len(ui._thumb_btns) == 1)
    check("tags are cleared afterwards",
          not ui.tagged and ui.delimg_btn.cget("text") == "🗑 Delete image")
    check("the remaining image is selected", ui.current == 0)
    ui._clear_history()
    root.update()

# ---- a swap whose face file is gone stops before generating -------------
print("swap guard")
real_alive = app.engine_alive
app.engine_alive = lambda *a, **k: True
ui._set(ui.prompt_box, "a hero on a rooftop")
ui.swap_rag_var.set(True)
ui.ref_paths = ["C:/nope/gone_face.png"]
ui.ref_var.set("selection")
ui._generate()
root.update()
check("a missing face stops the run with a clear message",
      "no longer exists" in ui.status_var.get()
      and "Nothing was generated" in ui.status_var.get(), ui.status_var.get())
check("…and the editor lets the missing file go",
      ui.ref_paths == [] and ui.ref_var.get() == "none — text only")
check("…and the app is not left busy", not ui.busy)
ui.swap_rag_var.set(False)
ui._set(ui.prompt_box, "")
app.engine_alive = real_alive

# ---- the startup path: a queued app_update opens the window -------------
# automatic mode (the default): the download starts on its own, the app
# keeps running, and only Restart now hands over to the new exe
print("startup check — automatic")
with tempfile.TemporaryDirectory() as td:
    su.configure(td, td)
    upd = su.Update("v1.99.0", "http://x/a.zip", 75 * 1024 * 1024,
                    "- something new", "2026-08-20")
    staged_tags, installs, discards = [], [], []
    real_stage, real_install = su.stage_update, su.install_staged

    class FakeStaged:
        def discard(self):
            discards.append(1)

    def fake_stage(upd_, cur, status, progress, cancel):
        staged_tags.append(upd_.tag)
        return FakeStaged()

    def fake_install(staged, status):
        installs.append(1)
        return Path(td) / "ComicArtCreator.exe"

    su.stage_update = fake_stage
    su.install_staged = fake_install
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
    pump(root, lambda: win._staged is not None)
    check("the update downloaded and verified on its own",
          staged_tags == ["v1.99.0"] and win._staged is not None)
    check("NOTHING was installed or restarted without approval",
          not installs and not _started and ui.root.winfo_exists())
    check("Install and restart / Not now / Skip this version are offered",
          win.update_btn.cget("text") == "Install and restart"
          and "disabled" not in win.update_btn.state()
          and win.cont_btn.cget("text") == "Not now"
          and win.skip_btn.winfo_manager() == "pack")

    # a second notification must not stack a second window
    ui.ui_queue.put(("app_update", upd))
    ui._poll_queue()
    root.update()
    check("a second notification reuses the open window",
          getattr(ui, "_upd_win") is win)

    # Not now: nothing changes, the app runs on, asked again next time
    win._continue()
    root.update()
    check("Not now closes the window", not win.winfo_exists())
    check("Not now leaves the app running on the old version",
          ui.root.winfo_exists() and not installs)
    check("Not now discards the download", discards == [1])
    check("Not now starts no new process", not _started)
    check("Not now says it will ask again next time",
          "next time" in ui.status_var.get(), ui.status_var.get())
    su.stage_update, su.install_staged = real_stage, real_install

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
