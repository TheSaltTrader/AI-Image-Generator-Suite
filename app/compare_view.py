"""Compare a decal result with the original it came from, inside the app.

Two pictures on ONE physical grid (inches): the original page — or the
decal's own region of it — and the result (cleaned, traced or redrawn),
side by side or under a wipe divider, with the zoom and the pan linked so
the same spot is under the eye on both sides. A Re-run button hands the
job back to the app with the settings as they are now, and the result side
is swapped when the new picture lands — tweak, re-run, look, repeat.

A Pane is one side: a PIL image, its pixels-per-inch, where it sits on the
inch grid (offset), the colour behind it, and optionally the SVG it was
rendered from (zoomed in past the raster, the SVG region is rendered crisp
instead of enlarging pixels).
"""
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk


class Pane:
    def __init__(self, title, image=None, ppi=300.0, offset=(0.0, 0.0),
                 backing=(235, 235, 235), svg=None, rgba=None):
        self.title = title
        self.image = image.convert("RGB") if image is not None else None
        self.rgba = rgba            # the transparent result (clear-film preview)
        self.ppi = float(ppi) if ppi else 300.0
        self.offset = (float(offset[0]), float(offset[1]))
        self.backing = tuple(int(v) for v in backing) if backing else (235, 235, 235)
        self.svg = svg

    @property
    def size_in(self):
        if self.image is None:
            return (0.0, 0.0)
        return (self.image.width / self.ppi, self.image.height / self.ppi)

    @property
    def extent_in(self):
        """(x0, y0, x1, y1) on the inch grid."""
        w, h = self.size_in
        return (self.offset[0], self.offset[1], self.offset[0] + w,
                self.offset[1] + h)


def render_frame(pane, W, H, s, ox, oy, fill=(60, 60, 60), svg_render=None):
    """The W×H picture a pane shows at `s` screen px per inch with the
    grid point (ox, oy) inches at the top-left. Pure function, so it can
    be tested without a window. `svg_render(svg, img_width_px, box, scale,
    backing)` renders a region of the SVG crisp when zoomed past 1:1."""
    frame = Image.new("RGB", (max(1, W), max(1, H)), fill)
    if pane is None or pane.image is None or s <= 0:
        return frame
    scale = s / pane.ppi                       # screen px per image px
    x0 = (ox - pane.offset[0]) * pane.ppi      # visible window, image px
    y0 = (oy - pane.offset[1]) * pane.ppi
    x1 = x0 + W / scale
    y1 = y0 + H / scale
    ix0, iy0 = max(0, int(x0)), max(0, int(y0))
    ix1 = min(pane.image.width, int(x1) + 1)
    iy1 = min(pane.image.height, int(y1) + 1)
    if ix1 <= ix0 or iy1 <= iy0:
        return frame
    dw = max(1, int(round((ix1 - ix0) * scale)))
    dh = max(1, int(round((iy1 - iy0) * scale)))
    disp = None
    if pane.svg and scale > 1.0 and svg_render is not None:
        try:
            disp = svg_render(pane.svg, pane.image.width, (ix0, iy0, ix1, iy1),
                              scale, pane.backing)
            if disp is not None and disp.size != (dw, dh):
                disp = disp.resize((dw, dh), Image.LANCZOS)
        except Exception:
            disp = None
    if disp is None:
        crop = pane.image.crop((ix0, iy0, ix1, iy1))
        method = (Image.LANCZOS if scale < 1.0 else
                  Image.NEAREST if scale >= 3.0 else Image.BILINEAR)
        disp = crop.resize((dw, dh), method)
    px = int(round((ix0 - x0) * scale))
    py = int(round((iy0 - y0) * scale))
    frame.paste(disp, (px, py))
    return frame


FILM_RGB = (214, 222, 226)          # clear decal film over light card


def clear_film_preview(rgba, film=FILM_RGB, white_min=235, chroma_max=24):
    """How the decal prints on CLEAR film with no white ink: the white ink
    is not printed (the film shows through there). Returns RGB."""
    import numpy as np
    a = np.asarray(rgba.convert("RGBA")).astype(np.int32)
    white = (a[..., 3] > 128) & (a[..., :3].min(2) >= white_min) & \
        ((a[..., :3].max(2) - a[..., :3].min(2)) <= chroma_max)
    a[..., 3] = np.where(white, 0, a[..., 3])
    im = Image.fromarray(a.astype(np.uint8), "RGBA")
    bg = Image.new("RGB", im.size, film)
    bg.paste(im, mask=im.split()[3])
    return bg


