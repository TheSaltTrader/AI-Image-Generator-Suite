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
          + (("  " + str(detail)) if detail and not cond else ""))


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
app.App._load_settings = lambda self: {}  # deterministic: ignore on-disk drift

_started = []
app.subprocess.Popen = lambda *a, **k: _started.append(a)
app.kill_engine = lambda: None

from tkinter import Tk

root = Tk()
root.withdraw()
ui = app.App(root)
check("the AI quality check is on by default (the verified recipe)", ui.decal_judge_var.get() is True)
root.update()

print("the app window")
check("app builds with the updater wired in", ui.root.winfo_exists())

# --- Variations feature (inline: Save Variation / Create more, no pop-ups) ---
check("variations store initialised", getattr(ui, "varsdb", None) is not None)
check("Variations section has its own enable checkbox", hasattr(ui, "var_cb"))
check("Variations dropdown is inline (no picker pop-up)",
      hasattr(ui, "variation_dd") and not hasattr(ui, "_pick_variation"))
# Cloning's own "Make" count was removed — Generate and the generate page's
# "Variations" count handle quantity now (the section "Create more" button is
# checked below, once _walk is defined)
check("Cloning no longer has its own 'Make' count", not hasattr(ui, "var_count_var"))
for _m in ("_save_variation", "_create_more", "_on_variation_pick",
           "_delete_variation", "_refresh_variation_dd", "_apply_variation",
           "_apply_variation_lock", "_clear_variation", "_on_variation_toggle",
           "_apply_variation_enabled", "_export_variations",
           "_import_variations"):
    check("variations method %s wired" % _m,
          callable(getattr(ui, _m, None)))
# Variations is its own section with a greyable body, off by default
check("Variations has its own greyable body", hasattr(ui, "variation_body"))
try:
    ui.var_enable_var.set(False); ui._apply_variation_enabled()
    _dd_off = "disabled" in (ui.variation_dd.state() or ())
    ui.var_enable_var.set(True); ui._apply_variation_enabled()
    _dd_on = "disabled" not in (ui.variation_dd.state() or ())
    ui.var_enable_var.set(False); ui._apply_variation_enabled()
    check("Variations picker greyed when off, selectable when on",
          _dd_off and _dd_on)
except Exception as _e:
    check("Variations enable toggles the body", False, str(_e))
# a locked variation must force face-swap on and grey the model/method
try:
    ui.var_enable_var.set(False)
    ui._apply_variation_lock()
    _model_free = "disabled" not in (ui.model_dd.state() or ())
    check("model/method NOT greyed when no variation is locked", _model_free)
except Exception as _e:
    check("variation lock toggles cleanly (off)", False, str(_e))

# --- Anatomy guard ---
check("anatomy guard toggle exists", hasattr(ui, "anatomy_var"))
try:
    _p = dict(model="Juggernaut-XL-v9.safetensors", prompt="a woman",
              negative="", style="", loras=[], width=1408, height=1408,
              seed=1, steps=30, cfg=6.0, anatomy_guard=True, batch=1)
    _g = app.build_graph(_p)
    check("anatomy guard caps the base to native",
          max(_g["5"]["inputs"]["width"], _g["5"]["inputs"]["height"])
          <= app.ANATOMY_NATIVE_MAX)
    check("anatomy guard adds an upscale-to-target refine",
          "62" in _g and _g["62"]["inputs"]["width"] == 1408)
    _p2 = dict(_p); _p2["anatomy_guard"] = False
    _g2 = app.build_graph(_p2)
    check("no anatomy nodes when the guard is off",
          _g2["5"]["inputs"]["width"] == 1408 and "62" not in _g2)
except Exception as _e:
    check("anatomy guard build_graph works", False, str(_e))

# --- Variations count goes up to 100; the Hi-res fix has been removed ---
check("the Variations count goes up to 100",
      hasattr(ui, "batch_sb")
      and str(ui.batch_sb.cget("to")) in ("100", "100.0"))
check("the Hi-res fix is gone (no hires toggle)",
      not hasattr(ui, "hires_var") and not hasattr(ui, "hires_scale_var"))
try:
    _ph = dict(model="Juggernaut-XL-v9.safetensors", prompt="a woman",
               negative="", style="", loras=[], width=1024, height=1024,
               seed=1, steps=30, cfg=6.0, batch=1)
    _qt = [v["class_type"] for v in app.build_graph(_ph).values()]
    check("a plain generation has ONE sampling pass (no hi-res second pass)",
          _qt.count("KSampler") == 1 and "LatentUpscaleBy" not in _qt, _qt)
    # a stale saved recipe carrying hires=True must NOT resurrect the pass
    _qt2 = [v["class_type"] for v in
            app.build_graph(dict(_ph, hires=True, hires_scale=1.5)).values()]
    check("a stale hires flag is ignored (feature removed)",
          _qt2.count("KSampler") == 1 and "LatentUpscaleBy" not in _qt2)
except Exception as _e:
    check("hi-res-removed build_graph works", False, str(_e))

# --- preview redraw is debounced (smooth panel-sash dragging) ---
check("preview resize is coalesced via a debounced handler",
      callable(getattr(ui, "_on_canvas_configure", None)))
try:
    ui._redraw_after = None
    ui._on_canvas_configure()
    check("a resize schedules a single pending redraw",
          getattr(ui, "_redraw_after", None) is not None)
    ui.root.after_cancel(ui._redraw_after)
    ui._redraw_after = None
except Exception as _e:
    check("debounced redraw schedules", False, str(_e))

# --- QoL: batch ETA formatter, window/sash persistence, keep-model-resident ---
check("_fmt_duration is compact and human",
      app._fmt_duration(45) == "45s" and app._fmt_duration(240) == "4m"
      and app._fmt_duration(90) == "1m 30s" and app._fmt_duration(3660) == "1h 1m")
try:
    _ws = ui._collect_window_state()
    check("window state captures the sash positions",
          "sash_h" in _ws and "sash_v" in _ws)
    check("window state captures geometry or zoomed",
          "geometry" in _ws or _ws.get("zoomed"))
    # a saved sash is honoured by _init_sashes (clamped) — needs a real size,
    # so deiconify briefly (the suite's root is normally withdrawn)
    ui.settings["window"] = {"sash_h": 520, "sash_v": 400}
    ui.root.deiconify(); ui.root.geometry("1400x900")
    ui.root.update_idletasks(); ui.root.update()
    ui._init_sashes(); ui.root.update_idletasks()
    _sp = ui.main_paned.sashpos(0)
    ui.root.withdraw()
    check("_init_sashes restores a saved sash position", abs(_sp - 520) <= 4, _sp)
except Exception as _e:
    check("window/sash persistence works", False, str(_e))
# keep-model-resident preference (adds --highvram at engine launch)
check("keep-model-in-VRAM toggle exists, off by default",
      hasattr(ui, "keep_resident_var") and not ui.keep_resident_var.get())
try:
    ui.keep_resident_var.set(True); ui._on_keep_resident()
    check("keep-resident writes the pref for start_engine",
          ui.settings.get("prefs", {}).get("keep_model_resident") is True)
    ui.keep_resident_var.set(False); ui._on_keep_resident()
except Exception as _e:
    check("keep-resident pref persists", False, str(_e))
# threaded rebuild is wired (no UI-thread PNG decode) via the queue kinds
check("rebuild-from-pictures runs off the UI thread (guard flag)",
      hasattr(ui, "_rebuild_history") and callable(ui._rebuild_history))

# --- anatomy negative embedding (one-time download, wired into the guard) ---
check("anatomy embedding constant set",
      getattr(app, "ANATOMY_EMBED", None) == "negativeXL_D")
try:
    import json as _json
    _man = _json.load(open(app.MANIFEST_FILE, encoding="utf-8"))
    _emb = [e for e in _man if e.get("local") == "negativeXL_D.safetensors"]
    check("negative embedding is in the model manifest", len(_emb) == 1)
    check("embedding manifest entry is well-formed",
          bool(_emb) and _emb[0].get("dir") == "embeddings"
          and _emb[0].get("repo") and _emb[0].get("remote_file"))
except Exception as _e:
    check("embedding manifest entry present", False, str(_e))

# --- red DELETE ALL danger button ---
check("Delete-all button is the red Danger style",
      str(ui.delfiles_btn.cget("style")) == "Danger.TButton")

# --- Rebuild history from pictures ---
check("rebuild-from-pictures handler + png parser exist",
      callable(getattr(ui, "_rebuild_history", None))
      and callable(getattr(ui, "_params_from_png", None)))

# --- Incognito toggle (hide images + history) ---
check("incognito eye button exists next to the badges",
      hasattr(ui, "incog_btn"))
check("incognito starts off (images shown)", not ui.incognito_var.get())
try:
    ui._toggle_incognito()
    check("incognito ON removes the preview from view",
          ui.incognito_var.get() and ui.canvas.grid_info() == {})
    check("incognito ON hides the gallery/history",
          ui._gwrap.grid_info() == {})
    check("incognito ON shows the hidden-cover", bool(ui._incog_cover.grid_info()))
    ui._toggle_incognito()
    check("incognito OFF restores the preview",
          (not ui.incognito_var.get()) and bool(ui.canvas.grid_info()))
    check("incognito OFF restores the gallery", bool(ui._gwrap.grid_info()))
except Exception as _e:
    check("incognito toggles cleanly", False, str(_e))

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
check("five tabs, in order",
      tabs == ["Image generation", "Animation", "Borders", "Edit image",
               "Decals"], tabs)
# ---- equal-width tab strip with ◀ ▶ arrows (v2.15) ------------------------
print("tab strip")
check("the left tabs have a TabStrip", hasattr(ui, "tab_strip"))
_ts = ui.tab_strip
# Style.layout(name, []) disables a layout by setting it to the word "null"
# (tkinter's documented way to hide notebook tabs) — so it reads back as
# [("null", {})], not as an empty list
_lay = app.ttk.Style().layout("Left.TNotebook.Tab")
check("the notebook's own headers are hidden (disabled tab layout)",
      ui.left_tabs.cget("style") == "Left.TNotebook"
      and (not _lay or [e[0] for e in _lay] == ["null"]), _lay)
check("the strip has left/right arrow buttons",
      hasattr(_ts, "left_btn") and hasattr(_ts, "right_btn"))
_tw, _vis = _ts.layout(avail=600)
check("every tab gets the SAME width (the strip shared out evenly)",
      _tw == 600 // 5 and _vis == 5, (_tw, _vis))
check("wide enough: all five fit, nothing to scroll", not _ts.can_scroll(avail=600))
_ts.refresh(avail=600, ensure=True)
check("arrows disabled when everything fits",
      "disabled" in _ts.left_btn.state() and "disabled" in _ts.right_btn.state())
check("too narrow: tabs keep their minimum width and the strip scrolls",
      _ts.can_scroll(avail=200) and _ts.layout(avail=200)[0] >= _ts.MIN_W,
      _ts.layout(avail=200))
_ts.refresh(avail=200, ensure=True)
check("narrow: the right arrow comes alive (more tabs to the right)",
      "disabled" not in _ts.right_btn.state()
      and "disabled" in _ts.left_btn.state())
_ts.scroll(1, avail=200)
check("the arrow scrolls the window of visible tabs",
      _ts.offset == 1 and "disabled" not in _ts.left_btn.state(), _ts.offset)
ui.left_tabs.select(4); root.update()
_ts.refresh(avail=200, ensure=True)
check("selecting a tab keeps it in view", _ts.offset + _ts.visible > 4,
      (_ts.offset, _ts.visible))
ui.left_tabs.select(0); root.update()
_ts.refresh(avail=600, ensure=True)
check("back to wide: the strip snaps to the start", _ts.offset == 0)


class _Click:
    x = _tw * 2 + 5          # inside the third tab


_ts._click(_Click())
root.update()
check("clicking the strip selects the tab under the pointer",
      ui.left_tabs.index("current") == 2, ui.left_tabs.index("current"))
ui.left_tabs.select(0); root.update()


def page_of(widget):
    """The tab page a widget sits on, or None when it is outside the tabs."""
    w = widget
    while w is not None and w.master is not ui.left_tabs:
        w = w.master
    return str(w) if w is not None else None


pages = list(ui.left_tabs.tabs())
check("the prompt lives on Image generation", page_of(ui.prompt_box) == pages[0])
# Generate is pinned in the always-visible bottom bar now (no scrolling to run)
check("Generate is pinned outside the scrolling tabs",
      page_of(ui.go_btn) is None
      and str(ui.go_btn).startswith(str(ui._gen_pinned)))
try:
    ui.left_tabs.select(pages[1]); ui._on_left_tab()
    check("pinned Generate is hidden off the Image tab",
          ui._gen_pinned.grid_info() == {})
    ui.left_tabs.select(pages[0]); ui._on_left_tab()
    check("pinned Generate returns on the Image tab",
          bool(ui._gen_pinned.grid_info()))
except Exception as _e:
    check("pinned Generate tab-visibility toggles", False, str(_e))
# resizable panels: the controls↔image split and the preview↔gallery split are
# both PanedWindows the user can drag
def _has_ancestor(w, target):
    while w is not None:
        if w is target:
            return True
        w = w.master
    return False
check("the controls/image split is a draggable PanedWindow",
      isinstance(getattr(ui, "main_paned", None), app.ttk.PanedWindow))
check("the preview/gallery split is a draggable PanedWindow",
      isinstance(getattr(ui, "right_paned", None), app.ttk.PanedWindow))
check("the controls panel is a pane of the main split",
      _has_ancestor(ui.left_tabs, ui.main_paned))
check("the preview is a pane of the vertical split",
      _has_ancestor(ui.canvas, ui.right_paned))
check("the gallery is a pane of the vertical split",
      _has_ancestor(ui._gwrap, ui.right_paned))
check("both splits expose one draggable sash",
      len(ui.main_paned.panes()) == 2 and len(ui.right_paned.panes()) == 2)
check("the LoRA list lives on Image generation", page_of(ui.lora_list) == pages[0])
check("the animator lives on Animation", page_of(ui.anim_prompt_box) == pages[1])
check("the border maker lives on Borders", page_of(ui.border_prompt_box) == pages[2])
check("the batch queue stays under the tabs, on every tab",
      page_of(ui.queue_list) is None and ui.queue_list.winfo_toplevel() is ui.root)
check("the version row stays under the tabs", page_of(ui.upd_btn) is None)
check("every page scrolls with the wheel", len(ui._scroll_canvases) == 5)
check("the edit box and Apply live on the Edit tab",
      page_of(ui.edit_prompt_box) == pages[3] and page_of(ui.editor_use_btn) == pages[3])
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

# ---- Decals tab: clean up scanned stickers into print-ready transparent art ----
print("decals tab")
check("the Decals tab exists", hasattr(ui, "_page_decals"))
for _w in ("decal_list", "decal_mode_var", "decal_removebg_var",
           "decal_tol_var", "decal_dpi_var",
           "decal_trim_var", "decal_btn"):
    check("Decals control %s wired" % _w, hasattr(ui, _w))
check("Decals methods wired",
      all(callable(getattr(ui, m, None)) for m in
          ("_add_decal_sources", "_clear_decal_sources", "_process_decals")))
check("Decals faithful defaults: cleanup, remove-bg on, native 300 DPI, "
      "auto-colour OFF",
      ui.decal_mode_var.get() == "cleanup" and ui.decal_removebg_var.get()
      and ui.decal_dpi_var.get() == "300" and not ui.decal_wb_var.get())
# the pipeline module itself, headless, on a synthetic tinted-carrier decal
import importlib
_dec = importlib.import_module("decals")
check("decals supports PDF + common image formats",
      ".pdf" in _dec.SUPPORTED_EXTS and ".png" in _dec.SUPPORTED_EXTS
      and ".jpg" in _dec.SUPPORTED_EXTS and ".webp" in _dec.SUPPORTED_EXTS)
try:
    import numpy as _np
    # light-blue carrier with a black square and a white square on it
    _arr = _np.full((80, 120, 3), (214, 240, 242), _np.uint8)
    _arr[20:60, 10:40] = (0, 0, 0)        # black art
    _arr[20:60, 80:110] = (255, 255, 255)  # white ink
    _src = app.Image.fromarray(_arr, "RGB")
    _res = _dec.process_image(_src, mode="cleanup", remove_bg=True, denoise=0,
                              tol=52, target_dpi=300, native_dpi=300)
    _out = _np.asarray(_res["rgba"])
    _a = _out[..., 3]
    check("cleanup removes the carrier background (corner transparent)",
          _a[2, 2] < 40, int(_a[2, 2]))
    check("cleanup keeps the black art opaque",
          _a[40, 25] > 200, int(_a[40, 25]))
    check("cleanup keeps the white ink opaque (not eaten as background)",
          _a[40, 95] > 200, int(_a[40, 95]))
    # vector mode returns an SVG + a raster (needs vtracer)
    try:
        _rv = _dec.process_image(_src, mode="vector", remove_bg=True, denoise=0,
                                 tol=52, target_dpi=300, native_dpi=300)
        check("vector mode returns an SVG and a transparent raster",
              bool(_rv.get("svg")) and _rv["rgba"].mode == "RGBA")
    except Exception as _ve:
        check("vector mode returns an SVG and a transparent raster", False,
              "vtracer/pymupdf missing: " + str(_ve))
except Exception as _e:
    check("decals pipeline runs headless", False, repr(_e))
# scale conversion (e.g. 3.75" / 1/18 -> 1/12 Classified = 1.5x)
check("decals scale presets include 1/12 and 1/18",
      _dec.SCALE_N.get('1/12 — 6" Classified') == 12
      and _dec.SCALE_N.get('1/18 — 3.75" (ARAH / Retro)') == 18)
check("3.75\"->Classified enlarges 1.5x", abs(_dec.scale_factor(18, 12) - 1.5) < 1e-6)
check("Classified->3.75\" shrinks to ~0.667x", abs(_dec.scale_factor(12, 18) - 2/3) < 1e-6)
check("the Decals tab has From/To scale pickers",
      hasattr(ui, "decal_src_scale") and hasattr(ui, "decal_tgt_scale"))
check("scale defaults to no resize (1/12 -> 1/12)",
      abs(ui._decal_scale_factor() - 1.0) < 1e-6)
try:
    ui.decal_src_scale.set('1/18 — 3.75" (ARAH / Retro)')
    ui.decal_tgt_scale.set('1/12 — 6" Classified')
    root.update()
    check("picking 3.75->Classified computes 1.5x in the UI",
          abs(ui._decal_scale_factor() - 1.5) < 1e-6)
    _rs = _dec.process_image(_src, mode="cleanup", remove_bg=False, denoise=0,
                             target_dpi=300, native_dpi=300, size_scale=1.5)
    check("size_scale enlarges the output 1.5x",
          abs(_rs["rgba"].width - _src.width * 1.5) <= 2)
    ui.decal_src_scale.set('1/12 — 6" Classified')
except Exception as _e:
    check("decals scale conversion works", False, repr(_e))
# scanner streak removal (on) + colour cast correction (off = faithful) + AI upscale
check("Decals: remove-lines ON, auto-colour OFF (faithful), AI-upscale toggle",
      hasattr(ui, "decal_lines_var") and ui.decal_lines_var.get()
      and hasattr(ui, "decal_wb_var") and not ui.decal_wb_var.get()
      and hasattr(ui, "decal_ai_var") and not ui.decal_ai_var.get())
check("AI-upscale helper is wired", callable(getattr(ui, "_decal_ai_upscale", None)))
try:
    import numpy as _np3
    _c = _np3.full((60, 90, 3), (214, 240, 242), _np3.uint8)   # blue carrier
    _c[20:50, 10:40] = (206, 22, 30)                            # a red decal
    _fim = app.Image.fromarray(_c, "RGB")
    _fr = _dec.process_image(_fim, mode="cleanup", remove_bg=True, balance=False,
                             remove_lines=False, denoise=0, tol=52,
                             target_dpi=300, native_dpi=300)
    _fa = _np3.asarray(_fr["rgba"])
    _px = _fa[35, 25]                       # centre of the red decal
    check("faithful mode keeps the exact art colour (no white-balance shift)",
          tuple(int(x) for x in _px[:3]) == (206, 22, 30) and _px[3] > 200,
          tuple(int(x) for x in _px))
except Exception as _e:
    check("faithful colour fidelity", False, repr(_e))
# dedicated "Redraw to vector" button + forced mode
check("Decals has a Redraw-to-vector (AI) button",
      hasattr(ui, "decal_vec_btn")
      and "Redraw" in ui.decal_vec_btn.cget("text"))
ui.decal_sources = []
ui._process_decals("vector")   # force_mode accepted; graceful with no sources
check("trace-to-vector force_mode is accepted (no-op with no files)",
      "add" in ui.decal_status_var.get().lower())
# edge/halo cleanup toggle + clean_matte
check("Decals has an edge/halo cleanup toggle, on by default",
      hasattr(ui, "decal_tidy_var") and ui.decal_tidy_var.get())
try:
    import numpy as _np4
    _ca = _np4.zeros((40, 40, 4), _np4.uint8)
    _ca[10:30, 10:30] = (0, 0, 0, 255)      # solid opaque art block
    _ca[2, 2] = (0, 0, 0, 255)              # a stray 1px speckle in the bg
    _ca[20, 35, 3] = 40                     # a faint halo pixel
    _cleaned = _np4.asarray(_dec.clean_matte(app.Image.fromarray(_ca, "RGBA")))
    check("clean_matte removes stray background speckle", _cleaned[2, 2, 3] == 0)
    check("clean_matte drops the faint halo pixel", _cleaned[20, 35, 3] == 0)
    check("clean_matte keeps the solid art interior opaque",
          _cleaned[20, 20, 3] == 255)
except Exception as _e:
    check("clean_matte works", False, repr(_e))
# solidify black: patchy dark grey -> pure black, greys/colours left alone
check("Decals has a solidify-black toggle, OFF by default (verified recipe)",
      hasattr(ui, "decal_solid_var") and not ui.decal_solid_var.get())
try:
    import numpy as _np5
    _sb = _np5.array([[[40, 44, 38], [128, 128, 128], [150, 20, 24]]], _np5.uint8)
    _sr = _np5.asarray(_dec.solidify_black(app.Image.fromarray(_sb, "RGB")))
    check("solidify snaps patchy near-black to pure black",
          tuple(int(x) for x in _sr[0, 0]) == (0, 0, 0))
    check("solidify leaves medium grey and dark colour untouched",
          tuple(int(x) for x in _sr[0, 1]) == (128, 128, 128)
          and tuple(int(x) for x in _sr[0, 2]) == (150, 20, 24))
except Exception as _e:
    check("solidify_black works", False, repr(_e))
# smooth colour mottling (edge-preserving)
check("Decals has a smooth-mottling toggle, off by default",
      hasattr(ui, "decal_smooth_var") and not ui.decal_smooth_var.get())
try:
    import numpy as _np6
    _rng6 = _np6.random.default_rng(3)
    _blk = (180 + _rng6.integers(-12, 13, size=(50, 50, 3))).astype(_np6.uint8)
    _sm = _np6.asarray(_dec.smooth_flats(app.Image.fromarray(_blk, "RGB")))
    check("smooth_flats reduces mottling inside flat areas",
          _sm.std() < _blk.std(), (round(float(_sm.std()), 1),
                                   round(float(_blk.std()), 1)))
except Exception as _e:
    check("smooth_flats works", False, repr(_e))
# text-driven Generate -> SVG (original art from a prompt); do NOT call it here
# (it would hit a live engine) — just verify it's wired and graceful is coded
check("Decals has a Generate->SVG prompt box + button + method",
      hasattr(ui, "decal_prompt_box") and hasattr(ui, "decal_gen_btn")
      and callable(getattr(ui, "_generate_decal", None)))
# _generate tags a decal-gen run so _finish_image traces it to SVG
check("decal-gen flag defaults off (normal generation unaffected)",
      not getattr(ui, "_decal_gen", False))

# ---- AI redraw to vector (v2.15): red buttons, segmentation, palette snap,
# ---- sheet rebuild at the printed size, the per-decal engine graph ----------
print("decals AI redraw")
# ---- v2.16: redraw methods, vision model, grouping, preview, film preview --
_want_m = "recraft" if app.recraft_vectorize.get_key() else "trace"
check("redraw method defaults to the verified recipe: Recraft with a fal key, "
      "else the clean trace",
      hasattr(ui, "decal_method_var") and ui.decal_method_var.get() == _want_m,
      ui.decal_method_var.get())
check("the vision model picker lists Opus 5.5 (default) and Sonnet 5.5",
      hasattr(ui, "decal_vision_model_var")
      and ui._vision_model_id() == "claude-opus-5-5"
      and any(m == "claude-sonnet-5-5" for _l, m in app.vector_redraw.MODELS))
ui.decal_vision_model_var.set(app.vector_redraw.MODELS[1][0])
check("picking the cheaper option resolves to Sonnet 5.5",
      ui._vision_model_id() == "claude-sonnet-5-5")
ui.decal_vision_model_var.set(app.vector_redraw.MODELS[0][0])
check("API key button + status label + preview button are wired",
      hasattr(ui, "decal_key_btn") and hasattr(ui, "decal_key_lab")
      and hasattr(ui, "decal_preview_btn")
      and callable(getattr(ui, "_set_vision_key", None)))
check("the API key is never part of the saved UI state",
      not any("key" in k.lower() and "decal" in k.lower()
              for k in ui._collect_ui_state().keys())
      and "sk-ant" not in json.dumps(ui._collect_ui_state()))
check("grouping distance defaults to 1.35 mm and is saved with the UI state",
      abs(float(ui.decal_gap_var.get()) - 1.35) < 1e-6
      and abs(float(ui._collect_ui_state().get("decal_gap", 0)) - 1.35) < 1e-6
      and ui._collect_ui_state().get("decal_method") == _want_m)
_im6 = app.Image.new("RGB", (1200, 600), "white")
_w6, _d6 = app.decals.to_working_dpi(_im6, 600)
_w7, _d7 = app.decals.to_working_dpi(_im6, 300)
check("a 600 dpi page is worked at 300 dpi (the verified resolution); a 300 dpi page is left as it is",
      _w6.size == (600, 300) and _d6 == 300 and _w7.size == (1200, 600) and _d7 == 300,
      (_w6.size, _d6, _w7.size, _d7))
_ug = app.build_decal_upscale_graph({"width": 640, "height": 448, "esrgan": True,
                                     "ref_image_name": "in.png"})
check("the clean-trace graph is ESRGAN + scale only (no sampler)",
      [v["class_type"] for v in _ug.values()]
      == ["LoadImage", "UpscaleModelLoader", "ImageUpscaleWithModel",
          "ImageScale", "SaveImage"])
_film = app._on_film(app.Image.new("RGBA", (4, 4), (255, 255, 255, 255)),
                     (214, 240, 242))
check("gallery previews sit on the film colour (white ink stays visible)",
      _film.getpixel((1, 1)) == (255, 255, 255)
      and app._on_film(app.Image.new("RGBA", (4, 4), (0, 0, 0, 0)),
                       (214, 240, 242)).getpixel((1, 1)) == (214, 240, 242)
      and app._on_film(app.Image.new("RGBA", (4, 4), (0, 0, 0, 0)),
                       (250, 247, 246)).getpixel((1, 1)) == (205, 215, 225))
try:
    import numpy as _np5
    # size-aware grouping: two BIG decals 12 px apart stay separate; a small
    # satellite 8 px under a big one joins it; letters join into a word
    _g = _np5.zeros((200, 300, 4), _np5.uint8)
    _g[20:120, 20:120] = (206, 22, 30, 255)        # big A
    _g[20:120, 132:232] = (20, 60, 200, 255)       # big B, 12 px to the right
    _g[128:140, 30:60] = (0, 0, 0, 255)            # small caption 8 px under A
    for _x in (150, 170, 190, 210):                # four "letters" 8 px under B
        _g[128:140, _x:_x + 8] = (0, 0, 0, 255)
    _gb = _dec.segment_decals(app.Image.fromarray(_g, "RGBA"), gap=16, pad=0)
    check("two big decals near each other stay separate; small marks join",
          len(_gb) == 2 and _gb[0][3] >= 140 and _gb[1][3] >= 140, _gb)
    check("gap=0 keeps every piece on its own",
          len(_dec.segment_decals(app.Image.fromarray(_g, "RGBA"), gap=0,
                                  pad=0, min_side=6)) == 7)
except Exception as _e:
    check("size-aware grouping", False, repr(_e))
# vector results carry their SVG: the preview zooms from it, the caption says
# so, Save As hands out the SVG, delete takes the twin PNG too
try:
    import tempfile as _tf
    _vd = Path(_tf.mkdtemp())
    _vsvg = _vd / "demo.svg"
    _vsvg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="0.5in" '
                     'height="0.2in" viewBox="0 0 150 60">'
                     '<rect x="6" y="8" width="104" height="44" fill="#e04231"/></svg>',
                     encoding="utf-8")
    _vpng = _vd / "demo.png"
    _vimg = app.Image.new("RGB", (150, 60), (214, 240, 242))
    _vimg.save(_vpng)
    _vparams = {"model": "decal", "seed": "demo", "user_prompt": "demo",
                "svg": str(_vsvg), "png": str(_vpng), "film": (214, 240, 242),
                "size_in": (0.5, 0.2)}
    ui.session.append((_vimg, _vparams, str(_vsvg)))
    ui.current = len(ui.session) - 1
    ui._show_current()
    check("a vector result is captioned as SVG with its printed size",
          "SVG" in ui.info_var.get() and "0.50 × 0.20 in" in ui.info_var.get(),
          ui.info_var.get())
    _calls = []
    _real_rr = app.vector_redraw.render_svg_region
    app.vector_redraw.render_svg_region = lambda *a, **k: _calls.append((a, k)) or _real_rr(*a, **k)
    ui._zoom, ui._view_c = 3.0, None
    ui._draw_frame(_vimg)
    app.vector_redraw.render_svg_region = _real_rr
    check("zooming a vector result renders from the SVG (sharp), not the raster",
          len(_calls) == 1 and _calls[0][1].get("backing") == (214, 240, 242))
    ui._zoom, ui._view_c = 1.0, None
    check("Save As copies the SVG for an .svg name and the PNG for a .png name",
          ui._save_source(_vparams, str(_vsvg), r"C:\x\out.svg") == str(_vsvg)
          and ui._save_source(_vparams, str(_vsvg), r"C:\x\out.png") == str(_vpng)
          and ui._save_source({"model": "x"}, r"C:\y\a.png", r"C:\x\o.png") == r"C:\y\a.png")
    ui._add_thumb(ui.current)
    ui._delete_paths([str(_vsvg)])
    check("deleting a vector result removes both the SVG and its PNG",
          not _vsvg.exists() and not _vpng.exists())
except Exception as _e:
    import traceback
    traceback.print_exc()
    check("vector results in the gallery", False, repr(_e))