def wipe_frame(left_frame, right_frame, divider_px):
    """One picture: the left pane up to the divider, the right pane past it."""
    W, H = right_frame.size
    d = max(0, min(W, int(divider_px)))
    out = right_frame.copy()
    if d > 0:
        out.paste(left_frame.crop((0, 0, d, H)), (0, 0))
    return out


class CompareWindow(tk.Toplevel):
    """The window. Wheel = zoom about the cursor, drag = pan (both sides
    move together), Fit = whole picture, the wipe divider drags."""

    def __init__(self, master, left, right, on_rerun=None, colours=None,
                 title="Compare with the original", svg_render=None,
                 on_decal_action=None):
        super().__init__(master)
        c = colours or {}
        self.bg = c.get("bg", "#1e1e22")
        self.fg = c.get("fg", "#e6e6e6")
        self.dim = c.get("dim", "#9a9aa3")
        self.fill = c.get("fill", (48, 48, 54))
        self.title(title)
        self.configure(bg=self.bg)
        self.left, self.right = left, right
        self.on_rerun = on_rerun
        self.on_decal_action = on_decal_action
        self.svg_render = svg_render
        self.params = None
        self.crop_box = None
        self.s = None                 # screen px per inch; None = fit
        self.ox = self.oy = 0.0       # grid point at the top-left, inches
        self.wipe = 0.5               # divider, share of the width
        self.mode = tk.StringVar(value="side")
        self._tk = {}
        self._frames = {}
        self._drag = None
        self._pending = None
        self._build()
        self.geometry("1100x720")
        self.minsize(640, 400)
        self.bind("<Configure>", lambda e: self._schedule())
        self.after(50, self.render)

    # ---------------------------------------------------------------- UI
    def _build(self):
        bar = tk.Frame(self, bg=self.bg)
        bar.pack(side="top", fill="x", padx=8, pady=(8, 4))
        ttk.Radiobutton(bar, text="Side by side", value="side",
                        variable=self.mode, command=self._mode_changed
                        ).pack(side="left")
        ttk.Radiobutton(bar, text="Wipe", value="wipe", variable=self.mode,
                        command=self._mode_changed).pack(side="left", padx=(6, 12))
        ttk.Button(bar, text="Fit", command=self.fit).pack(side="left")
        ttk.Button(bar, text="1:1", command=self.one_to_one).pack(side="left",
                                                                  padx=(4, 0))
        self.zoom_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.zoom_var, bg=self.bg, fg=self.dim,
                 width=9, anchor="w").pack(side="left", padx=(8, 0))
        self.film_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Printed on clear film, no white ink",
                        variable=self.film_var,
                        command=self._film_changed).pack(side="left", padx=(12, 0))
        if self.on_rerun is not None:
            self.rerun_btn = ttk.Button(
                bar, text="↻ Re-run with the current settings",
                command=self._rerun)
            self.rerun_btn.pack(side="left", padx=(12, 0))
        tk.Label(bar, text="wheel = zoom · drag = pan · both sides move "
                           "together"
                           + (" · right-click a decal to fix it"
                              if self.on_decal_action else ""),
                 bg=self.bg, fg=self.dim).pack(side="right")
        self.msg_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.msg_var, bg=self.bg, fg=self.dim,
                 anchor="w").pack(side="bottom", fill="x", padx=8, pady=(0, 6))
        self.body = tk.Frame(self, bg=self.bg)
        self.body.pack(side="top", fill="both", expand=True, padx=8, pady=4)
        self.body.columnconfigure(0, weight=1)
        self.body.columnconfigure(1, weight=1)
        self.body.rowconfigure(1, weight=1)
        self.lab_l = tk.Label(self.body, text=self.left.title, bg=self.bg,
                              fg=self.fg, anchor="w")
        self.lab_r = tk.Label(self.body, text=self.right.title, bg=self.bg,
                              fg=self.fg, anchor="w")
        self.cv_l = tk.Canvas(self.body, bg="#303036", highlightthickness=0)
        self.cv_r = tk.Canvas(self.body, bg="#303036", highlightthickness=0)
        for cv in (self.cv_l, self.cv_r):
            cv.bind("<MouseWheel>", self._on_wheel)
            cv.bind("<ButtonPress-1>", self._on_press)
            cv.bind("<B1-Motion>", self._on_motion)
            cv.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
            cv.bind("<Configure>", lambda e: self._schedule())
            cv.bind("<ButtonPress-3>", self._on_menu)
        self._layout()

    def _layout(self):
        for w in (self.lab_l, self.lab_r, self.cv_l, self.cv_r):
            w.grid_forget()
        if self.mode.get() == "wipe":
            self.lab_l.configure(text=f"{self.left.title}  ◀ divider ▶  "
                                      f"{self.right.title}")
            self.lab_l.grid(row=0, column=0, columnspan=2, sticky="ew")
            self.cv_l.grid(row=1, column=0, columnspan=2, sticky="nsew")
        else:
            self.lab_l.configure(text=self.left.title)
            self.lab_l.grid(row=0, column=0, sticky="ew")
            self.lab_r.grid(row=0, column=1, sticky="ew", padx=(6, 0))
            self.cv_l.grid(row=1, column=0, sticky="nsew")
            self.cv_r.grid(row=1, column=1, sticky="nsew", padx=(6, 0))

    def _mode_changed(self):
        self._layout()
        self._schedule()

    # ---------------------------------------------------------------- view
    def _extent(self):
        l = self.left.extent_in
        r = self.right.extent_in
        if self.left.image is None:
            return r
        if self.right.image is None:
            return l
        return (min(l[0], r[0]), min(l[1], r[1]), max(l[2], r[2]),
                max(l[3], r[3]))

    def _pane_size(self):
        W = max(self.cv_l.winfo_width(), 50)
        H = max(self.cv_l.winfo_height(), 50)
        return W, H

    def _fit_scale(self, W, H):
        x0, y0, x1, y1 = self._extent()
        w, h = max(1e-6, x1 - x0), max(1e-6, y1 - y0)
        return max(1e-6, min((W - 16) / w, (H - 16) / h))

    def fit(self):
        self.s = None
        self._schedule()

    def one_to_one(self):
        """One screen pixel per pixel of the original."""
        W, H = self._pane_size()
        cx, cy = W / 2.0, H / 2.0
        s_old = self.s or self._fit_scale(W, H)
        if self.s is None:
            self._centre(W, H, s_old)
        ix, iy = self.ox + cx / s_old, self.oy + cy / s_old
        self.s = float(self.left.ppi if self.left.image is not None
                       else self.right.ppi)
        self.ox, self.oy = ix - cx / self.s, iy - cy / self.s
        self._schedule()

    def _centre(self, W, H, s):
        x0, y0, x1, y1 = self._extent()
        self.ox = x0 - (W / s - (x1 - x0)) / 2.0
        self.oy = y0 - (H / s - (y1 - y0)) / 2.0

    def zoom_at(self, factor, cx, cy):
        W, H = self._pane_size()
        s_old = self.s or self._fit_scale(W, H)
        if self.s is None:
            self._centre(W, H, s_old)
        s_new = s_old * factor
        lo = self._fit_scale(W, H) / 4.0
        hi = 64.0 * max(self.left.ppi, self.right.ppi)
        s_new = max(lo, min(hi, s_new))
        ix, iy = self.ox + cx / s_old, self.oy + cy / s_old
        self.s = s_new
        self.ox, self.oy = ix - cx / s_new, iy - cy / s_new
        self._schedule()

    def pan(self, dx_px, dy_px):
        W, H = self._pane_size()
        if self.s is None:
            self.s = self._fit_scale(W, H)
            self._centre(W, H, self.s)
        self.ox -= dx_px / self.s
        self.oy -= dy_px / self.s
        self._schedule()

    DECAL_ACTIONS = (
        ("text", "Redraw this decal: set its lettering in type"),
        ("geometric", "Redraw this decal: straight lines (banners, flags)"),
        ("vector", "Redraw this decal: Recraft drawing"),
        ("trace", "Redraw this decal: clean trace of the scan"),
        (None, None),
        ("fill", "Fill this spot (the colour around it)"),
        ("clear", "Make this spot clear (no ink)"),
    )

    def screen_to_inch(self, x, y):
        """A point on a canvas as inches on the shared grid."""
        W, H = self._pane_size()
        s = self.s or self._fit_scale(W, H)
        return self.ox + x / float(s), self.oy + y / float(s)

    def _on_menu(self, e):
        """Right-click on a decal: redraw it another way, or fill / clear
        the spot under the cursor (the app does the work)."""
        if self.on_decal_action is None:
            return
        x_in, y_in = self.screen_to_inch(e.x, e.y)
        m = tk.Menu(self, tearoff=0)
        for key, text in self.DECAL_ACTIONS:
            if key is None:
                m.add_separator()
                continue
            m.add_command(label=text, command=lambda k=key: self._decal_action(
                k, x_in, y_in))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _decal_action(self, key, x_in, y_in):
        try:
            msg = self.on_decal_action(key, x_in, y_in)
        except Exception as ex:
            msg = f"Could not start: {ex}"
        if msg:
            self.msg_var.set(msg)

    def _film_changed(self):
        """Swap the result side between the screen view and the clear-film
        print preview (white ink not printed)."""
        pane = self.right
        if pane is None or getattr(pane, "rgba", None) is None:
            self.msg_var.set("No transparent result to preview on clear film.")
            return
        if not hasattr(pane, "_screen_image"):
            pane._screen_image = pane.image
            pane._screen_svg = pane.svg
        if self.film_var.get():
            pane.image = clear_film_preview(pane.rgba)
            pane.svg = None           # the preview is the raster
            self.msg_var.set("Clear film without white ink: the white parts "
                             "are not printed. Use a white-ink printer or the "
                             "white layer in Export for print.")
        else:
            pane.image = pane._screen_image
            pane.svg = pane._screen_svg
            self.msg_var.set("")
        self._schedule()

    def set_right(self, pane, note=""):
        """A new result landed (after Re-run): swap it in, same view."""
        self.right = pane
        self.lab_r.configure(text=pane.title)
        self._layout()
        if note:
            self.msg_var.set(note)
        self._schedule()

    # ---------------------------------------------------------------- events
    def _on_wheel(self, e):
        steps = e.delta / 120.0 if e.delta else 0
        if not steps:
            return
        self.zoom_at(1.25 ** steps, e.x, e.y)

    def _on_press(self, e):
        W, H = self._pane_size()
        if self.mode.get() == "wipe" and abs(e.x - self.wipe * W) <= 10:
            self._drag = ("wipe", e.x, e.y)
        else:
            self._drag = ("pan", e.x, e.y)

    def _on_motion(self, e):
        if not self._drag:
            return
        kind, x0, y0 = self._drag
        if kind == "wipe":
            W, H = self._pane_size()
            self.wipe = max(0.0, min(1.0, e.x / float(W)))
            self._schedule()
        else:
            self.pan(e.x - x0, e.y - y0)
        self._drag = (kind, e.x, e.y)

    def _rerun(self):
        if self.on_rerun is None:
            return
        try:
            ok = self.on_rerun()
        except Exception as ex:
            ok = False
            self.msg_var.set(f"Re-run failed to start: {ex}")
            return
        self.msg_var.set("Re-running with the current settings — the result "
                         "side updates when it lands…" if ok else
                         "A Decals job is already running — wait for it to "
                         "finish, then Re-run.")

    # ---------------------------------------------------------------- draw
    def _schedule(self):
        if self._pending is None:
            try:
                self._pending = self.after(16, self.render)
            except Exception:
                self._pending = None

    def render(self):
        self._pending = None
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        W, H = self._pane_size()
        s = self.s or self._fit_scale(W, H)
        if self.s is None:
            self._centre(W, H, s)
        self._frames = {}
        fl = render_frame(self.left, W, H, s, self.ox, self.oy, self.fill,
                          self.svg_render)
        fr = render_frame(self.right, W, H, s, self.ox, self.oy, self.fill,
                          self.svg_render)
        if self.mode.get() == "wipe":
            d = int(self.wipe * W)
            out = wipe_frame(fl, fr, d)
            self._frames["wipe"] = out
            self._tk["l"] = ImageTk.PhotoImage(out)
            self.cv_l.delete("all")
            self.cv_l.create_image(0, 0, anchor="nw", image=self._tk["l"])
            self.cv_l.create_line(d, 0, d, H, fill="#ffcc00", width=2)
        else:
            self._frames["l"], self._frames["r"] = fl, fr
            self._tk["l"] = ImageTk.PhotoImage(fl)
            self._tk["r"] = ImageTk.PhotoImage(fr)
            self.cv_l.delete("all")
            self.cv_l.create_image(0, 0, anchor="nw", image=self._tk["l"])
            self.cv_r.delete("all")
            self.cv_r.create_image(0, 0, anchor="nw", image=self._tk["r"])
        ref = self.left.ppi if self.left.image is not None else self.right.ppi
        self.zoom_var.set(f"{100.0 * s / ref:.0f}%")