check("Decals action buttons are red Go buttons (like the other tabs' generate)",
      all(getattr(ui, n).cget("style") == "Go.TButton"
          for n in ("decal_btn", "decal_vec_btn", "decal_gen_btn")))
check("redraw strength defaults to a gentle 0.35, exact-colour lock on",
      abs(ui.decal_redraw_var.get() - 0.35) < 1e-6
      and ui.decal_palette_var.get())
check("the AI redraw worker is wired",
      callable(getattr(ui, "_redraw_decals", None)))
ui.decal_sources = []
ui._redraw_decals()
check("redraw with no sources says to add files (no crash)",
      "add" in ui.decal_status_var.get().lower(), ui.decal_status_var.get())
check("the Decals buttons are enabled again after a no-op",
      "disabled" not in ui.decal_vec_btn.state())
try:
    import numpy as _np4
    # a sheet: two decals far apart on a transparent background; the first
    # has a black detail inside its red block
    _sh = _np4.zeros((300, 400, 4), _np4.uint8)
    _sh[40:120, 30:130] = (206, 22, 30, 255)
    _sh[60:100, 50:110] = (0, 0, 0, 255)
    _sh[180:260, 250:370] = (20, 60, 200, 255)
    _sheet = app.Image.fromarray(_sh, "RGBA")
    _boxes = _dec.segment_decals(_sheet)
    check("segment_decals finds the two decals", len(_boxes) == 2, _boxes)
    check("…in reading order (top-left first), padded around the art",
          len(_boxes) == 2 and _boxes[0][0] < _boxes[1][0]
          and _boxes[0][0] <= 30 and _boxes[0][2] >= 130, _boxes)
    _pal = _dec.palette_of(_sheet.crop(_boxes[0]))
    _pal_set = {tuple(int(v) for v in c) for c in _pal}
    check("palette_of returns the decal's exact scan colours",
          (206, 22, 30) in _pal_set and (0, 0, 0) in _pal_set
          and len(_pal_set) <= 3, _pal_set)
    _noisy = app.Image.fromarray(
        _np4.array([[[210, 30, 25], [3, 2, 0]]], _np4.uint8), "RGB")
    _snapped = _np4.asarray(_dec.snap_palette(_noisy, _pal))
    check("snap_palette maps near colours onto the exact palette",
          tuple(int(v) for v in _snapped[0, 0]) == (206, 22, 30)
          and tuple(int(v) for v in _snapped[0, 1]) == (0, 0, 0))
    _calls = []

    def _fake_refine(rgb, size):
        _calls.append((rgb.size, size))
        return rgb.resize(size)        # an identity "AI" at the work size

    # plain rectangles are straight-line art (rebuilt without the AI):
    # first that path, then the AI path with it switched off
    _geo_st = {}
    _gout = _dec.redraw_sheet(_sheet, _fake_refine, native_dpi=100,
                              size_scale=1.5, target_dpi=100, stats=_geo_st)
    check("straight-line decals are rebuilt from their outlines, no AI call "
          "(GI JOE banners: straight letters and flag)",
          not _calls and len(_gout["items"]) == 2
          and _geo_st.get("geometric") == 2
          and "<path" in _gout["items"][0]["svg"], (_calls, _geo_st))
    _orig_geo = _dec.geometric_svg
    _dec.geometric_svg = lambda *a, **k: None
    try:
        _out = _dec.redraw_sheet(_sheet, _fake_refine, native_dpi=100,
                                 size_scale=1.5, target_dpi=100)
    finally:
        _dec.geometric_svg = _orig_geo
    check("redraw_sheet redraws every decal once",
          len(_calls) == 2 and len(_out["items"]) == 2, (_calls, len(_out["items"])))
    # thin WHITE ink on its own (a white label on the film) must survive the
    # edge-band fringe rule — it used to be dropped as "light = background"
    _wi = _np4.zeros((120, 200, 4), _np4.uint8)
    _wi[50:60, 20:180] = (255, 255, 255, 255)          # a 10 px white stroke
    _wo = _dec.redraw_sheet(app.Image.fromarray(_wi, "RGBA"),
                            lambda rgb, size: rgb.resize(size),
                            native_dpi=100, size_scale=1.0, target_dpi=100,
                            keep_palette=True, min_side=6)
    _woa = _np4.asarray(_wo["rgba"]) if _wo["rgba"] is not None else None
    check("thin white ink survives the clean trace",
          _woa is not None and _woa[55, 100][3] > 200
          and tuple(int(v) for v in _woa[55, 100][:3]) == (255, 255, 255)
          and (_woa[50:60, 20:180, 3] > 128).mean() > 0.8,
          None if _woa is None else
          (tuple(_woa[55, 100]), float((_woa[50:60, 20:180, 3] > 128).mean())))
    check("the crop goes to the AI at scan size; the canvas is /8 and >= floor",
          all(r[0] < 200 and s[0] % 8 == 0 and s[1] % 8 == 0 and max(s) >= 640
              for r, s in _calls), _calls)
    check("the sheet SVG states its printed size (scale applied) + viewBox",
          'width="6.0000in"' in _out["svg"] and 'height="4.5000in"' in _out["svg"]
          and 'viewBox="0 0 400 300"' in _out["svg"], _out["svg"][:200])
    check("each decal SVG has its own physical size",
          all('in"' in it["svg"] and "viewBox" in it["svg"]
              for it in _out["items"]))
    check("the sheet raster is at target DPI × scale (600×450)",
          _out["rgba"].size == (600, 450), _out["rgba"].size)
    _ra = _np4.asarray(_out["rgba"])
    check("the redrawn decals land in place with the exact colours",
          tuple(int(v) for v in _ra[70, 60][:3]) == (206, 22, 30)
          and _ra[70, 60][3] > 200
          and tuple(int(v) for v in _ra[120, 120][:3]) == (0, 0, 0)
          and tuple(int(v) for v in _ra[330, 465][:3]) == (20, 60, 200),
          (tuple(_ra[70, 60]), tuple(_ra[120, 120]), tuple(_ra[330, 465])))
    check("the background stays transparent",
          _ra[10, 10][3] == 0 and _ra[200, 150][3] == 0)
    check("cancel aborts the redraw",
          _dec.redraw_sheet(_sheet, _fake_refine, cancelled=lambda: True) is None)
    # the fringe fix: where the AI's shape sits inside the scan's outline the
    # gap holds the AI's white background — that band must go, the interior
    # must stay (an all-white "AI" result is the extreme case)
    _ow = _dec.redraw_sheet(
        _sheet, lambda rgb, size: app.Image.new("RGB", size, (255, 255, 255)),
        native_dpi=100, size_scale=1.0, target_dpi=100, keep_palette=False)
    _oa = _np4.asarray(_ow["rgba"])[..., 3] > 128
    _full = int((_sh[..., 3] > 0).sum())
    check("background-coloured pixels along the outline are dropped, interior kept",
          0 < int(_oa.sum()) < _full and bool(_oa[80, 80]),
          (int(_oa.sum()), _full))
    # the tracer hazard: a decal that fills most of its crop must not come
    # back as a solid rectangle (the key-coloured margin keeps the key as
    # the base layer)
    _blk = _np4.zeros((200, 200, 4), _np4.uint8)
    _blk[20:180, 20:180] = (206, 22, 30, 255)
    _bs, _br = _dec.vectorize(app.Image.fromarray(_blk, "RGBA"), target_px=200,
                              quantize_colors=0, presmooth=False)
    _ba = _np4.asarray(_br)[..., 3] > 128
    check("vectorize keeps the background transparent when ink dominates the crop",
          not _ba[2, 2] and _ba[100, 100]
          and abs(int(_ba.sum()) - 160 * 160) < 1200, int(_ba.sum()))
    # the palette ignores edge blends: a red block with pinkish anti-aliased
    # edge pixels must give a red (+black) palette, no pink
    _edge = _sh.copy()
    _edge[40, 30:130] = (230, 140, 150, 255)
    _edge[119, 30:130] = (230, 140, 150, 255)
    _pe = {tuple(int(v) for v in c)
           for c in _dec.palette_of(app.Image.fromarray(_edge, "RGBA").crop(_boxes[0]))}
    check("palette_of skips the one-pixel edge blends",
          (206, 22, 30) in _pe and (230, 140, 150) not in _pe, _pe)
    # a sheet scanned on WHITE paper: white is the background there (the
    # white-ink protection is for tinted carrier film only) — v2.15.2: a
    # whole white page used to stay opaque and become one giant decal
    _wp = _np4.full((120, 160, 3), (250, 247, 246), _np4.uint8)
    _wp[30:70, 20:80] = (206, 22, 30)                 # a red decal
    _wp[30:70, 100:140] = (255, 255, 255)             # white "ink" (invisible)
    _wp[90:92, 20:140] = (205, 205, 205)              # a sticker's faint cut edge
    _wpr = _dec.process_image(app.Image.fromarray(_wp, "RGB"), mode="cleanup",
                              remove_bg=True, denoise=0, tol=52,
                              target_dpi=300, native_dpi=300, exact=True)
    _wpa = _np4.asarray(_wpr["rgba"])[..., 3]
    check("white paper is detected as a neutral carrier",
          _dec.is_neutral_carrier(_dec.detect_carrier(app.Image.fromarray(_wp, "RGB")))
          and not _dec.is_neutral_carrier((214, 240, 242)))
    check("on white paper the page background goes transparent",
          _wpa[5, 5] < 40 and _wpa[100, 150] < 40, (int(_wpa[5, 5]), int(_wpa[100, 150])))
    check("…the red decal stays and is the only cut-out (faint grey edges dropped)",
          _wpa[50, 50] > 200 and _wpa[91, 80] < 40
          and len(_dec.segment_decals(_wpr["rgba"])) == 1,
          (int(_wpa[91, 80]), len(_dec.segment_decals(_wpr["rgba"]))))
    _wt = _sh.copy()
    _wt[40:120, 30:130] = (230, 235, 234, 255)      # film-tinted "white" ink
    _pw = {tuple(int(v) for v in c)
           for c in _dec.palette_of(app.Image.fromarray(_wt, "RGBA").crop(_boxes[0]))}
    check("film-tinted white ink snaps to pure white (printers leave it bare)",
          (255, 255, 255) in _pw and (230, 235, 234) not in _pw, _pw)
    _ps = _dec.svg_set_physical_size(
        '<svg xmlns="x" width="90" height="60"><path d="M0 0"/></svg>', 1.5, 1.0)
    check("svg_set_physical_size adds inches + a viewBox",
          'width="1.5000in"' in _ps and 'height="1.0000in"' in _ps
          and 'viewBox="0 0 90 60"' in _ps, _ps)
    _rv2 = _dec.process_image(_src, mode="vector", remove_bg=True, denoise=0,
                              tol=52, target_dpi=300, native_dpi=300,
                              size_scale=1.5)
    check("plain Vectorize SVGs now carry their printed size too",
          'width="0.6000in"' in (_rv2.get("svg") or ""), (_rv2.get("svg") or "")[:160])
except Exception as _e:
    import traceback
    traceback.print_exc()
    check("AI redraw pipeline runs headless", False, repr(_e))
_gp = {"model": "Juggernaut-XL-v9.safetensors", "prompt": "p", "negative": "n",
       "width": 1024, "height": 768, "seed": 1, "denoise": 0.35,
       "ref_image_name": "in.png"}
_g = app.build_decal_refine_graph(
    {**_gp, "loras": [("Decals.safetensors", 1.0)], "esrgan": True})
_kinds = [v["class_type"] for v in _g.values()]
check("refine graph: LoadImage -> ESRGAN -> scale -> VAEEncode -> KSampler("
      "denoise) -> decode -> save",
      all(k in _kinds for k in ("LoadImage", "ImageUpscaleWithModel",
                                "ImageScale", "VAEEncode", "KSampler",
                                "VAEDecode", "SaveImage"))
      and _g["6"]["inputs"]["denoise"] == 0.35
      and _g["13"]["inputs"]["width"] == 1024
      and _g["13"]["inputs"]["crop"] == "disabled", _kinds)
check("refine graph applies the ticked LoRA",
      "LoraLoader" in _kinds
      and _g["20"]["inputs"]["lora_name"] == "Decals.safetensors")
check("refine graph without ESRGAN skips the upscale nodes",
      "ImageUpscaleWithModel" not in [
          v["class_type"] for v in
          app.build_decal_refine_graph({**_gp, "esrgan": False}).values()])
# the AI-upscale helper used to call Generator-only helpers on the App and
# swallow the AttributeError; with no engine it must simply hand back the input
_ea = app.engine_alive
app.engine_alive = lambda *a, **k: False
try:
    check("AI-upscale helper returns the input when the engine is down",
          ui._decal_ai_upscale(_sheet, 2.0) is _sheet)
finally:
    app.engine_alive = _ea
try:
    import numpy as _np2
    _rng = _np2.random.default_rng(1)
    _bg = (200 + _rng.integers(-8, 9, size=(80, 120, 1))).astype(_np2.uint8) \
        .repeat(3, 2)
    # surgical: a streak-free scan must be left completely untouched
    _nd = _np2.asarray(_dec.destripe(app.Image.fromarray(_bg, "RGB")))
    check("destripe never touches a streak-free scan (no false positives)",
          bool((_nd == _bg).all()))
    # a real 1px vertical streak is removed, and ONLY that column changes
    _st = _bg.copy(); _st[:, 60] = 110
    _sd = _np2.asarray(_dec.destripe(app.Image.fromarray(_st, "RGB")))
    _cols = _np2.where(_np2.any(_sd != _st, axis=(0, 2)))[0]
    check("destripe removes a vertical scanner line (only that column changes)",
          list(_cols) == [60] and abs(int(_sd[40, 60, 0]) - 200) <= 15,
          (list(_cols), int(_sd[40, 60, 0])))
    # white balance neutralises a blue-tinted grey
    _t = app.Image.fromarray(_np2.full((10, 10, 3), (200, 224, 236), _np2.uint8),
                             "RGB")
    _wb = _np2.asarray(_dec.white_balance(_t)).astype(int)[5, 5]
    check("white balance neutralises the film tint",
          (max(_wb) - min(_wb)) < 14, tuple(int(x) for x in _wb))
except Exception as _e:
    check("destripe / white-balance run", False, repr(_e))

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

# ---- optimized delete (prune, not full re-render) + tag-safe generation ----
print("optimized delete + tag-safe new image")
with tempfile.TemporaryDirectory() as _td2:
    _ps = []
    for i in range(5):
        _p = Path(_td2) / f"g{i}.png"
        _Img.new("RGB", (64, 64), (10 + i * 40, 80, 120)).save(_p)
        _ps.append(_p)
    ui.session = [(_Img.open(p).convert("RGB"),
                   {"model": "m.safetensors", "seed": i}, p)
                  for i, p in enumerate(_ps)]
    ui.current = 4
    ui._rebuild_gallery(); root.update()
    check("gallery has one button per image", len(ui._thumb_btns) == 5)
    # deleting a MIDDLE image prunes only that thumbnail — no full re-render
    _adds = [0]
    _orig_add = ui._add_thumb
    ui._add_thumb = lambda *a, **k: _adds.__setitem__(0, _adds[0] + 1)
    try:
        ui._delete_paths([str(_ps[2])])          # delete the middle one
    finally:
        ui._add_thumb = _orig_add
    root.update()
    check("delete does NOT rebuild the whole strip (0 _add_thumb calls)",
          _adds[0] == 0, _adds[0])
    check("only the deleted thumbnail is removed",
          len(ui._thumb_btns) == 4 and len(ui.session) == 4)
    check("surviving thumbnails re-index themselves (g3 now at index 2)",
          ui._thumb_index(ui._thumb_btns[2]) == 2
          and str(ui.session[2][2]).endswith("g3.png"))
    ui._thumb_btns[2].invoke()
    check("clicking a survivor selects the right image", ui.current == 2)
    # a freshly generated image must NOT steal the selection while tagging
    ui.current = 1
    ui._toggle_tag(0); root.update()             # user is curating
    _new = Path(_td2) / "gnew.png"
    _Img.new("RGB", (64, 64), (200, 0, 0)).save(_new)
    ui._handle_msg(("finished_image", _Img.open(_new).convert("RGB"),
                    {"model": "m.safetensors", "seed": 99}, _new))
    root.update()
    check("a new image while tagging does NOT move the selection",
          ui.current == 1, ui.current)
    check("the new image is still added to the gallery",
          str(ui.session[-1][2]).endswith("gnew.png")
          and len(ui._thumb_btns) == len(ui.session))
    # nothing tagged: a new image selects itself (the normal flow)
    ui.tagged.clear(); ui._refresh_tag_ui()
    _new2 = Path(_td2) / "gnew2.png"
    _Img.new("RGB", (64, 64), (0, 200, 0)).save(_new2)
    ui._handle_msg(("finished_image", _Img.open(_new2).convert("RGB"),
                    {"model": "m.safetensors", "seed": 100}, _new2))
    root.update()
    check("not tagging: a new image selects itself",
          ui.current == len(ui.session) - 1)
    ui._clear_history(); root.update()

# ---- the simplified face/character section + the loading sweep ----------
print("face/character section + loading sweep")
check("the face-source chooser defaults to file, both source rows exist",
      ui.face_source_var.get() == "file"
      and hasattr(ui, "face_file_row") and hasattr(ui, "face_db_row"))
check("the file row shows and the database rows are hidden for 'file'",
      bool(ui.face_file_row.grid_info()) and not ui.face_db_row.grid_info())
ui.face_source_var.set("db")
ui._on_face_source()
root.update()
check("choosing 'database' swaps which rows show",
      bool(ui.face_db_row.grid_info()) and not ui.face_file_row.grid_info())
ui.face_source_var.set("file")
ui._on_face_source()
root.update()
check("swap + two guide checkboxes exist and are settable",
      hasattr(ui, "swap_rag_var") and hasattr(ui, "swap_cb")
      and hasattr(ui, "swap_use_rag_var") and hasattr(ui, "swap_use_lora_var")
      and not hasattr(ui, "swap_guide_cb"))
check("the swap checkbox sits above the clone body",
      int(ui.swap_cb.grid_info().get("row", 9)) < int(ui.clone_body.grid_info().get("row", 0)))
ui.swap_rag_var.set(False)
ui._on_clone_toggle()
root.update()
check("unticking the swap greys the clone body",
      ui.photo_excl_btn.instate(["disabled"]))
ui.swap_rag_var.set(True)
ui._on_clone_toggle()
root.update()
check("re-ticking re-enables it", ui.photo_excl_btn.instate(["!disabled"]))
check("the Edit tab has a Common-edits menu", hasattr(ui, "edit_menu_btn")
      and hasattr(ui, "_edit_menu"))
ui._pick_edit_action("remove the watermark")
root.update()
check("a menu action fills the edit box and shows the Edit tab",
      "remove the watermark" in ui._get(ui.edit_prompt_box)
      and ui.left_tabs.index("current") == 3)
ui._set(ui.edit_prompt_box, "")
ui.left_tabs.select(0)
ui.swap_rag_var.set(True); ui.swap_fast_var.set(False)
check("swap on + Best quality read back",
      ui.swap_rag_var.get() and not ui.swap_fast_var.get())
# two independent guide checkboxes: LoRA and RAG (either/both/neither)
ui.swap_use_lora_var.set(True); ui.swap_use_rag_var.set(False)
check("LoRA and RAG guide the base independently",
      ui.swap_use_lora_var.get() and not ui.swap_use_rag_var.get())
ui.swap_use_rag_var.set(True)
check("no quality radio / no canvas option; always Best + canvas",
      hasattr(ui, "swap_fast_var") and not ui.swap_fast_var.get()
      and ui.editor_canvas_var.get())
check("the Using strip exists (thumbnail samples)",
      hasattr(ui, "face_using") and hasattr(ui, "_face_thumb_image"))
# ---- Face Swap: selectable method, defaulting to the fast face swap ----
_labels = [str(getattr(w, "cget", lambda *_: "")("text"))
           for w in _walk(ui._page_gen) if w.winfo_class() == "TLabel"]
check("the face section is titled FACE SWAP (renamed from Clone Tool)",
      any(t.startswith("FACE SWAP") for t in _labels))
check("the cloning section is titled CLONING (renamed from Variations)",
      any(t.startswith("CLONING") for t in _labels))
# Face Swap and Cloning are mutually exclusive
try:
    ui.swap_rag_var.set(False); ui.var_enable_var.set(False)
    ui.var_enable_var.set(True); ui._on_variation_toggle()
    _fs_off = not ui.swap_rag_var.get()
    ui.swap_rag_var.set(True); ui._on_clone_toggle()
    _cl_off = not ui.var_enable_var.get()
    check("enabling one of Face Swap / Cloning disables the other",
          _fs_off and _cl_off)
    ui.swap_rag_var.set(True); ui.var_enable_var.set(False)
except Exception as _e:
    check("Face Swap / Cloning mutual exclusion", False, str(_e))

# a selected clone's image belongs to the Cloning section's OWN "Using:"
# preview — it must NOT leak into the Face Swap section's face_paths/strip
try:
    _cdir = Path(tempfile.mkdtemp())
    _cface = _cdir / "clone_face.png"; _cref = _cdir / "clone_ref.png"
    app.Image.new("RGB", (48, 48), (200, 40, 40)).save(_cface)
    app.Image.new("RGB", (48, 48), (40, 60, 200)).save(_cref)
    ui.swap_rag_var.set(False); ui.face_paths = []
    ui.variation_sel = {"id": -1, "name": "tester", "description": "tester",
                        "face_path": str(_cface), "ref_path": str(_cref),
                        "config": json.dumps({}), "seed": "",
                        "created": "20260101000000"}
    ui.var_enable_var.set(True)
    ui.clone_lock_face_var.set(False)          # default: pure recipe replay
    ui._apply_variation()
    root.update()
    check("the Cloning section has its own 'Using:' preview widget",
          hasattr(ui, "clone_preview_lab") and hasattr(ui, "clone_preview_name"))
    check("the Cloning section has an 'Also lock the exact face' toggle, off",
          hasattr(ui, "clone_lock_face_var") and not ui.clone_lock_face_var.get())
    _clone_texts = []
    for _w in _walk(ui.variation_body):
        try:
            _clone_texts.append(str(_w.cget("text")))
        except Exception:
            pass
    check("Cloning section has no 'Create more' button (Generate handles it)",
          not any("Create more" in t for t in _clone_texts), _clone_texts)
    check("selecting a clone does NOT put its face in the Face Swap section",
          ui.face_paths == [], ui.face_paths)
    check("the clone's face is held by the Cloning section, not Face Swap",
          ui._variation_face == str(_cface))
    check("the Cloning 'Using:' names the selected clone",
          "tester" in ui.clone_preview_name.get())
    # default (face-lock off) = pure recipe replay: NO swap runs
    check("recipe-replay clone runs no face swap by default",
          not ui._swap_active() and ui._swap_face_source() == [])
    check("recipe-replay clone does NOT steer the base (pure recipe)",
          not ui._swap_guides_base())
    # face-lock on = the swap draws its face from the clone AND guides the base
    ui.clone_lock_face_var.set(True); ui._on_clone_lock_toggle()
    check("face-lock on makes the swap active for the clone",
          ui._swap_active())
    check("the swap draws its face from the clone while face-lock is on",
          ui._swap_face_source() == [str(_cface)])
    check("face-lock guides the base with the clone's whole saved image",
          ui._swap_guides_base())
    ui.clone_lock_face_var.set(False)
    check("Face Swap stays OFF when a clone is selected (mutually exclusive)",
          not ui.swap_rag_var.get())
    # the Face Swap 'Using:' strip must not show the clone image
    _fs_names = [str(getattr(w, "cget", lambda *_: "")("text"))
                 for w in _walk(ui.face_using) if w.winfo_class() == "TLabel"]
    check("the Face Swap 'Using:' strip does not display the clone image",
          not any("clone_face" in t for t in _fs_names), _fs_names)
    # turning Cloning off clears the clone preview and its face
    ui.var_enable_var.set(False); ui._on_variation_toggle()
    check("turning Cloning off clears the clone face and preview",
          ui._variation_face is None
          and ui.clone_preview_name.get() == "no clone selected")
except Exception as _e:
    check("clone image lives in the Cloning section, not Face Swap",
          False, repr(_e))
finally:
    ui.swap_rag_var.set(False); ui.var_enable_var.set(False)
    ui.clone_lock_face_var.set(False)
    ui.variation_sel = None; ui._variation_face = None; ui.face_paths = []

# plain Face Swap must NOT feed the headshot into the base — the reported bug
# was a face-only image recreating the portrait instead of drawing the prompt
ui.swap_rag_var.set(True); ui.var_enable_var.set(False)
check("plain Face Swap draws the base from the prompt, not the face photo",
      not ui._swap_guides_base())
ui.swap_rag_var.set(False)

# the re-render swap methods (Qwen/Kontext) were dropped — insightface is the
# only face-swap method now, so there's no Method picker to show
check("the Qwen/Kontext swap methods were removed (insightface only)",
      len(app.CLONE_METHODS) == 1 and app.CLONE_METHODS[0][1] == "faceswap")
check("no face-swap Method dropdown is shown (nothing to pick)",
      not hasattr(ui, "clone_method_dd"))
check("the clone method is always the fast local face swap",
      ui._clone_method() == "faceswap")
check("the face-swap engine helpers are module-level and callable",
      callable(app.faceswap_ready) and callable(app.run_face_swap)
      and isinstance(app.faceswap_ready(), bool))
# the face-swap install must not crash on a wrong-class method reference
# (v2.1/v2.2 shipped App._install_face_swap calling Generator._download_to
# -> AttributeError -> re-offered every Generate = an install loop)
check("download_stream is a module-level helper, not a class method",
      callable(getattr(app, "download_stream", None)))
import types as _types3, tempfile as _tf3, queue as _q3
_fs_tmp = Path(_tf3.mkdtemp())
_save = (app.INSIGHTFACE_DIR, app.subprocess.run, app.download_stream,
         app._sha256, ui.ui_queue)
app.INSIGHTFACE_DIR = _fs_tmp
app.subprocess.run = lambda *a, **k: _types3.SimpleNamespace(
    returncode=0, stdout="DET_OK", stderr="")
app.download_stream = lambda url, dest, progress=None: Path(dest).write_bytes(b"x")
app._sha256 = lambda p: app.INSWAPPER_SHA256
ui.ui_queue = _iq = _q3.Queue()
ui._install_face_swap()
_errs = []
while not _iq.empty():
    _m = _iq.get()
    if _m[0] == "error":
        _errs.append(_m[1])
check("the face-swap install runs to 'ready' with no error (no _download_to crash)",
      not _errs and (_fs_tmp / ".ready").exists(), _errs)
(app.INSIGHTFACE_DIR, app.subprocess.run, app.download_stream,
 app._sha256, ui.ui_queue) = _save
# ---- the QUALITY section: extra detail (FreeU), 4x upscale (no hi-res) ----
check("a QUALITY section exists with FreeU and upscale toggles, no hi-res",
      hasattr(ui, "freeu_var") and hasattr(ui, "upscale_var")
      and not hasattr(ui, "hires_var")
      and any(str(getattr(w, "cget", lambda *_: "")("text")) == "QUALITY"
              for w in _walk(ui._page_gen) if w.winfo_class() == "TLabel"))
_pq = dict(model="Juggernaut-XL.safetensors", prompt="a hero", negative="",
           seed=1, width=1024, height=1024, loras=[], steps=None, cfg=None,
           freeu=True)
_qt = [v["class_type"] for v in app.build_graph(_pq).values()]
check("no hi-res latent upscale / second sampling pass in the graph",
      "LatentUpscaleBy" not in _qt and _qt.count("KSampler") == 1, _qt)
check("Extra detail adds a FreeU node on SDXL", "FreeU_V2" in _qt)
ui.freeu_var.set(True); ui.upscale_var.set(True)
_sq = dict(ui._collect_ui_state())
ui.freeu_var.set(False); ui.upscale_var.set(False)
ui._apply_ui_state(_sq)
root.update()
check("the Quality toggles are remembered",
      ui.freeu_var.get() and ui.upscale_var.get())
ui.freeu_var.set(False); ui.upscale_var.set(False)
# ---- Flux image-guided RAG goes through Redux, SDXL through IP-Adapter ----
_sm = Path(app.MODELS) / "style_models"; _cv = Path(app.MODELS) / "clip_vision"
_sm.mkdir(parents=True, exist_ok=True); _cv.mkdir(parents=True, exist_ok=True)
_redux_made = []
for _d, _f in ((_sm, app.REDUX_FILE), (_cv, app.REDUX_SIGLIP)):
    if not (_d / _f).exists():
        (_d / _f).write_bytes(b"x"); _redux_made.append(_d / _f)
check("redux_ready() sees the Redux models", app.redux_ready())
_ftypes = [v["class_type"] for v in app.build_graph(dict(
    model="flux1-dev-fp8.safetensors", prompt="x", negative="", seed=1,
    width=512, height=512, loras=[], steps=None, cfg=None,
    style_ref_names=["r.png"], style_weight=0.6)).values()]
check("Flux RAG uses Redux (StyleModelApply), never IP-Adapter",
      "StyleModelApply" in _ftypes and "StyleModelLoader" in _ftypes
      and not any("IPAdapter" in t for t in _ftypes), _ftypes)
_stypes = [v["class_type"] for v in app.build_graph(dict(
    model="Juggernaut-XL.safetensors", prompt="x", negative="", seed=1,
    width=512, height=512, loras=[], steps=None, cfg=None,
    style_ref_names=["r.png"], style_weight=0.6)).values()]
check("SDXL RAG still uses IP-Adapter, never Redux",
      any("IPAdapter" in t for t in _stypes)
      and "StyleModelApply" not in _stypes)
for _f in _redux_made:
    _f.unlink()
# ---- SD3.5 uses TripleCLIPLoader + the InstantX SD3 IP-Adapter for RAG ----
check("model_family detects SD3.5",
      app.model_family("sd3.5_large_fp8_scaled.safetensors") == "sd3")
_ipa = Path(app.ENGINE_DIR) / "models" / "ipadapter"
_ipa.mkdir(parents=True, exist_ok=True)
_cv.mkdir(parents=True, exist_ok=True)
_nodedir = Path(app.ENGINE_DIR) / "custom_nodes" / app.SD3_NODE_DIR
_sd3_made = []
for _pth in ((_ipa / app.SD3_IPA), (_cv / app.REDUX_SIGLIP)):
    if not _pth.exists():
        _pth.write_bytes(b"x"); _sd3_made.append(_pth)
_node_made = False
if not _nodedir.exists():
    _nodedir.mkdir(parents=True); _node_made = True
check("sd3_ipa_ready() sees the adapter, encoder and node", app.sd3_ipa_ready())
_s3 = [v["class_type"] for v in app.build_graph(dict(
    model="sd3.5_large_fp8_scaled.safetensors", prompt="x", negative="", seed=1,
    width=1024, height=1024, loras=[], steps=None, cfg=None,
    style_ref_names=["r.png"], style_weight=0.5)).values()]
check("SD3.5 RAG uses the InstantX SD3 adapter, not the SDXL IP-Adapter/Redux",
      "ApplyIPAdapterSD3" in _s3 and "IPAdapterSD3Loader" in _s3
      and not any(("IPAdapterUnifiedLoader" in t or "StyleModelApply" in t)
                  for t in _s3), _s3)
for _f in _sd3_made:
    _f.unlink()
if _node_made:
    _nodedir.rmdir()
# a face from a file, and the source round-trips through persistence
import tempfile as _tf
from PIL import Image as _Im2
_ftd = _tf.mkdtemp()
_fp = Path(_ftd) / "face.png"; _Im2.new("RGB", (32, 32), "pink").save(_fp)
ui.face_paths = [str(_fp)]
ui.face_source_var.set("file")
ui._set_face_label()
st = dict(ui._collect_ui_state())
check("the face source and file are remembered",
      st.get("face_source") == "file" and st.get("face_paths") == [str(_fp)])
check("the face label shows the file", "face.png" in ui.face_file_var.get())
ui.face_paths = []
ui.face_source_var.set("db")
ui._apply_ui_state(st)
root.update()
check("…and restored (source and file)",
      ui.face_source_var.get() == "file" and ui.face_paths == [str(_fp)])
ui._refresh_face_list()
root.update()
check("the Using strip shows a thumbnail sample of the face file",
      len(ui._face_thumb_imgs) >= 1 and len(ui.face_using.winfo_children()) >= 1,
      (len(ui._face_thumb_imgs), len(ui.face_using.winfo_children())))
ui.swap_fast_var.set(False)
ui.ui_queue.put(("progress_mode", "loading"))
ui._poll_queue()
root.update()
check("a loading model makes the bar sweep",
      str(ui.progress.cget("mode")) == "indeterminate" and ui.pct_var.get() == "loading…")
ui.ui_queue.put(("progress", 2, 4))
ui._poll_queue()
root.update()
check("the first step brings a real bar back at its value",
      str(ui.progress.cget("mode")) == "determinate" and int(ui.progress["value"]) == 2
      and ui.pct_var.get() == "50%", (ui.progress.cget("mode"), ui.pct_var.get()))
ui.ui_queue.put(("progress_mode", "loading"))
ui.ui_queue.put(("done", None))
ui._poll_queue()
root.update()
check("done stops the sweep and clears the bar",
      str(ui.progress.cget("mode")) == "determinate" and int(ui.progress["value"]) == 0)


def stray_sweeps():
    """Tk timer chains still stepping a progress bar."""
    return [aid for aid in root.tk.call("after", "info")
            if "Autoincrement" in str(root.tk.call("after", "info", aid))]


# the Tk trap: every start() stacked a timer chain stop() could not reach —
# after four pictures the Generate bar raced for ever
for _ in range(4):
    ui.ui_queue.put(("progress_mode", "loading"))
    ui._poll_queue()
    root.update()
ui.ui_queue.put(("done", None))
ui._poll_queue()
root.update()
check("four loads then done leave no sweeping timer behind",
      not stray_sweeps() and str(ui.progress.cget("mode")) == "determinate",
      stray_sweeps())
for _ in range(3):
    ui._set_pending("engine", "engine starting")
root.update()
ui._set_pending("engine", None)
root.update()
check("the readiness strip leaves no timer behind either", not stray_sweeps(),
      stray_sweeps())
for ph in ("reading", "indexing", "embeddings"):
    ui._rag_progress(ui._ragmap_load_gen, ph, 0, 0)
    root.update()
ui._rag_progress_done()
root.update()
check("the RAG loading bar leaves no timer behind either", not stray_sweeps(),
      stray_sweeps())

# ---- a database person's photos: cycle + drop (not delete) --------------
print("photo cycle / drop")
import io as _io, tempfile as _tf2
from PIL import Image as _Im3
def _blob(col):
    b = _io.BytesIO(); _Im3.new("RGB", (32, 32), col).save(b, "JPEG"); return b.getvalue()
_blobs = [_blob("red"), _blob("green"), _blob("blue")]
_dbf = Path(_tf2.mkdtemp()) / "people.sqlite"; _dbf.write_bytes(b"x")
ui.actordb_path = str(_dbf)
ui._actor_photo_blobs = lambda imdb: _blobs
ui.face_source_var.set("db")
ui._actor_excluded = {}
ui._set_actor({"imdb_id": "tt1", "first_name": "Test", "last_name": "Person"})
root.update()
check("the photo arrows show for a multi-photo person",
      bool(ui.photo_nav.grid_info()))
ui.actor_photo_i = 0
ui._actor_step_photo(1)
check("the right arrow advances the shown photo", ui.actor_photo_i == 1)
ui._actor_step_photo(-1)
check("the left arrow goes back (wrapping)", ui.actor_photo_i == 0)
ui._actor_toggle_photo()   # drop photo 0
check("dropping a photo records it as excluded, not deleted",
      0 in ui._excluded_set())
paths = ui._actor_ref_paths_all()
check("the dropped photo is not sent to the model",
      paths and all("_0.jpg" not in Path(p).name for p in paths), paths)
st = dict(ui._collect_ui_state())
check("the dropped photo is remembered", st.get("actor_excluded") == {"tt1": [0]})
# cannot drop the last remaining photo
ui._actor_excluded["tt1"] = {0, 1}
ui.actor_photo_i = 2
ui._actor_toggle_photo()
check("the last remaining photo cannot be dropped",
      2 not in ui._excluded_set() and "must stay" in ui.status_var.get())
ui.actor_sel = None
ui._actor_excluded = {}
ui.face_source_var.set("file")
ui._refresh_editor_state()

# ---- a swap whose face file is gone stops before generating -------------
print("swap guard")
real_alive = app.engine_alive
app.engine_alive = lambda *a, **k: True
ui._set(ui.prompt_box, "a hero on a rooftop")
ui.ref_paths = []                       # not editing — this is a swap
ui.swap_rag_var.set(True)
ui.face_source_var.set("file")
ui.face_paths = ["C:/nope/gone_face.png"]
ui._set_face_label()
ui._generate()
root.update()
check("a missing face stops the run with a clear message",
      "no longer exists" in ui.status_var.get()
      and "Nothing was generated" in ui.status_var.get(), ui.status_var.get())
check("…and the missing face file is let go",
      ui.face_paths == [])
check("…and the app is not left busy", not ui.busy)
ui.swap_rag_var.set(False)
ui._set(ui.prompt_box, "")
app.engine_alive = real_alive

# ---- a loaded Edit image no longer forces LoRA/RAG off for GENERATE ------
print("edit image doesn't disable generate")
ui.lora_list.delete(0, "end")
ui.lora_list.insert("end", "some_style.safetensors")
ui.lora_list.selection_set(0)
ui.model_var.set("Juggernaut-XL")          # SDXL family
ui.ragmap = {"weight": 0.8}
ui.swap_rag_var.set(False)
ui.actor_sel = None
ui.ref_paths = ["C:/some/loaded_edit.png"]  # an image sitting on the Edit tab
ui._refresh_mode_badges()
root.update()
check("a loaded Edit image does NOT turn the LoRA badge red",
      str(ui.lora_badge.cget("style")) == "BadgeOn.TLabel",
      ui.lora_badge.cget("style"))
check("…nor the RAG badge — GENERATE ignores the Edit image",
      str(ui.rag_badge.cget("style")) == "BadgeOn.TLabel",
      ui.rag_badge.cget("style"))
# and "Apply edit" with no image loaded asks for one instead of running
ui.ref_paths = []
app.engine_alive = lambda *a, **k: True
_info_calls = []
_real_info = app.messagebox.showinfo
app.messagebox.showinfo = lambda *a, **k: _info_calls.append(a)
ui._generate(edit=True)
root.update()
app.messagebox.showinfo = _real_info
check("Apply edit with nothing loaded asks for an image, does not run",
      not ui.busy and any("Edit image" in str(a) for a in _info_calls),
      _info_calls)
app.engine_alive = real_alive
ui.ragmap = None
ui.lora_list.selection_clear(0, "end")
ui.lora_list.delete(0, "end")
ui._set(ui.prompt_box, "")
ui._set(ui.edit_prompt_box, "")
ui._refresh_mode_badges()

# ---- the prompt enhancer always has a model to pick (built-in offline) ---
print("prompt enhancer (built-in offline)")
check("a built-in offline enhancer exists and is the default option",
      app.BUILTIN_ENHANCER in ui.ollama_dd["values"]
      and ui.ollama_var.get() != "", (ui.ollama_dd["values"], ui.ollama_var.get()))
_enh = app.builtin_enhance("a knight on a cliff", "oil painting", "sdxl")
check("the built-in enhancer keeps the user's words and adds detail",
      "a knight on a cliff" in _enh and "oil painting" in _enh
      and len(_enh) > len("a knight on a cliff, oil painting"), _enh)
check("the built-in enhancer returns nothing for an empty prompt",
      app.builtin_enhance("", "", "sdxl") == "")
# even when no Ollama is found, the built-in option stays selectable
ui._on_ollama_found([])
root.update()
check("with no Ollama the built-in option is still there and chosen",
      app.BUILTIN_ENHANCER in ui.ollama_dd["values"]
      and ui.ollama_var.get() == app.BUILTIN_ENHANCER)
# a full round-trip through _run_enhance using the built-in path
ui._set(ui.prompt_box, "a red car")
ui.ollama_var.set(app.BUILTIN_ENHANCER)
ui._run_enhance("a red car", app.BUILTIN_ENHANCER, "", "sdxl")
ui._poll_queue()
root.update()
check("Enhance rewrites the prompt in place with the built-in enhancer",
      "a red car" in ui._get(ui.prompt_box)
      and len(ui._get(ui.prompt_box)) > len("a red car"),
      ui._get(ui.prompt_box))
ui._undo_enhance()
check("Undo puts the original wording back",
      ui._get(ui.prompt_box).strip() == "a red car")
ui._set(ui.prompt_box, "")

# ---- settings: theme + menu size ----------------------------------------
print("settings (theme + size)")
check("apply_theme switches the palette",
      app.apply_theme("light") == "light" and app.FG == "#1b1b24"
      and app.BG == "#f4f4f7")
app.apply_theme("dark")
check("…and back to dark", app.BG == "#17171c" and app.FG == "#e8e8f0")
check("a Settings button exists", hasattr(ui, "settings_btn")
      and "Settings" in ui.settings_btn.cget("text"))
ui.settings.setdefault("prefs", {})["theme"] = "light"
ui.settings["prefs"]["ui_scale"] = 1.2
ui._apply_prefs_live()
root.update()
check("live apply sets the light palette", app.BG == "#f4f4f7")
from tkinter import ttk as _ttk2
_st = _ttk2.Style()
check("daylight tooltips are a pale bg with dark text (readable)",
      str(_st.lookup("Tip.TLabel", "background")).lower() in ("#fffbe6", "#fffbe6")
      and str(_st.lookup("Tip.TLabel", "foreground")).lower() == "#1b1b24",
      (_st.lookup("Tip.TLabel", "background"), _st.lookup("Tip.TLabel", "foreground")))
check("live apply recolours the prompt box",
      str(ui.prompt_box.cget("bg")).lower() == "#d8d8e2")
check("the scaling reflects the size choice",
      abs(float(root.tk.call("tk", "scaling"))
          - ui._base_scaling * 1.2) < 0.01)
ui.settings["prefs"]["theme"] = "dark"
ui.settings["prefs"]["ui_scale"] = 1.0
ui._apply_prefs_live()
root.update()
check("switching back to dark recolours again", app.BG == "#17171c"
      and str(ui.prompt_box.cget("bg")).lower() == "#2a2a38")

# font family: applied live to the ttk styles and the tk widgets
check("the font list and pref exist",
      hasattr(app, "UI_FONTS") and "Segoe UI" in app.UI_FONTS)
ui.settings["prefs"]["font"] = "Verdana"
ui._apply_prefs_live()
root.update()
check("the UI font family changes", app.UI_FONT == "Verdana")
import tkinter.font as _tf
check("the prompt box uses the new family",
      _tf.Font(font=ui.prompt_box.cget("font")).cget("family") == "Verdana")
ui.settings["prefs"]["font"] = "Segoe UI"
ui._apply_prefs_live()
root.update()
check("…and back to Segoe UI",
      _tf.Font(font=ui.prompt_box.cget("font")).cget("family") == "Segoe UI")

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
    check("NOTHING is downloaded before the user says so",
          staged_tags == [] and not installs and not _started)
    check("Download and install / Not now / Skip this version are offered",
          win.update_btn.cget("text") == "Download and install"
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
    check("Not now leaves the app running on the old version with nothing "
          "downloaded", ui.root.winfo_exists() and not installs
          and staged_tags == [] and not _started)

    # the yes: download + install, then ask about the restart
    ui.ui_queue.put(("app_update", upd))
    ui._poll_queue()
    root.update()
    win = ui._upd_win
    win._start()
    pump(root, lambda: win._newexe is not None)
    check("after the yes: downloaded and installed, not restarted",
          staged_tags == ["v1.99.0"] and installs == [1] and not _started)
    check("Restart now / Later are offered",
          win.update_btn.cget("text") == "Restart now"
          and win.cont_btn.cget("text") == "Later")
    win._continue()
    root.update()
    check("Later leaves the app running; the new version starts next time",
          ui.root.winfo_exists() and not _started
          and "next time" in ui.status_var.get(), ui.status_var.get())
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

# ---- photos of a sheet (v2.17) --------------------------------------------
print("photo mode")
check("photo handling is automatic: only the sheet width is an input, plus the report button",
      hasattr(ui, "decal_width_mm_var") and hasattr(ui, "decal_report_btn")
      and not hasattr(ui, "decal_photo_var") and not hasattr(ui, "decal_rotate_var")
      and callable(getattr(ui, "_assess_decal_sources", None)))
try:
    import numpy as _np6
    # a "photo": dark table, a white sheet lying slightly askew, a red decal on it
    _ph = _np6.full((400, 500, 3), (70, 40, 30), _np6.uint8)
    _sheet_img = app.Image.fromarray(_ph, "RGB")
    _d = app.ImageDraw.Draw(_sheet_img)
    _quad = [(90, 60), (420, 75), (410, 345), (80, 330)]       # TL TR BR BL
    _d.polygon(_quad, fill=(245, 243, 240))
    _d.rectangle([200, 150, 300, 230], fill=(206, 22, 30))
    check("a scan is not mistaken for a photo",
          not _dec.looks_like_photo(_src) and _dec.looks_like_photo(_sheet_img))
    _c = _dec.find_sheet(_sheet_img)
    check("the sheet's corners are found in the photo",
          _c is not None and all(abs(_c[i][0] - _quad[i][0]) <= 10
                                 and abs(_c[i][1] - _quad[i][1]) <= 10
                                 for i in range(4)), _c)
    _st = _dec.straighten(_sheet_img, _c)
    _sa = _np6.asarray(_st)
    check("straightening yields the sheet alone, white at the corners, decal inside",
          _sa[3, 3].min() > 220 and _sa[-4, -4].min() > 220
          and _sa.shape[1] > 300 and _sa.shape[0] > 250
          and (_sa[..., 0] > 180).sum() > (_sa[..., 1] < 60).sum() * 0.5, _sa.shape)
    _pp, _note = _dec.prepare_photo(_sheet_img, rotate=90, auto_crop=True)
    check("prepare_photo crops, rotates and says so",
          "cropped" in _note and "rotated 90" in _note
          and _pp.width == _st.height and _pp.height == _st.width, _note)
    check("a scan passes through prepare_photo untouched",
          _dec.prepare_photo(_src, rotate=0, auto_crop=True)[1] == ""
          and _dec.prepare_photo(_src, rotate=0, auto_crop=True)[0].size == _src.size)
    check("DPI from the sheet width (1500 px wide, 127 mm) = 300",
          abs(_dec.dpi_from_width(1500, 127) - 300.0) < 0.5
          and _dec.dpi_from_width(1500, 0) is None)
    ui.decal_width_mm_var.set("127")
    _prep = ui._decal_source_prep()
    check("the UI hands the workers the sheet width", abs(_prep["width_mm"] - 127) < 1e-6)
    check("there is no Scan DPI control any more (the resolution is read from the file)",
          not hasattr(ui, "decal_native_var"))
    # the sheet width is measured on the UPRIGHT picture: 1500×500 rotated
    # 270° is 500 px wide, and 500 px over 127 mm is 100 dpi
    _im2, _dpi2, _n2, _ph2 = ui._prepare_source(app.Image.new("RGB", (1500, 500), (250, 247, 246)), _prep, None, rotate=270)
    check("_prepare_source applies the sheet-width DPI to the upright page",
          _dpi2 == 100 and "100 dpi" in _n2 and _im2.size == (500, 1500) and not _ph2, (_dpi2, _n2, _im2.size))
    ui.decal_width_mm_var.set("")
    _prep = ui._decal_source_prep()
    _im3, _dpi3, _n3, _ph3 = ui._prepare_source(_src, _prep, 200)
    check("a scan takes the resolution its file carries", _dpi3 == 200 and _n3 == "" and not _ph3, (_dpi3, _n3))
    _im4, _dpi4, _n4, _ph4 = ui._prepare_source(_src, _prep, None)
    check("…and 300 is assumed, and said, when the file has none",
          _dpi4 == 300 and "assumed" in _n4, (_dpi4, _n4))
    _im5, _dpi5, _n5, _ph5 = ui._prepare_source(_sheet_img, _prep, 72)
    check("a photo is recognised, cropped, flattened; its 72 dpi tag is ignored",
          _ph5 and _dpi5 == 300 and "cropped" in _n5 and "flattened" in _n5
          and "assumed" in _n5 and "sheet width" in _n5, (_dpi5, _n5))
    _a5 = _np6.asarray(_im5)
    check("the photographed sheet comes out white, its decal still red",
          _a5[5, 5].min() >= 236 and _a5[-6, -6].min() >= 236
          and ((_a5[..., 0] > 150) & (_a5[..., 1] < 80)).sum() > 5000, (_a5[5, 5], _a5[-6, -6]))
    # the resolution comes from the file: a PDF page's drawn size, an
    # image's DPI tag (72/96 = unknown)
    import pymupdf as _mu, io as _io6
    _pdf = app.Path(str(_fs_tmp)) / "dpi_probe.pdf"
    _doc = _mu.open()
    _pg = _doc.new_page(width=144, height=72)           # 2 × 1 inch
    _buf = _io6.BytesIO()
    app.Image.new("RGB", (400, 200), (250, 247, 246)).save(_buf, format="PNG")
    _pg.insert_image(_pg.rect, stream=_buf.getvalue())
    _doc.save(str(_pdf)); _doc.close()
    _got = list(_dec.iter_sources(_pdf))
    check("a PDF's resolution is read from the page (400 px over 2 in = 200 dpi)",
          len(_got) == 1 and _got[0][2] == 200 and _got[0][1].size == (400, 200), _got[0][2] if _got else None)
    _png6 = app.Path(str(_fs_tmp)) / "dpi_probe.png"
    app.Image.new("RGB", (60, 40), (250, 247, 246)).save(_png6, dpi=(600, 600))
    _png7 = app.Path(str(_fs_tmp)) / "dpi_none.png"
    app.Image.new("RGB", (60, 40), (250, 247, 246)).save(_png7)
    check("an image's DPI tag is read; none means unknown",
          list(_dec.iter_sources(_png6))[0][2] == 600
          and list(_dec.iter_sources(_png7))[0][2] is None)
    # the orientation is the vision model's answer, asked once per page
    _asked = []
    _orig_ask = app.vector_redraw.ask_orientation
    _orig_key = app.vector_redraw.get_api_key
    app.vector_redraw.ask_orientation = lambda img, **kw: (_asked.append(1), 180)[1]
    app.vector_redraw.get_api_key = lambda: "sk-ant-test-key-not-real"
    try:
        ui._decal_orient = {}
        _o1 = ui._decal_orientation("x.pdf", "p1", _src)
        _o2 = ui._decal_orientation("x.pdf", "p1", _src)
        app.vector_redraw.get_api_key = lambda: None
        _o3 = ui._decal_orientation("y.pdf", "p1", _src)
    finally:
        app.vector_redraw.ask_orientation = _orig_ask
        app.vector_redraw.get_api_key = _orig_key
    check("orientation: asked once per page and remembered; 0 without a key",
          _o1 == 180 and _o2 == 180 and len(_asked) == 1 and _o3 == 0, (_o1, _o2, _asked, _o3))
    # the automatic quality report
    _r = _dec.assess_source(_src, native_dpi=300, raw=_src, orientation=0)
    check("assess_source reports kind, decals, scores and a recommendation",
          _r["kind"] == "scan" and _r["decals"] >= 1
          and set(_r["scores"]) == {"trace", "vision", "reimagine"}
          and all(0.0 <= v <= 1.0 for v in _r["scores"].values())
          and any(ln.startswith("Orientation: upright") for ln in _r["report"])
          and any(ln.startswith("Recommendation:") for ln in _r["report"]), _r["report"])
    _rp = _dec.assess_source(_im5, native_dpi=240, raw=_sheet_img, photo=True, orientation=None, dpi_known=False)
    check("…and tells a photo from a scan, with the backing and the width advice",
          _rp["kind"] == "photo" and any("keyed as white" in ln for ln in _rp["report"])
          and any("sheet's real width" in ln for ln in _rp["report"])
          and any(ln.startswith("Orientation: not checked") for ln in _rp["report"]), _rp["report"])
    ui.ui_queue.put(("decal_report", [("demo", _r, ["— demo —"] + _r["report"])]))
    ui._poll_queue()
    root.update()
    check("the report lands in the status headline and the report window text",
          "expected usable" in ui.decal_status_var.get()
          and "Recommendation:" in getattr(ui, "_decal_report_text", ""),
          ui.decal_status_var.get())
except Exception as _e:
    import traceback
    traceback.print_exc()
    check("photo mode runs headless", False, repr(_e))

# ---- taskbar identity (v2.16.1) -------------------------------------------
# ---- no freezes: fast morphology, per-decal Vectorize (v2.22.7) ------------
print("no window freezes")
try:
    import time as _t13, threading as _th13
    from PIL import ImageFilter as _IF13
    _rng = _np6.random.default_rng(3)
    _m13 = _rng.random((70, 90)) > 0.7
    _pd = _np6.asarray(app.Image.fromarray((_m13 * 255).astype(_np6.uint8)).filter(_IF13.MaxFilter(7))) > 0
    _pe = _np6.asarray(app.Image.fromarray((_m13 * 255).astype(_np6.uint8)).filter(_IF13.MinFilter(7))) > 0
    check("the running-sum dilation and erosion match Pillow's filters",
          bool((_dec.dilate_mask(_m13, 3) == _pd).all())
          and bool((_dec.erode_mask(_m13, 3)[3:-3, 3:-3] == _pe[3:-3, 3:-3]).all()))
    _gaps = []
    _stop = _th13.Event()

    def _tick():
        _last = _t13.perf_counter()
        while not _stop.is_set():
            _t13.sleep(0.01)
            _now = _t13.perf_counter()
            _gaps.append(_now - _last)
            _last = _now
    _th13.Thread(target=_tick, daemon=True).start()
    _bigd = app.Image.new("RGBA", (2546, 1031), (0, 0, 0, 0))
    _bigd.paste((20, 20, 30, 255), (100, 100, 2400, 900))
    _svgb = ('<svg xmlns="http://www.w3.org/2000/svg" width="2546" height="1031" '
             'viewBox="0 0 2546 1031"><rect x="100" y="100" width="2300" height="800" '
             'fill="#14141e"/></svg>')
    _gaps.clear()
    app.vector_redraw.check_against_scan(_svgb, _bigd)
    _g1 = max(_gaps or [0])
    # a sheet with several decals through Vectorize
    _sheet13 = app.Image.new("RGB", (1400, 900), (222, 253, 255))
    _dd = app.ImageDraw.Draw(_sheet13)
    for _q in range(6):
        _dd.rectangle([40 + 220 * _q, 60, 200 + 220 * _q, 300], fill=(200, 20, 30))
        _dd.ellipse([40 + 220 * _q, 400, 200 + 220 * _q, 560], fill=(20, 20, 30))
        _dd.ellipse([80 + 220 * _q, 440, 160 + 220 * _q, 520], fill=(239, 255, 254))
    _gaps.clear()
    _rv = _dec.process_image(_sheet13, mode="vector", remove_bg=True, denoise=0, tol=52,
                             target_dpi=600, native_dpi=300, exact=True, tidy_matte=True,
                             fill_holes=True, balance=False)
    _g2 = max(_gaps or [0])
    _stop.set()
    _av = _np6.asarray(_rv["rgba"])
    check("the scan check on a big decal never starves the window (was 178 s)", _g1 < 1.0, round(_g1, 2))
    check("Vectorize traces decal by decal: no long freeze, transparent background, "
          "red stays red, white ink stays white",
          _g2 < 2.0 and _av[10, 10, 3] == 0 and _av[360, 240, 3] == 255
          and _av[360, 240, 0] > 170 and _av[360, 240, 1] < 80
          and _av[960, 240, 3] == 255 and _av[960, 240, :3].min() > 230,
          (round(_g2, 2), _av[10, 10].tolist(), _av[360, 240].tolist(), _av[960, 240].tolist()))
    check("…and its SVG has no full-page background layer",
          "#730073" not in _rv["svg"] and 'width="1400.0000in"' not in _rv["svg"])
except Exception as _e:
    import traceback
    traceback.print_exc()
    check("no-freeze checks run headless", False, repr(_e))

# ---- redraw only the chosen pages (v2.22.6) ---------------------------------
print("pages to redraw")
try:
    import time as _t12, pymupdf as _mu12, io as _io12
    check("page lists are read: all, one, a list, a range",
          _dec.parse_pages("all") is None and _dec.parse_pages("") is None
          and _dec.parse_pages("2") == {2} and _dec.parse_pages("1, 3") == {1, 3}
          and _dec.parse_pages("2-4") == {2, 3, 4} and _dec.parse_pages("1,3-4") == {1, 3, 4})
    _bad = 0
    for _t in ("0", "x", "3-1"):
        try:
            _dec.parse_pages(_t)
        except ValueError:
            _bad += 1
    check("…and nonsense is refused", _bad == 3)
    check("the Decals tab has the Pages box, 'all' by default",
          hasattr(ui, "decal_pages_var") and ui.decal_pages_var.get() == "all")
    # a 3-page PDF, a decal on every page
    _pdf3 = app.Path(str(_fs_tmp)) / "three_pages.pdf"
    _d3 = _mu12.open()
    for _k in range(3):
        _pg = _d3.new_page(width=144, height=144)
        _im = app.Image.new("RGB", (300, 300), (222, 253, 255))
        app.ImageDraw.Draw(_im).rectangle([60 + 20 * _k, 60, 200, 200], fill=(200, 20, 30))
        _b = _io12.BytesIO(); _im.save(_b, format="PNG")
        _pg.insert_image(_pg.rect, stream=_b.getvalue())
    _d3.save(str(_pdf3)); _d3.close()
    _old_out12 = app.DECALS_OUT
    app.DECALS_OUT = app.Path(str(_fs_tmp)) / "pages_out"
    _old_eng = app.engine_alive
    app.engine_alive = lambda *a, **k: False       # plain enlargement, no engine
    ui.decal_sources = [str(_pdf3)]
    ui.decal_method_var.set("trace")
    ui.decal_text_var.set(False)
    ui.decal_judge_var.set(False)   # never a paid call from a test
    ui.decal_pages_var.set("2")
    _n0 = len(ui.session)
    ui._redraw_decals()
    for _ in range(600):
        ui._poll_queue(); root.update()
        if not ui._decals_busy:
            break
        _t12.sleep(0.05)
    _new = [p for _i, p, _s in ui.session[_n0:] if isinstance(p, dict)]
    check("Redraw with Pages = 2 makes page 2 only",
          len(_new) == 1 and str(_new[0].get("page", "")).endswith("_p2"),
          [p.get("page") for p in _new])
    # a single decal fixed from the Compare window (v2.25.0)
    _pf = _new[0] if _new else {}
    _meta = json.loads(app.Path(_pf["fix_index"]).read_text(encoding="utf-8")) \
        if _pf.get("fix_index") else None
    check("a redraw saves the fix index (boxes) and its cleaned page",
          _meta is not None and _meta["items"]
          and (app.Path(_pf["fix_index"]).parent / "_scan.png").exists(), _pf)
    if _meta:
        _b = _meta["items"][0]["box"]
        _xi = (_b[0] + _b[2]) / 2.0 / _meta["src_dpi"]
        _yi = (_b[1] + _b[3]) / 2.0 / _meta["src_dpi"]
        _svg_before = app.Path(_pf["svg"]).read_text(encoding="utf-8")
        ui._decal_fix(_pf, "trace", _xi, _yi, sync=True)
        ui._poll_queue(); root.update()
        check("right-click 'clean trace' redraws that decal and rebuilds the sheet",
              ui.decal_status_var.get().startswith("Decal 1: a clean trace")
              and app.Path(_pf["svg"]).exists()
              and "<svg" in app.Path(_pf["svg"]).read_text(encoding="utf-8"),
              ui.decal_status_var.get())
        _m = ui._decal_fix(_pf, "fill", 0.001, 0.001, sync=True)
        check("a click away from every decal is answered, nothing changes",
              _m and "No decal under the cursor" in _m, _m)
    check("the answer cache is on by default; the quality-check switch exists",
          ui.decal_cache_var.get() is True and hasattr(ui, "decal_judge_var"))
    ui.decal_pages_var.set("7")
    ui._redraw_decals()
    for _ in range(200):
        ui._poll_queue(); root.update()
        if not ui._decals_busy:
            break
        _t12.sleep(0.05)
    check("a page that is not in the file is reported, not silently skipped",
          "none of the pages asked for (7)" in ui.decal_status_var.get(), ui.decal_status_var.get())
    # a PNG with Pages = 2 left over from a PDF: the picture is still used
    _png12 = app.Path(str(_fs_tmp)) / "one_picture.png"
    _imp = app.Image.new("RGB", (300, 300), (222, 253, 255))
    app.ImageDraw.Draw(_imp).rectangle([60, 60, 200, 200], fill=(200, 20, 30))
    _imp.save(str(_png12))
    ui.decal_sources = [str(_png12)]
    ui.decal_pages_var.set("2")
    _n1 = len(ui.session)
    ui._redraw_decals()
    for _ in range(600):
        ui._poll_queue(); root.update()
        if not ui._decals_busy:
            break
        _t12.sleep(0.05)
    check("a PNG is redrawn whatever the Pages box says (pages pick PDF pages)",
          len(ui.session) > _n1
          and "none of the pages" not in ui.decal_status_var.get(),
          ui.decal_status_var.get())
    ui.decal_sources = [str(_pdf3)]
    ui.decal_pages_var.set("x")
    ui._redraw_decals()
    check("a bad page list is explained before anything runs",
          "Pages to redraw" in ui.decal_status_var.get() and not ui._decals_busy)
    # Recraft vectorize: its method button, the key guard, a run through it
    check("the Recraft vectorize method has its own button and fal key button",
          hasattr(ui, "decal_fal_btn") and hasattr(ui, "decal_fal_lab"))
    _orig_gk = app.recraft_vectorize.get_key
    _orig_mk = app.recraft_vectorize.make_vector_fn
    app.recraft_vectorize.get_key = lambda: None
    ui.decal_method_var.set("recraft")
    ui.decal_pages_var.set("1")
    ui._redraw_decals()
    check("Recraft without a fal key says so and does not start",
          "fal.ai key" in ui.decal_status_var.get() and not ui._decals_busy, ui.decal_status_var.get())
    _calls_rc = []

    def _fake_mk(key, dpi, stats=None, cancelled=None, log=None, session=None):
        def _fn(crop, pal, w_in, h_in):
            _calls_rc.append((key, w_in))
            stats["calls"] = stats.get("calls", 0) + 1
            stats["cost"] = stats.get("cost", 0.0) + 0.01
            stats["fallback"] = stats.get("fallback", 0) + 1
            return None                         # falls back to the trace
        return _fn
    app.recraft_vectorize.get_key = lambda: "fal-test-key-not-real"
    app.recraft_vectorize.make_vector_fn = _fake_mk
    _orig_geo12 = app.decals.geometric_svg
    app.decals.geometric_svg = lambda *a, **k: None   # rectangles: else no call
    try:
        _n1 = len(ui.session)
        ui._redraw_decals()
        for _ in range(600):
            ui._poll_queue(); root.update()
            if not ui._decals_busy:
                break
            _t12.sleep(0.05)
    finally:
        app.recraft_vectorize.get_key = _orig_gk
        app.recraft_vectorize.make_vector_fn = _orig_mk
        app.decals.geometric_svg = _orig_geo12
    check("a Recraft run calls the service per decal with the key and finishes "
          "(cost in red, Recraft named in the note)",
          _calls_rc and _calls_rc[0][0] == "fal-test-key-not-real"
          and len(ui.session) > _n1 and "Recraft" in ui.decal_status_var.get(),
          (len(_calls_rc), ui.decal_status_var.get()))
    ui.decal_method_var.set("trace")
    ui.decal_pages_var.set("all")
    ui.decal_text_var.set(True)
    ui.decal_sources = []
    ui._compare_win = None
    app.engine_alive = _old_eng
    app.DECALS_OUT = _old_out12
except Exception as _e:
    import traceback
    traceback.print_exc()
    check("pages to redraw runs headless", False, repr(_e))

# ---- freeze recorder + upscale graph (v2.22.4) ------------------------------
print("freeze recorder")
try:
    import time as _t10
    _sp = app.Path(str(_fs_tmp)) / "stall_test.log"
    _sw = app.StallWatch(root, _sp, timeout=0.6, beat_ms=100).start()
    root.update()
    _t10.sleep(1.2)                 # the window thread is blocked: no beats
    for _ in range(5):
        root.update(); _t10.sleep(0.12)
    _sw.stop()
    _txt = _sp.read_text(encoding="utf-8", errors="replace")
    check("a frozen window is recorded: every thread's stack, then when it answered again",
          "Timeout" in _txt and "Thread 0x" in _txt and "answered again" in _txt
          and _sw.stalls and _sw.stalls[0] >= 0.6, _txt[-400:])
    check("the app arms the recorder at start", getattr(ui, "_stall", None) is not None)
    if getattr(ui, "_stall", None) is not None:
        ui._stall.stop()            # the test blocks on purpose below; keep it quiet
    _src = open(app.__file__, encoding="utf-8").read()
    _i = _src.index("def _decal_ai_upscale")
    check("the decal AI upscale graph's save node has its filename prefix",
          '"filename_prefix": "cbac_decal_up"' in _src[_i:_i + 3000])
except Exception as _e:
    import traceback
    traceback.print_exc()
    check("freeze recorder runs headless", False, repr(_e))

# ---- print export + local vision models (v2.20) -----------------------------
print("print export, local models")
try:
    _old_out = app.DECALS_OUT
    _pe_root = app.Path(str(_fs_tmp)) / "print_root"
    app.DECALS_OUT = _pe_root
    ui._no_open_folders = True
    check("the Decals tab has Export for print and Pull local model",
          hasattr(ui, "decal_export_btn") and hasattr(ui, "decal_pull_btn"))
    # a decal result: a 2 x 1 in sheet with two decals
    _pe_png = app.Path(str(_fs_tmp)) / "pe_sheet.png"
    _pe_img = app.Image.new("RGBA", (600, 300), (0, 0, 0, 0))
    app.ImageDraw.Draw(_pe_img).rectangle([20, 20, 220, 120], fill=(204, 16, 32, 255))
    app.ImageDraw.Draw(_pe_img).ellipse([300, 40, 560, 280], fill=(16, 64, 204, 255))
    _pe_img.save(_pe_png)
    _pe_prm = {"model": "decal", "seed": "pe_sheet", "user_prompt": "pe_sheet",
               "png": str(_pe_png), "film": (205, 215, 225), "size_in": (2.0, 1.0),
               "src": "x.pdf", "page": "pe_sheet", "kind": "process", "src_dpi": 300}
    ui.ui_queue.put(("decal_add", _pe_img.convert("RGB"), _pe_prm, str(_pe_png)))
    ui._poll_queue(); root.update()
    _ents = ui._print_export_entries("all")
    check("every decal result in the gallery is offered for print",
          any(e.get("seed") == "pe_sheet" for e in _ents)
          and ui._print_export_entries("selected")[-1].get("seed") == "pe_sheet")
    _dlg = ui._open_print_export()
    root.update()
    check("the Export dialog opens", _dlg is not None and _dlg.winfo_exists())
    _dlg.destroy()
    ui._run_print_export([_pe_prm], dict(paper="Letter 8.5 × 11 in", landscape=False,
                                         margin_mm=10.0, gap_mm=3.0, formats=("pdf", "png"),
                                         dpi=100), sync=True)
    ui._poll_queue(); root.update()
    _pres = getattr(ui, "_last_print_export", None)
    check("Export writes a PDF and PNG pages and reports it",
          _pres is not None and _pres["pieces"] == 2 and _pres["pages"] == 1
          and any(f.endswith(".pdf") for f in _pres["files"])
          and "Exported 2 decal(s)" in ui.decal_status_var.get(), ui.decal_status_var.get())
    # a figure-scale enlargement bigger than the page is tiled, not shrunk
    _big_prm = dict(_pe_prm, seed="pe_big", kind="preview", size_in=(14.0, 7.0))
    ui._run_print_export([_big_prm], dict(paper="Letter 8.5 × 11 in", landscape=False,
                                          margin_mm=10.0, gap_mm=3.0, formats=("pdf",),
                                          dpi=100), sync=True)
    ui._poll_queue(); root.update()
    _pres2 = ui._last_print_export
    check("a decal bigger than the page is split across pages at full size",
          _pres2["pieces"] == 1 and _pres2["tiles"] >= 2 and _pres2["pages"] >= 2, _pres2)
    # local models: listed, no key needed, the client is Ollama's
    _locals = [m for _l, m in app.vector_redraw.MODELS if app.vector_redraw.is_local(m)]
    check("local vision models are in the list (Qwen3-VL, Gemma 3, Mistral Small)",
          "ollama:qwen3-vl:32b" in _locals and "ollama:qwen3-vl:8b" in _locals
          and any("gemma3" in m for m in _locals) and any("mistral-small" in m for m in _locals))
    ui.decal_vision_model_var.set("Qwen3-VL 8B — local, fast (6 GB)")
    check("picking one gives its Ollama id", ui._vision_model_id() == "ollama:qwen3-vl:8b")
    root.update()
    check("a local model greys out the API key button and says no key is needed",
          "disabled" in ui.decal_key_btn.state()
          and "no key needed" in str(ui.decal_key_lab.cget("text")))
    ui.decal_vision_model_var.set(app.vector_redraw.MODELS[1][0])
    root.update()
    check("…and a Claude model brings it back",
          "disabled" not in ui.decal_key_btn.state()
          and "no key needed" not in str(ui.decal_key_lab.cget("text")))
    ui.decal_vision_model_var.set("Qwen3-VL 8B — local, fast (6 GB)")
    check("Pull local model has a progress bar and an ETA label beside it",
          hasattr(ui, "decal_pull_bar")
          and str(ui.decal_pull_bar.master) == str(ui.decal_pull_btn.master))
    ui.ui_queue.put(("decal_pull_progress", 0.37, "37% · 7.8 / 21.0 GB · about 5 min left"))
    ui._poll_queue(); root.update()
    check("a pull progress message moves the bar and shows the time left",
          int(float(ui.decal_pull_bar["value"])) == 370
          and "min left" in str(ui.decal_local_lab.cget("text")))
    ui.decal_vision_model_var.set(app.vector_redraw.MODELS[0][0])
    app.DECALS_OUT = _old_out
    # stickers glued on green paper: the paper colour is keyed out
    check("the paper-colour button sits beside Add files, auto by default",
          hasattr(ui, "decal_paper_swatch") and ui._decal_paper_rgb() is None
          and "auto" in str(ui.decal_paper_swatch.cget("text"))
          and str(ui.decal_paper_swatch.master) == str(ui.decal_count_var and ui.decal_paper_swatch.master))
    _gp = app.Image.new("RGB", (300, 200), (40, 170, 70))
    _gd = app.ImageDraw.Draw(_gp)
    _gd.rectangle([30, 30, 130, 130], fill=(200, 20, 30))
    _gd.rectangle([160, 40, 260, 140], fill=(250, 250, 250))       # a white-ink sticker
    _gpath = app.Path(str(_fs_tmp)) / "green_paper.png"
    _gp.save(_gpath)
    ui.decal_sources = [str(_gpath)]
    ui._sample_decal_paper(sync=True)
    ui._poll_queue(); root.update()
    check("💧 reads the paper colour off the page and shows it",
          ui._decal_paper_rgb() is not None
          and sum(abs(a - b) for a, b in zip(ui._decal_paper_rgb(), (40, 170, 70))) < 12
          and "#" in str(ui.decal_paper_swatch.cget("text")), ui._decal_paper_rgb())
    _prepg = ui._decal_source_prep()
    _imgp, _dpip, _np_, _php = ui._prepare_source(_gp, _prepg, 300)
    _resg = _dec.process_image(_imgp, mode="cleanup", remove_bg=True, denoise=0, tol=52,
                               target_dpi=300, native_dpi=300, exact=True, tidy_matte=True,
                               photo=_php, carrier=_prepg["paper"])
    _ga = _np6.asarray(_resg["rgba"])
    check("the green paper becomes transparent; the red AND the white stickers stay",
          _ga[10, 10, 3] == 0 and _ga[80, 80, 3] == 255 and _ga[90, 210, 3] == 255
          and _ga[90, 210, 0] > 240, (_ga[10, 10], _ga[80, 80], _ga[90, 210]))
    _rg = _dec.assess_source(_imgp, native_dpi=300, carrier=_prepg["paper"])
    check("the quality report names the picked paper colour",
          any("paper colour you picked" in ln for ln in _rg["report"]), _rg["report"])
    ui._set_decal_paper(None)
    check("Auto clears it", ui._decal_paper_rgb() is None and "auto" in str(ui.decal_paper_swatch.cget("text")))
    # 🖨 Print beside Save As: a decal at its true size, through the print dialog
    check("a Print button sits beside Save As", hasattr(ui, "print_btn"))
    ui.session.append((_pe_img.convert("RGB"), dict(_pe_prm, size_in=(14.0, 7.0)), str(_pe_png)))
    ui.current = len(ui.session) - 1
    _pargs = []
    ui._print_current(runner=lambda a: (_pargs.append(a), "printed")[1], sync=True)
    ui._poll_queue(); root.update()
    check("Print sends a decal at true size (14 in wide: tiled over pages) and reports it",
          getattr(ui, "_last_print", (None,))[0] == "printed" and ui._last_print[1] >= 2
          and "Sent to the printer" in ui.status_var.get(),
          (getattr(ui, "_last_print", None), ui.status_var.get()))
    ui.session.append((_pe_img.convert("RGB"), {"model": "flux", "seed": 7}, str(_pe_png)))
    ui.current = len(ui.session) - 1
    ui._print_current(runner=lambda a: "cancelled", sync=True)
    ui._poll_queue(); root.update()
    check("an ordinary picture prints fitted on one page; Cancel is reported",
          ui._last_print[0] == "cancelled" and ui._last_print[1] == 1
          and "cancelled" in ui.status_var.get())
    ui.decal_sources = []
except Exception as _e:
    import traceback
    traceback.print_exc()
    check("print export / local models run headless", False, repr(_e))

# ---- enclosed holes + text sweep + cancel (v2.19) --------------------------
print("holes, text sweep, cancel")
try:
    # a black ring (window inside) and a small letter 'o' (counter) on transparent
    _ring = app.Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    _rd = app.ImageDraw.Draw(_ring)
    _rd.ellipse([20, 20, 180, 180], fill=(0, 0, 0, 255))
    _rd.ellipse([84, 84, 116, 116], fill=(0, 0, 0, 0))          # 33 px window (1/5 of the disc)
    _rd.rectangle([50, 150, 90, 156], fill=(0, 0, 0, 0))         # a 7 px dash, inside the disc
    _rd.ellipse([220, 80, 260, 120], fill=(200, 20, 30, 255))
    _rd.ellipse([232, 92, 248, 108], fill=(0, 0, 0, 0))         # a letter's counter (40%)
    _filled, _nh = _dec.fill_enclosed_holes(_ring, min_side=4, min_area=24)
    _fa = _np6.asarray(_filled)
    check("the window and the dash inside the disc are filled white; the letter's counter stays clear",
          _nh == 2 and tuple(_fa[100, 100]) == (255, 255, 255, 255)
          and tuple(_fa[153, 60]) == (255, 255, 255, 255) and _fa[100, 240, 3] == 0
          and _fa[10, 10, 3] == 0, (_nh, _fa[100, 100], _fa[153, 60], _fa[100, 240]))
    # through the pipeline: white paper keys white away, the hole rule brings the window back
    _page = app.Image.new("RGB", (300, 200), (252, 252, 252))
    _pd = app.ImageDraw.Draw(_page)
    _pd.ellipse([20, 20, 180, 180], fill=(0, 0, 0))
    _pd.ellipse([84, 84, 116, 116], fill=(255, 255, 255))
    _res_h = _dec.process_image(_page, mode="cleanup", remove_bg=True, denoise=0, tol=52,
                                target_dpi=300, native_dpi=300, exact=True, tidy_matte=True,
                                fill_holes=True)
    _ra = _np6.asarray(_res_h["rgba"])
    check("process_image(fill_holes=True) on white paper: the window is white, the page clear",
          _res_h.get("holes") == 1 and _ra[100, 100, 3] == 255 and _ra[100, 100, 0] == 255
          and _ra[10, 290, 3] == 0, (_res_h.get("holes"), _ra[100, 100]))
    _res_n = _dec.process_image(_page, mode="cleanup", remove_bg=True, denoise=0, tol=52,
                                target_dpi=300, native_dpi=300, exact=True, tidy_matte=True,
                                fill_holes=False)
    check("…and stays a hole when the rule is off", _np6.asarray(_res_n["rgba"])[100, 100, 3] == 0)
    # the whale sheets: pale-blue film, white ink that picks up some film tint
    _film = (223, 254, 255)
    _wp = app.Image.new("RGB", (400, 220), _film)
    _wd = app.ImageDraw.Draw(_wp)
    _wd.ellipse([20, 20, 180, 180], fill=(20, 20, 30))
    _wd.ellipse([50, 50, 150, 150], fill=(232, 252, 253))        # white ink, tinted
    _wd.ellipse([220, 20, 380, 180], fill=(20, 20, 30))
    _wd.ellipse([250, 50, 350, 150], fill=_film)                  # a clear window
    _wd.rectangle([30, 195, 370, 205], fill=(239, 255, 254))      # thin white text stroke
    _rw = _np6.asarray(_dec.process_image(_wp, mode="cleanup", remove_bg=True, denoise=0, tol=52,
                                          target_dpi=300, native_dpi=300, exact=True,
                                          tidy_matte=True, fill_holes=True)["rgba"])
    check("tinted film: white ink inside a decal stays white, a clear window stays clear",
          _rw[100, 100, 3] == 255 and _rw[100, 300, 3] == 0 and _rw[5, 5, 3] == 0,
          (_rw[100, 100], _rw[100, 300], _rw[5, 5]))
    check("tinted film: thin white ink on the film is kept",
          _rw[200, 200, 3] == 255, _rw[200, 200].tolist())
    check("the Decals tab has the hole-fill and text-sweep switches, on by default",
          hasattr(ui, "decal_holes_var") and ui.decal_holes_var.get()
          and hasattr(ui, "decal_text_var") and ui.decal_text_var.get())
    check("Decals and Edit image have a Cancel",
          hasattr(ui, "decal_cancel_btn") and hasattr(ui, "edit_cancel_btn")
          and callable(getattr(ui, "_cancel_decals", None)))
    ui._decals_busy = True
    ui._set_decal_buttons(False)
    check("the Decals Cancel is live only while a job runs",
          "disabled" not in ui.decal_cancel_btn.state() and "disabled" in ui.decal_btn.state())
    ui._cancel_decals()
    check("Cancel raises the shared flag and says so",
          app.CANCEL.is_set() and "ancel" in ui.decal_status_var.get())
    app.CANCEL.clear()
    ui._decals_busy = False
    ui._set_decal_buttons(True)
    check("…and goes grey again when the job is over", "disabled" in ui.decal_cancel_btn.state())
except Exception as _e:
    import traceback
    traceback.print_exc()
    check("holes/text/cancel run headless", False, repr(_e))

# ---- Compare with the original (v2.18) -----------------------------------
print("compare view")
try:
    import compare_view as _cv
    import time as _time8
    # two pictures of the same 2 x 1 inch sheet at different resolutions
    _lo = app.Image.new("RGB", (200, 100), (240, 240, 240))
    app.ImageDraw.Draw(_lo).rectangle([50, 25, 150, 75], fill=(200, 20, 30))
    _hi = app.Image.new("RGB", (400, 200), (255, 255, 255))
    app.ImageDraw.Draw(_hi).rectangle([100, 50, 300, 150], fill=(210, 25, 35))
    _pl = _cv.Pane("orig", image=_lo, ppi=100)
    _pr = _cv.Pane("result", image=_hi, ppi=200)
    check("panes share the inch grid", _pl.size_in == (2.0, 1.0) and _pr.size_in == (2.0, 1.0))
    _fl = _cv.render_frame(_pl, 400, 200, 150, 0.0, 0.0)
    _fr = _cv.render_frame(_pr, 400, 200, 150, 0.0, 0.0)
    _al, _ar = _np6.asarray(_fl), _np6.asarray(_fr)
    check("the same grid point lands on the same screen pixel on both sides",
          _fl.size == (400, 200) and _ar[112, 150, 0] > 180 and _ar[112, 150, 1] < 80
          and _al[112, 150, 0] > 180 and _al[112, 150, 1] < 80
          and _al[112, 50, 0] > 200 and _al[112, 50, 1] > 200,
          (_al[112, 150], _ar[112, 150]))
    _wf = _cv.wipe_frame(_fl, _fr, 200)
    _aw = _np6.asarray(_wf)
    # at 150 px/in the 2-inch sheet spans x 0..300: sample inside it on both sides
    check("the wipe shows the original left of the divider and the result right of it",
          int(_aw[10, 100, 0]) == 240 and int(_aw[10, 250, 0]) == 255
          and int(_aw[10, 350, 0]) == 60, (_aw[10, 100], _aw[10, 250], _aw[10, 350]))
    _po = _cv.Pane("decal", image=_hi, ppi=200, offset=(0.5, 0.25))
    _fo = _cv.render_frame(_po, 400, 200, 100, 0.0, 0.0)
    _ao = _np6.asarray(_fo)
    check("an offset pane sits at its place on the grid",
          _ao[10, 10, 0] == 60 and _ao[100, 200, 0] > 180, (_ao[10, 10], _ao[100, 200]))
    _win = _cv.CompareWindow(root, _pl, _pr, on_rerun=lambda: True)
    _win.render()
    _s0 = _win.s if _win.s is not None else _win._fit_scale(*_win._pane_size())
    _win.zoom_at(2.0, 10, 10)
    check("wheel zoom doubles the scale and keeps the cursor's grid point",
          abs(_win.s / _s0 - 2.0) < 1e-6, (_s0, _win.s))
    _ox = _win.ox
    _win.pan(50, 0)
    check("drag pans in inches", abs((_ox - _win.ox) - 50 / _win.s) < 1e-9)
    _win.mode.set("wipe"); _win._mode_changed(); _win.render()
    check("wipe mode renders one composite", "wipe" in _win._frames)
    _win._rerun()
    check("Re-run reports it started", "Re-running" in _win.msg_var.get(), _win.msg_var.get())
    _win.set_right(_cv.Pane("new", image=_lo, ppi=100), note="swapped")
    check("set_right swaps the result side", _win.right.title == "new" and _win.msg_var.get() == "swapped")
    _win.destroy()
    # the App side: a processed entry knows its source; Compare loads the
    # original from it and Re-run re-processes that sheet only
    check("the Decals tab has the Compare button", hasattr(ui, "decal_compare_btn"))
    _png8 = app.Path(str(_fs_tmp)) / "cmp_result.png"
    _hi.convert("RGBA").save(_png8)
    _prm8 = {"model": "decal", "seed": "dpi_probe", "user_prompt": "dpi_probe",
             "png": str(_png8), "film": (205, 215, 225), "size_in": (2.0, 1.0),
             "src": str(_pdf), "page": "dpi_probe", "kind": "process", "src_dpi": 200}
    ui.tagged = set()
    ui.ui_queue.put(("decal_add", _hi, _prm8, str(_png8)))
    ui._poll_queue(); root.update()
    ui._open_decal_compare()
    for _ in range(200):
        ui._poll_queue(); root.update()
        if getattr(ui, "_compare_win", None) is not None:
            break
        _time8.sleep(0.05)
    _w8 = getattr(ui, "_compare_win", None)
    check("Compare opens with the original page (from the PDF) beside the result",
          _w8 is not None and _w8.left.image is not None and _w8.left.image.size == (400, 200)
          and _w8.left.ppi == 200 and abs(_w8.right.ppi - 200) < 1e-6, (ui.decal_status_var.get()))
    if _w8 is not None:
        _old_right = _w8.right
        ui.decal_mode_var.set("cleanup"); ui.decal_ai_var.set(False)
        check("Re-run starts a job on that sheet only",
              ui._compare_rerun(_prm8) and ui.decal_sources[-1] == str(_pdf) and ui._decals_busy)
        for _ in range(400):
            ui._poll_queue(); root.update()
            if not ui._decals_busy and _w8.right is not _old_right:
                break
            _time8.sleep(0.05)
        check("…and the result side is swapped when the new picture lands",
              _w8.right is not _old_right and ui._compare_wait is None
              and "new picture" in _w8.msg_var.get(), (ui.decal_status_var.get(), _w8.msg_var.get()))
        _w8.destroy()
        # a finished job opens Compare by itself on its last result
        ui._compare_win = None
        ui._last_run_entry = None
        ui.ui_queue.put(("decal_add", _hi, dict(_prm8), str(_png8)))
        ui.ui_queue.put(("decal_done", 1, None, False, "process"))
        for _ in range(200):
            ui._poll_queue(); root.update()
            if getattr(ui, "_compare_win", None) is not None:
                break
            _time8.sleep(0.05)
        _w9 = getattr(ui, "_compare_win", None)
        check("no job opens Compare by itself (the user presses ⇄); the Done line stays",
              _w9 is None and ui.decal_status_var.get().startswith("Done"),
              ui.decal_status_var.get())
        if _w9 is not None:
            _w9.destroy()
        ui._compare_win = None
        # …but a finished Redraw leaves Compare to the user
        ui.ui_queue.put(("decal_add", _hi, dict(_prm8, kind="redraw"), str(_png8)))
        ui.ui_queue.put(("decal_done", 1, None, False, "redraw"))
        for _ in range(20):
            ui._poll_queue(); root.update()
            _time8.sleep(0.05)
        check("a finished Redraw does not open Compare by itself",
              getattr(ui, "_compare_win", None) is None and ui._last_run_entry is None)
    ui.decal_sources = [s for s in ui.decal_sources if s != str(_pdf)]
    ui.decal_list.delete(0, "end")
except Exception as _e:
    import traceback
    traceback.print_exc()
    check("compare view runs headless", False, repr(_e))

# ---- run figures + progress bar (v2.17.1) --------------------------------
print("decal progress")
try:
    import tkinter.ttk as _ttk7
    check("the Decals tab has the green count badge, the red cost badge and the bar",
          hasattr(ui, "decal_count_badge") and hasattr(ui, "decal_cost_badge")
          and hasattr(ui, "decal_progress")
          and str(ui.decal_count_badge.cget("style")) == "Good.TLabel"
          and str(ui.decal_cost_badge.cget("style")) == "Cost.TLabel")
    _sty7 = _ttk7.Style(root)
    check("green is green and red is red",
          _sty7.lookup("Good.TLabel", "foreground") == "#22c55e"
          and _sty7.lookup("Cost.TLabel", "foreground") == "#ef4444",
          (_sty7.lookup("Good.TLabel", "foreground"), _sty7.lookup("Cost.TLabel", "foreground")))
    ui.ui_queue.put(("decal_progress", 0.5, "3 / 6 decals (2 drawn, 1 traced)", "$0.12"))
    ui._poll_queue()
    root.update()
    check("a progress message moves the bar and fills the figures",
          int(float(ui.decal_progress["value"])) == 500
          and str(ui.decal_count_badge.cget("text")).startswith("3 / 6")
          and str(ui.decal_cost_badge.cget("text")) == "$0.12",
          (ui.decal_progress["value"], ui.decal_count_badge.cget("text")))
    ui._decal_note = ""
    ui.ui_queue.put(("decal_done", 1, None, False, "redraw"))
    ui._poll_queue()
    root.update()
    check("a finished run fills the bar", int(float(ui.decal_progress["value"])) == 1000)
    # Cancel clears the bar, the decal count and the cost at once, and keeps
    # them cleared against late updates from the stopping job
    ui._decals_busy = True
    ui._set_decal_buttons(False)
    ui.ui_queue.put(("decal_progress", 0.4, "4 / 10 decals (3 drawn)", "$0.21"))
    ui._poll_queue(); root.update()
    ui._cancel_decals()
    root.update()
    _cleared = (int(float(ui.decal_progress["value"])) == 0
                and str(ui.decal_count_badge.cget("text")) == ""
                and str(ui.decal_cost_badge.cget("text")) == "")
    ui.ui_queue.put(("decal_progress", 0.5, "5 / 10 decals", "$0.25"))
    ui.ui_queue.put(("decal_done", 4, "cancelled", False, "redraw"))
    ui._poll_queue(); root.update()
    check("Cancel clears the progress bar, the decal count and the cost — and they stay cleared",
          _cleared and int(float(ui.decal_progress["value"])) == 0
          and str(ui.decal_cost_badge.cget("text")) == ""
          and str(ui.decal_count_badge.cget("text")) == "",
          (ui.decal_progress["value"], ui.decal_count_badge.cget("text"), ui.decal_cost_badge.cget("text")))
    app.CANCEL.clear()
    ui._decal_progress_reset("counting the decals…", new_run=True)
    ui.ui_queue.put(("decal_progress", 0.2, "2 / 10 decals", "$0.05"))
    ui._poll_queue(); root.update()
    check("the next run shows its progress again", int(float(ui.decal_progress["value"])) == 200)
    _src11 = open(app.__file__, encoding="utf-8").read()
    _j = _src11.index("def _cancel_generation")
    check("the main ✕ Cancel also stops a running Decals job",
          "self._cancel_decals()" in _src11[_j:_j + 600])
    ui._decal_progress_reset("counting the decals…")
    check("a new run empties the bar and the cost",
          int(float(ui.decal_progress["value"])) == 0
          and str(ui.decal_cost_badge.cget("text")) == ""
          and str(ui.decal_count_badge.cget("text")) == "counting the decals…")
except Exception as _e:
    import traceback
    traceback.print_exc()
    check("decal progress runs headless", False, repr(_e))

print("taskbar identity")
_scr = app._shortcut_fix_script(r"D:\Some Dir\AIImageGeneratorSuite.exe")
check("the shortcut upkeep script targets this exe and stamps the app's ID",
      r"D:\Some Dir\AIImageGeneratorSuite.exe" in _scr
      and app.APP_AUMID in _scr and "User Pinned" in _scr
      and "Start Menu" in _scr)
check("…and only ever touches shortcuts to this app, never unpins or deletes",
      "ComicArtCreator.exe" in _scr and "Remove-Item" not in _scr
      and "unpin" not in _scr.lower() and "ReleaseComObject" in _scr)
check("a quote in the install path is escaped for PowerShell",
      "''" in app._shortcut_fix_script(r"D:\O'Brien\AIImageGeneratorSuite.exe"))
check("focusing a running copy reports False when no such window exists",
      app._focus_running_instance(title_prefix="ZZZ-no-such-window-ZZZ") is False)
check("shortcut upkeep is a no-op outside a frozen build (tests, dev)",
      app._ensure_shortcuts() is None)
# the window itself carries the app's identity (v2.16.3): set in App.__init__,
# readable back through the same shell property store
_hwnd = app.ctypes.windll.user32.GetParent(root.winfo_id())
check("the main window is tagged with the app's AppUserModelID",
      bool(_hwnd) and app.get_window_aumid(_hwnd) == app.APP_AUMID,
      (bool(_hwnd), app.get_window_aumid(_hwnd)))
check("tagging an unrelated window id fails quietly (no exception)",
      app.set_window_aumid(0x12345) in (True, False))

# ---- closing ------------------------------------------------------------
print("closing")
destroyed.clear()
# the engine must be killed on close EVEN when we don't "own" it — the
# ownership gate used to leave the engine (and its VRAM) running after an
# update relaunched the app
_killed = []
_real_kill = app.kill_engine
app.kill_engine = lambda: _killed.append(1)
app.engine_ours_to_stop = lambda: False
_swept = []
_real_sweep = su.sweep_old_exes
su.sweep_old_exes = lambda: _swept.append(1)
ui._on_close()
check("closing returns before the engine shutdown is done", not destroyed)
pump(root, lambda: destroyed, timeout=10)
check("the app closes once the shutdown thread reports back",
      bool(destroyed))
check("the engine is killed on close even when not 'ours' (no VRAM leak)",
      bool(_killed))
check("leftover *_old_*.exe copies are swept again on close", bool(_swept))
app.kill_engine = _real_kill
su.sweep_old_exes = _real_sweep

print()
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
