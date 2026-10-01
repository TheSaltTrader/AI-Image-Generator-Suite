"""Decal restoration pipeline — turn an imperfect scan of a sticker/decal sheet
into a clean, print-ready, transparent-background image (and optional SVG).

Faithful, NOT a redraw: it denoises the scan, lifts the artwork off its carrier
film / background (keeping white ink), sharpens, and either

  * CLEANUP  — a high-res transparent raster (robust for whole mixed sheets), or
  * VECTOR   — traces flat art to crisp, infinitely-scalable vectors (best for
               logos/symbols/bold text), rasterised back to a transparent PNG.

Runs anywhere numpy + Pillow are present; PDF input needs PyMuPDF and VECTOR
needs vtracer (both optional — the app installs them into the engine venv on
first use). Importable and runnable as a CLI (see main())."""

import io
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
PDF_EXTS = {".pdf"}
SUPPORTED_EXTS = IMAGE_EXTS | PDF_EXTS

# Figure/model scales as 1/N (label -> denominator N). Converting a decal from
# one figure scale to another enlarges/shrinks it by factor = source_N / target_N
# — e.g. a 3.75" (1/18) sheet onto a 1/12 "Classified" 6" vehicle is 18/12 = 1.5x.
SCALE_PRESETS = [
    ('1/12 — 6" Classified', 12),
    ('1/18 — 3.75" (ARAH / Retro)', 18),
    ('1/6 — 12" figure', 6),
    ('1/10', 10),
    ('1/16', 16),
    ('1/24', 24),
    ('1/35', 35),
    ('1/48', 48),
]
SCALE_N = dict(SCALE_PRESETS)


def scale_factor(source_n, target_n):
    """Linear resize factor to convert a decal from a 1/source_n figure scale
    to a 1/target_n one. >1 enlarges (e.g. 3.75\"->Classified = 18/12 = 1.5)."""
    try:
        f = float(source_n) / float(target_n)
        return f if f > 0 else 1.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0


# ---------------------------------------------------------------- input
def iter_source_images(path):
    """Yield (label, PIL.Image RGB) for every page/image in a source file.
    Supports PDF (each page's embedded image, at its native resolution) and
    ordinary raster formats. A multi-page PDF yields one entry per page."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in PDF_EXTS:
        import pymupdf  # noqa
        doc = pymupdf.open(str(path))
        for pno in range(doc.page_count):
            page = doc[pno]
            imgs = page.get_images(full=True)
            got = None
            if imgs:
                # the largest embedded image at native pixels (scanners embed
                # one full-page JPEG); fall back to rendering the page
                best = max(imgs, key=lambda im: doc.extract_image(im[0])["width"]
                           * doc.extract_image(im[0])["height"])
                info = doc.extract_image(best[0])
                got = Image.open(io.BytesIO(info["image"])).convert("RGB")
            if got is None:
                pm = page.get_pixmap(dpi=300)
                got = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
            label = path.stem if doc.page_count == 1 else f"{path.stem}_p{pno + 1}"
            yield label, got
        doc.close()
    else:
        yield path.stem, Image.open(path).convert("RGB")


# ---------------------------------------------------------------- cleaning
def _auto_orient(img):
    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        return img


def detect_carrier(img, border=0.06):
    """The scan's background/carrier-film colour, sampled as the median of a
    thin border frame (the edges are almost always empty carrier). Returns an
    (r,g,b) tuple, or None if the border is already near-white/neutral (then a
    plain white background is assumed)."""
    a = np.asarray(img.convert("RGB"))
    h, w = a.shape[:2]
    bw = max(2, int(min(h, w) * border))
    frame = np.concatenate([
        a[:bw].reshape(-1, 3), a[-bw:].reshape(-1, 3),
        a[:, :bw].reshape(-1, 3), a[:, -bw:].reshape(-1, 3)])
    med = np.median(frame, axis=0)
    return tuple(int(v) for v in med)


def is_neutral_carrier(carrier):
    """True when the scan's backing is plain white/neutral paper rather than
    a tinted carrier film. On white paper, white IS the background: white
    ink cannot be told from it (and would not print on white anyway)."""
    try:
        c = [float(v) for v in carrier]
    except (TypeError, ValueError):
        return False
    return (max(c) - min(c)) < 14 and (sum(c) / 3.0) > 225


def _carrier_alpha(a_rgb, carrier, tol=52, soft=18):
    """Alpha (0=carrier/transparent, 255=keep) that removes the carrier colour
    while KEEPING white ink and coloured art.

    White ink sits close to a light carrier in plain distance, so we key on the
    carrier's TINT direction: the carrier is a light, faintly-tinted colour, so
    a pixel is carrier only when it is light AND shares that tint. Neutral white
    (no tint) and saturated art (different tint) are kept.

    On a NEUTRAL carrier (a sheet scanned on white paper) that protection is
    turned off and the key is plain colour distance: with it on, a whole white
    page stayed opaque, became one giant "decal" and the AI redraw invented
    art into it (v2.15.2)."""
    a = a_rgb.astype(np.float32)
    C = np.array(carrier, np.float32)
    grayC = C.mean()
    tintC = C - grayC
    ntC = float(np.linalg.norm(tintC)) or 1.0
    grayP = a.mean(2, keepdims=True)
    tintP = a - grayP                       # each pixel's chroma vector
    proj = (tintP * tintC).sum(2) / (ntC * ntC)      # tint alignment w/ carrier
    dist = np.linalg.norm(a - C, axis=2)             # colour distance
    light = a.mean(2)
    if is_neutral_carrier(carrier):
        is_white = np.zeros(dist.shape, bool)         # white = background
        carrier_like = dist < tol
    else:
        is_white = (np.abs(tintP).sum(2) < 24) & (light > 210)   # neutral & bright
        carrier_like = ((dist < tol) | ((proj > 0.55) & (proj < 1.8)
                        & (np.abs(light - grayC) < 40))) & (~is_white)
    # soft edge: ramp alpha over `soft` units of distance past the hard cut
    alpha = np.clip((dist - tol) / max(1, soft), 0, 1) * 255
    alpha[~carrier_like] = 255
    alpha[carrier_like & (dist < tol)] = 0
    return alpha.astype(np.uint8)


def destripe(img, window=25, z_thresh=5.0):
    """Remove thin, full-height vertical scanner streak lines SURGICALLY:
    only the few columns that are genuine streaks are touched; every other
    column is left BYTE-IDENTICAL.

    A streak column differs consistently from a horizontally-smoothed baseline
    all the way down (high median offset), and stands out from its neighbours.
    We flag those by a robust z-score of the per-column offset magnitude, then
    replace each flagged column by interpolating its two nearest CLEAN columns —
    so the defect is filled from the real art around it, nothing else changes."""
    a = np.asarray(img.convert("RGB")).astype(np.float32)
    h, w, _ = a.shape
    k = window if window % 2 else window + 1
    pad = k // 2
    ap = np.pad(a, ((0, 0), (pad, pad), (0, 0)), mode="edge")
    cs = np.cumsum(ap, axis=1)
    cs = np.pad(cs, ((0, 0), (1, 0), (0, 0)))
    smooth = (cs[:, k:, :] - cs[:, :-k, :]) / k
    bias = np.median(a - smooth[:, :w, :], axis=0)          # (w, 3)
    mag = np.abs(bias).sum(axis=1)                          # (w,) streak strength
    med = np.median(mag)
    mad = np.median(np.abs(mag - med)) + 1e-3
    z = (mag - med) / (1.4826 * mad)
    streak = z > z_thresh                                   # strong anomalies
    # keep only THIN runs (<=3px) — a real scanner line is 1-3px; wider runs
    # are art edges (a bar/letter) and must be left alone
    x = 0
    while x < w:
        if streak[x]:
            run = x
            while run < w and streak[run]:
                run += 1
            if run - x > 3:
                streak[x:run] = False
            x = run
        else:
            x += 1
    idx = np.where(streak)[0]
    if len(idx) == 0 or len(idx) > w // 8:                  # none, or not streaky
        return img.convert("RGB")
    out = a.copy()
    for x in idx:
        l = x - 1
        while l >= 0 and streak[l]:
            l -= 1
        r = x + 1
        while r < w and streak[r]:
            r += 1
        if l >= 0 and r < w:
            out[:, x] = (a[:, l] + a[:, r]) / 2
        elif l >= 0:
            out[:, x] = a[:, l]
        elif r < w:
            out[:, x] = a[:, r]
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB")


def white_balance(img, carrier=None, amount=1.0):
    """Neutralise the carrier film's colour cast so reds read as red and whites
    as white (the light-blue film otherwise darkens/shifts every colour). Scales
    each channel so the carrier becomes neutral, then nudges it toward white."""
    rgb = img.convert("RGB")
    if carrier is None:
        carrier = detect_carrier(rgb)
    C = np.array(carrier, np.float32)
    C[C < 1] = 1
    target = float(C.mean())
    gain = 1.0 + amount * (target / C - 1.0)          # neutralise the tint
    a = np.asarray(rgb).astype(np.float32) * gain
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


def clean(img, denoise=2, sharpen=True, remove_lines=True, balance=True,
          carrier=None):
    """Knock back scanner/JPEG noise + paper texture, remove vertical scan
    streaks, neutralise the carrier colour cast, then re-crisp edges."""
    img = _auto_orient(img).convert("RGB")
    if balance:
        img = white_balance(img, carrier=carrier)
    if remove_lines:
        img = destripe(img)
    if denoise > 0:
        img = img.filter(ImageFilter.MedianFilter(size=3 if denoise < 3 else 5))
    if sharpen:
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=120,
                                                 threshold=3))
    return img


def remove_background(img, carrier=None, tol=52):
    """Return an RGBA with the carrier/background lifted to transparency,
    white ink preserved. carrier=None auto-detects; carrier='white' keys a
    plain white/neutral background."""
    rgb = img.convert("RGB")
    a = np.asarray(rgb)
    if carrier is None:
        carrier = detect_carrier(rgb)
    if carrier == "white" or carrier is None:
        carrier = (255, 255, 255)
    alpha = _carrier_alpha(a, carrier, tol=tol)
    out = np.dstack([a, alpha])
    return Image.fromarray(out, "RGBA")


def _box_mean(a, r):
    """Mean over a (2r+1) box for every pixel of 2D array a (integral image)."""
    H, W = a.shape
    ii = np.pad(a.astype(np.float64), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    y = np.arange(H); x = np.arange(W)
    y0 = np.clip(y - r, 0, H); y1 = np.clip(y + r + 1, 0, H)
    x0 = np.clip(x - r, 0, W); x1 = np.clip(x + r + 1, 0, W)
    A = ii[y1][:, x1]; B = ii[y0][:, x1]; C = ii[y1][:, x0]; D = ii[y0][:, x0]
    area = (y1 - y0)[:, None] * (x1 - x0)[None, :]
    return (A - B - C + D) / np.maximum(area, 1)


def smooth_flats(img, r=2, var_thresh=90, kernel=5):
    """Remove JPEG colour mottling INSIDE flat areas while keeping edges crisp
    and colours faithful. Median-smooth only LOW-variance (flat) regions; leave
    high-variance regions (edges, text, fine detail) exactly as scanned. The
    median of a flat region is its true colour, so nothing shifts — the blotchy
    patchiness just goes."""
    rgb = img.convert("RGB")
    a = np.asarray(rgb)
    gray = a.mean(2)
    var = _box_mean(gray * gray, r) - _box_mean(gray, r) ** 2
    flat = (var < var_thresh)[..., None]
    k = kernel if kernel % 2 else kernel + 1
    med = np.asarray(rgb.filter(ImageFilter.MedianFilter(k)))
    out = np.where(flat, med, a).astype(np.uint8)
    return Image.fromarray(out, "RGB")


def solidify_black(img, thresh=85, sat_max=40):
    """A bad scan turns solid black ink into patchy dark grey (JPEG mottling).
    Snap those near-black, near-neutral pixels to pure #000000 so black areas
    read as clean solid black. Only VERY dark + low-saturation pixels move —
    medium greys and dark colours (dark red, navy) are left untouched."""
    rgb = np.asarray(img.convert("RGB"))
    mx = rgb.max(2).astype(np.int16)
    mn = rgb.min(2).astype(np.int16)
    dark = (mx < thresh) & ((mx - mn) < sat_max)
    if not dark.any():
        return img.convert("RGB")
    out = rgb.copy()
    out[dark] = (0, 0, 0)
    return Image.fromarray(out, "RGB")


def clean_matte(rgba, alpha_floor=70, despeckle=3):
    """Tidy the transparency matte: drop the faint semi-transparent halo of
    carrier pixels hugging the art (the "noise around the images"), remove
    isolated opaque speckles in the background, and fill pinholes. Only the
    ALPHA changes — the art's RGB (and its fully-opaque interior) is untouched,
    so a pixel-faithful restoration stays faithful."""
    a = np.asarray(rgba).copy()
    alpha = a[..., 3]
    alpha[alpha < alpha_floor] = 0            # faint carrier halo/fringe -> gone
    if despeckle:
        k = despeckle if despeckle % 2 else despeckle + 1
        am = Image.fromarray(alpha)
        am = am.filter(ImageFilter.MinFilter(k)).filter(ImageFilter.MaxFilter(k))
        am = am.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k))
        alpha = np.asarray(am)
    # where alpha is now 0, zero the RGB too so no stray colour hides under it
    a[..., 3] = alpha
    return Image.fromarray(a, "RGBA")


def trim(rgba, pad=8):
    """Crop to the opaque content plus a small padding."""
    a = np.asarray(rgba)
    ys, xs = np.where(a[..., 3] > 12)
    if len(xs) == 0:
        return rgba
    x0, x1 = max(0, xs.min() - pad), min(a.shape[1], xs.max() + 1 + pad)
    y0, y1 = max(0, ys.min() - pad), min(a.shape[0], ys.max() + 1 + pad)
    return rgba.crop((x0, y0, x1, y1))


# ---------------------------------------------------------------- vector
def vectorize(rgba, target_px=2400, filter_speckle=12, color_precision=8,
              quantize_colors=16, presmooth=True, drop_halo=True):
    """Trace flat art to vectors and rasterise back to a crisp transparent PNG.
    Returns (svg_text, rgba_result). Needs vtracer + PyMuPDF.

    Quality for commercial flat art comes from feeding the tracer CLEAN, FLAT
    colours: a scan has JPEG mush and thousands of near-duplicate colours that
    trace into speckle. So we median-smooth, then quantise the opaque art to a
    small palette (solid fills), before tracing with a high speckle filter.

    quantize_colors=0 / presmooth=False trace the pixels EXACTLY as given (for
    art that is already flat, e.g. a palette-snapped AI redraw). drop_halo=False
    keeps light bluish fills — only right when the input has no carrier halo
    left (a hard alpha), since a pale blue decal colour looks like a halo."""
    import vtracer
    import pymupdf
    import tempfile
    KEY = (255, 0, 255)                 # magenta stand-in for transparent
    a = np.asarray(rgba)
    rgb = a[..., :3].astype(np.uint8).copy()
    transparent = a[..., 3] < 128
    art = Image.fromarray(rgb, "RGB")
    if presmooth:
        art = art.filter(ImageFilter.MedianFilter(3))
    if quantize_colors:
        art = art.quantize(colors=int(quantize_colors),
                           method=Image.MEDIANCUT, dither=Image.NONE
                           ).convert("RGB")
    rgb = np.asarray(art).astype(np.uint8).copy()
    rgb[transparent] = KEY              # re-mark transparent after quantise
    # vtracer's stacked mode paints the dominant colour as a full-canvas BASE
    # layer and the rest on top. On a tightly cropped decal the ink can be
    # the majority, so the base would be ink and dropping the key paths
    # would leave a solid rectangle. A key-coloured margin of 25% a side
    # (area >= 2x) makes the key the base every time; the margin is undone
    # below with a translate, so coordinates stay those of the input.
    H0, W0 = rgb.shape[:2]
    pad = int(round(0.25 * max(H0, W0))) + 4
    padded = np.empty((H0 + 2 * pad, W0 + 2 * pad, 3), np.uint8)
    padded[...] = KEY
    padded[pad:pad + H0, pad:pad + W0] = rgb
    rgb = padded
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.png"
        svg = Path(td) / "out.svg"
        Image.fromarray(rgb, "RGB").save(src)
        vtracer.convert_image_to_svg_py(
            str(src), str(svg), colormode="color", hierarchical="stacked",
            mode="spline", filter_speckle=int(filter_speckle),
            color_precision=int(color_precision), layer_difference=16,
            corner_threshold=60, length_threshold=4.0, splice_threshold=45,
            path_precision=8)
        svg_text = svg.read_text(encoding="utf-8", errors="replace")
        # DROP the magenta stand-in shapes so the background is truly empty —
        # then anti-aliased edges blend against transparency (no colour fringe),
        # and the SVG itself is clean/transparent for commercial use. The tracer
        # emits the key as a FAMILY of near-magenta shades (AA), plus a faint
        # blue-grey carrier halo, so match by colour, not one exact hex.
        import re

        def _drop(hexstr):
            try:
                h = hexstr.lstrip("#")
                r, g, b = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except (ValueError, IndexError):
                return False
            magenta = r > 165 and g < 115 and b > 165
            bright = (r + g + b) / 3
            halo = bright > 196 and (b - r) >= 6 and (g - r) >= -4 \
                and (max(r, g, b) - min(r, g, b)) < 42   # light, bluish, low-sat
            return magenta or (halo and drop_halo)

        def _clean_path(m):
            fill = re.search(r'fill="(#[0-9A-Fa-f]{6})"', m.group(0))
            return "" if (fill and _drop(fill.group(1))) else m.group(0)

        svg_clean = re.sub(r"<path\b[^>]*?/>", _clean_path, svg_text)
        # undo the margin: the page is the input's size again and the paths
        # are shifted back, so every coordinate matches the input pixels
        root = re.search(r"<svg\b[^>]*>", svg_clean)
        if root:
            tag = root.group(0)
            tag2 = re.sub(r'\swidth="[^"]*"', f' width="{W0}"', tag, count=1)
            tag2 = re.sub(r'\sheight="[^"]*"', f' height="{H0}"', tag2, count=1)
            body_end = svg_clean.rfind("</svg>")
            svg_clean = (svg_clean[:root.start()] + tag2
                         + f'<g transform="translate(-{pad} -{pad})">'
                         + svg_clean[root.end():body_end] + "</g>"
                         + svg_clean[body_end:])
        csvg = Path(td) / "clean.svg"
        csvg.write_text(svg_clean, encoding="utf-8")
        doc = pymupdf.open(str(csvg))
        page = doc[0]
        scale = target_px / max(1, page.rect.width)
        pm = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
        r = Image.frombytes("RGBA", (pm.width, pm.height), pm.samples)
    return svg_clean, r


# ---------------------------------------------------------------- SVG sizing
def svg_set_physical_size(svg_text, w_in, h_in):
    """Give an SVG a real-world size: width/height in inches (what printers
    and cutters read), keeping its pixel geometry as the viewBox. vtracer
    writes plain pixel width/height with no viewBox, so one is added."""
    import re
    m = re.search(r"<svg\b[^>]*>", svg_text)
    if not m:
        return svg_text
    tag = m.group(0)
    wm = re.search(r'\swidth="([0-9.]+)(?:px)?"', tag)
    hm = re.search(r'\sheight="([0-9.]+)(?:px)?"', tag)
    new = tag
    if "viewBox" not in tag and wm and hm:
        new = new[:-1] + f' viewBox="0 0 {wm.group(1)} {hm.group(1)}">'
    size = f' width="{w_in:.4f}in" height="{h_in:.4f}in"'
    if wm:
        new = re.sub(r'\swidth="[^"]*"', "", new, count=1)
    if hm:
        new = re.sub(r'\sheight="[^"]*"', "", new, count=1)
    new = new[:-1] + size + ">"
    return svg_text[:m.start()] + new + svg_text[m.end():]


def svg_inner(svg_text):
    """The drawing elements between <svg …> and </svg> (for nesting)."""
    import re
    m = re.search(r"<svg\b[^>]*>(.*)</svg>", svg_text, re.S)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------- segmentation
def _components(mask):
    """Bounding boxes (x0, y0, x1, y1 inclusive) of the 8-connected regions
    of a boolean 2-D mask. Run-based union-find in plain numpy/Python: a
    decal sheet has thousands of runs, not millions, so this is fast and
    needs no scipy."""
    H, W = mask.shape
    parent = []

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    runs = []
    prev = []
    for y in range(H):
        row = mask[y]
        if not row.any():
            prev = []
            continue
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        starts = np.where(d == 1)[0]
        ends = np.where(d == -1)[0]
        cur = []
        for x0, x1 in zip(starts, ends):
            rid = len(parent)
            parent.append(rid)
            for px0, px1, pid in prev:
                if px0 <= x1 and x0 <= px1:        # touching incl. diagonals
                    union(pid, rid)
            cur.append((int(x0), int(x1), rid))
            runs.append((y, int(x0), int(x1), rid))
        prev = cur
    boxes = {}
    for y, x0, x1, rid in runs:
        r = find(rid)
        b = boxes.get(r)
        if b is None:
            boxes[r] = [x0, y, x1 - 1, y]
        else:
            b[0] = min(b[0], x0)
            b[2] = max(b[2], x1 - 1)
            b[3] = max(b[3], y)
    return [tuple(b) for b in boxes.values()]


def segment_decals(rgba, gap=16, min_side=24, pad=6, down=4):
    """Split a cleaned sheet (RGBA, transparent background) into the boxes of
    its individual decals, in reading order (rows top→bottom, left→right).

    Opaque pixels are grouped into connected regions after bridging gaps of
    up to `gap` px (so a logo and the text under it stay one decal); specks
    smaller than `min_side` px are dropped; each box gets `pad` px around it.
    Segmentation runs on a `down`× reduced mask for speed, then the boxes are
    tightened on the full-resolution alpha."""
    a = np.asarray(rgba)
    if a.ndim != 3 or a.shape[2] != 4:
        return [(0, 0, rgba.width, rgba.height)]
    opaque = a[..., 3] > 96
    H, W = opaque.shape
    down = max(1, int(down))
    hh, ww = max(1, H // down), max(1, W // down)
    m = opaque[:hh * down, :ww * down].reshape(hh, down, ww, down).any(axis=(1, 3))
    r = max(1, int(round(gap / down)))
    md = np.asarray(Image.fromarray(m.astype(np.uint8) * 255)
                    .filter(ImageFilter.MaxFilter(2 * r + 1))) > 0
    out = []
    for x0, y0, x1, y1 in _components(md):
        X0, Y0 = x0 * down, y0 * down
        X1, Y1 = min(W, (x1 + 1) * down), min(H, (y1 + 1) * down)
        sub = opaque[Y0:Y1, X0:X1]
        if not sub.any():
            continue
        ys, xs = np.where(sub)
        bx0, bx1 = X0 + int(xs.min()), X0 + int(xs.max()) + 1
        by0, by1 = Y0 + int(ys.min()), Y0 + int(ys.max()) + 1
        if (bx1 - bx0) < min_side or (by1 - by0) < min_side:
            continue
        out.append((max(0, bx0 - pad), max(0, by0 - pad),
                    min(W, bx1 + pad), min(H, by1 + pad)))
    if not out:
        return []
    # reading order: group into rows by vertical overlap, then left→right
    out.sort(key=lambda b: (b[1], b[0]))
    rows, cur = [], [out[0]]
    for b in out[1:]:
        y0s = [c[1] for c in cur]
        y1s = [c[3] for c in cur]
        band_y0, band_y1 = min(y0s), max(y1s)
        overlap = min(band_y1, b[3]) - max(band_y0, b[1])
        if overlap > 0.4 * min(b[3] - b[1], band_y1 - band_y0):
            cur.append(b)
        else:
            rows.append(cur)
            cur = [b]
    rows.append(cur)
    ordered = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda b: b[0]))
    return ordered


# ---------------------------------------------------------------- palette
def palette_of(rgba, colors=16, merge=40, sample=200000, min_share=0.01,
               erode=2):
    """The decal's own flat colours: the opaque pixels of the SCAN, reduced
    to at most `colors` entries and near-duplicates (JPEG noise shades)
    merged. Returns an (N, 3) uint8 array, or None when nothing is opaque.

    The sample is taken `erode` px INSIDE the silhouette (when enough is
    left) and entries under `min_share` of the pixels are dropped: the
    blends where art meets carrier film at the edge are not decal colours,
    and letting them in gave the redraw a pink/grey fringe."""
    a = np.asarray(rgba)
    if a.ndim != 3 or a.shape[2] != 4:
        a = np.dstack([np.asarray(rgba.convert("RGB")),
                       np.full(rgba.size[::-1], 255, np.uint8)])
    opaque = a[..., 3] >= 250
    if erode:
        er = np.asarray(Image.fromarray(opaque.astype(np.uint8) * 255)
                        .filter(ImageFilter.MinFilter(2 * int(erode) + 1))) > 0
        if er.sum() >= 400:
            opaque = er
    px = a[opaque][:, :3]
    if len(px) == 0:
        return None
    if len(px) > sample:
        idx = np.linspace(0, len(px) - 1, sample).astype(np.int64)
        px = px[idx]
    strip = Image.fromarray(px.reshape(1, -1, 3).astype(np.uint8), "RGB")
    q = strip.quantize(colors=int(colors), method=Image.MEDIANCUT,
                       dither=Image.NONE)
    pal = np.array(q.getpalette()[:int(colors) * 3], np.int32).reshape(-1, 3)
    counts = np.bincount(np.asarray(q).ravel(), minlength=len(pal))
    total = max(1, int(counts.sum()))
    kept = []
    for i in np.argsort(-counts):
        if counts[i] == 0:
            continue
        if counts[i] < min_share * total and len(kept) >= 2:
            continue                      # an edge blend, not a decal colour
        c = pal[i]
        if int(c.min()) >= 215 and int(c.max() - c.min()) <= 20:
            # white ink reads as film-tinted light grey in a scan; printed,
            # that would be a faint grey where the paper should stay bare
            c = np.array([255, 255, 255], np.int32)
        if all(int(np.abs(c - k).sum()) > merge for k in kept):
            kept.append(c)
    return np.array(kept, np.uint8) if kept else None


def snap_palette(rgb_img, palette):
    """Replace every pixel by the nearest colour of `palette` — the redrawn
    art then uses EXACTLY the scan's colours (and traces as flat fills)."""
    a = np.asarray(rgb_img.convert("RGB")).astype(np.int32)
    H, W, _ = a.shape
    flat = a.reshape(-1, 3)
    P = np.asarray(palette, np.int32)
    out = np.empty_like(flat)
    step = 262144
    for s in range(0, len(flat), step):
        blk = flat[s:s + step]
        d = ((blk[:, None, :] - P[None, :, :]) ** 2).sum(2)
        out[s:s + step] = P[d.argmin(1)]
    return Image.fromarray(out.reshape(H, W, 3).astype(np.uint8), "RGB")


# ---------------------------------------------------------------- AI redraw
def redraw_sheet(rgba, refine, native_dpi=300, size_scale=1.0, target_dpi=300,
                 keep_palette=True, palette_colors=16, work_px=1024,
                 min_work_px=640, progress=None, cancelled=None, gap=16,
                 min_side=24):
    """AI-redraw every decal on a cleaned sheet and rebuild the sheet as
    vector art at its correct physical size.

    rgba       the faithful cleanup of the scan (transparent background), at
               the scan's native resolution
    refine     refine(rgb_image, (w, h)) -> rgb_image | None — the AI step. It
               gets one decal as an RGB picture on white at scan size plus the
               work-canvas size to draw at, and returns the redrawn picture
               (None = cancelled; any size is resized to the canvas).
    The scan supplies what the AI must not invent: each decal's SILHOUETTE
    (its alpha is cut from the scan) and, with keep_palette, its exact
    COLOURS (the redraw is snapped to the scan's own palette).

    Returns None when cancelled, else a dict:
      svg    the whole sheet as one SVG — every decal in its place, width/
             height in inches (size_scale applied), so it prints at size
      rgba   the sheet rasterised at target_dpi (transparent)
      items  one dict per decal: box (in scan px), svg, rgba, size_in"""
    boxes = segment_decals(rgba, gap=gap, min_side=min_side)
    if not boxes:
        return {"svg": None, "rgba": None, "items": []}
    W, H = rgba.size
    k = float(size_scale) * float(target_dpi) / float(native_dpi)  # scan px -> out px
    sheet_w_in = W / float(native_dpi) * size_scale
    sheet_h_in = H / float(native_dpi) * size_scale
    sheet = Image.new("RGBA", (max(1, int(round(W * k))),
                               max(1, int(round(H * k)))), (0, 0, 0, 0))
    parts, items = [], []
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        if cancelled and cancelled():
            return None
        if progress:
            progress(i, len(boxes))
        crop = rgba.crop((x0, y0, x1, y1))
        cw, ch = crop.size
        # the work canvas: about 4× the scan crop, clamped to [min_work_px,
        # work_px] on the long edge (diffusion models draw badly on tiny
        # canvases and invent on huge ones), both sides multiples of 8,
        # aspect kept. The enlargement itself happens in refine (engine-side
        # RealESRGAN when available), so the crop goes over at native size.
        long = float(max(cw, ch))
        s = min(float(work_px), max(float(min_work_px), long * 4.0)) / long
        wr = max(64, int(round(cw * s / 8)) * 8)
        hr = max(64, int(round(ch * s / 8)) * 8)
        on_white = Image.alpha_composite(
            Image.new("RGBA", crop.size, (255, 255, 255, 255)), crop).convert("RGB")
        out = refine(on_white, (wr, hr))
        if out is None:
            return None
        out = out.convert("RGB")
        if out.size != (wr, hr):
            out = out.resize((wr, hr), Image.LANCZOS)
        # the silhouette comes from the SCAN (hard alpha). Where the AI drew
        # its shape a little inside that outline, the gap holds the AI's
        # white background: in a band along the outline, drop pixels that
        # are background-coloured (far from any ink) so no fringe is traced.
        # White INK deeper inside the decal is untouched.
        sil = crop.split()[3].resize((wr, hr), Image.BILINEAR)
        sil = np.asarray(sil.point(lambda v: 255 if v >= 128 else 0)) > 0
        band_r = max(3, int(round(0.012 * max(wr, hr))))
        inner = np.asarray(Image.fromarray(sil.astype(np.uint8) * 255)
                           .filter(ImageFilter.MinFilter(2 * band_r + 1))) > 0
        # "light" = white-ish / light grey, i.e. the background or the faint
        # contour some models draw around a shape (a sticker-border look)
        def _light(arr):
            return (arr.mean(2) >= 200) & ((arr.max(2) - arr.min(2)) < 40)

        ai = np.asarray(out).astype(np.int32)
        light = _light(ai)
        pal = palette_of(crop, colors=palette_colors)
        if keep_palette and pal is not None:
            out = snap_palette(out, pal)
            light |= _light(np.asarray(out).astype(np.int32))
        keep = sil.copy()
        keep[(sil & ~inner) & light] = False
        redrawn = out.convert("RGBA")
        redrawn.putalpha(Image.fromarray((keep * 255).astype(np.uint8)))
        w_in = cw / float(native_dpi) * size_scale
        h_in = ch / float(native_dpi) * size_scale
        px_w = max(1, int(round(w_in * target_dpi)))
        px_h = max(1, int(round(h_in * target_dpi)))
        svg, ras = vectorize(redrawn, target_px=px_w, quantize_colors=0,
                             presmooth=False, drop_halo=False)
        svg = svg_set_physical_size(svg, w_in, h_in)
        if ras.size != (px_w, px_h):
            ras = ras.resize((px_w, px_h), Image.LANCZOS)
        items.append(dict(box=(x0, y0, x1, y1), svg=svg, rgba=ras,
                          size_in=(w_in, h_in)))
        px, py = int(round(x0 * k)), int(round(y0 * k))
        part = ras
        if px + part.width > sheet.width or py + part.height > sheet.height:
            part = part.crop((0, 0, max(1, min(part.width, sheet.width - px)),
                              max(1, min(part.height, sheet.height - py))))
        if px < sheet.width and py < sheet.height:
            sheet.alpha_composite(part, (px, py))
        parts.append(f'<g transform="translate({x0} {y0}) '
                     f'scale({cw / wr:.6f} {ch / hr:.6f})">'
                     f'{svg_inner(svg)}</g>')
    sheet_svg = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
                 f'width="{sheet_w_in:.4f}in" height="{sheet_h_in:.4f}in" '
                 f'viewBox="0 0 {W} {H}">\n' + "\n".join(parts) + "\n</svg>\n")
    return {"svg": sheet_svg, "rgba": sheet, "items": items}


# ---------------------------------------------------------------- pipeline
def process_image(img, mode="cleanup", remove_bg=True, carrier=None,
                  denoise=2, tol=52, target_dpi=600, native_dpi=300,
                  do_trim=False, size_scale=1.0, remove_lines=True,
                  balance=True, exact=False, tidy_matte=True, solidify=False,
                  smooth=False):
    """Run one image through the pipeline. Returns a dict with 'rgba' (and
    'svg' for vector mode). size_scale rescales the result for a different
    figure scale (e.g. 1.5 to take a 3.75\" decal to 1/12 Classified).

    exact=True is the pixel-faithful path: the art's RGB is left BYTE-IDENTICAL
    to the scan (no denoise, no sharpen, no colour change) — only the alpha
    (background) is computed, and the only RGB change allowed is removing the
    scanner streak lines when remove_lines is on. Use it when fidelity must be
    perfect."""
    orig = _auto_orient(img).convert("RGB")
    if carrier is None:
        carrier = detect_carrier(orig)
    if exact:
        cleaned = destripe(orig) if remove_lines else orig
    else:
        cleaned = clean(img, denoise=denoise, remove_lines=remove_lines,
                        balance=balance, carrier=carrier)
    if smooth:
        cleaned = smooth_flats(cleaned)     # de-mottle flat areas, keep edges
    if solidify:
        cleaned = solidify_black(cleaned)   # patchy dark-grey -> solid black
    if remove_bg:
        # the transparency matte is computed from the ORIGINAL tinted image and
        # applied to the (possibly colour-processed) RGB — otherwise white-balance
        # erases the very tint the keyer uses to find the carrier film
        alpha = _carrier_alpha(np.asarray(orig), carrier, tol=tol)
        if is_neutral_carrier(carrier):
            # white paper: the faint grey shadows along the cut edges of
            # white stickers survive the distance key but are not art —
            # light AND colourless pixels go with the background
            o = np.asarray(orig).astype(np.int32)
            faint = (o.mean(2) > 200) & ((o.max(2) - o.min(2)) < 30)
            alpha = np.where(faint, 0, alpha).astype(np.uint8)
        rgba = Image.fromarray(
            np.dstack([np.asarray(cleaned.convert("RGB")), alpha]), "RGBA")
        if tidy_matte:
            rgba = clean_matte(rgba)   # drop the faint carrier halo + speckle
    else:
        rgba = cleaned.convert("RGBA")
    if do_trim:
        rgba = trim(rgba)
    out = {"svg": None}
    if mode == "vector":
        # target pixels from the requested print DPI vs the scan's native DPI
        scale = max(1.0, float(target_dpi) / max(72, native_dpi))
        target_px = int(rgba.width * scale)
        pw, ph = rgba.size
        out["svg"], rgba = vectorize(rgba, target_px=target_px)
        # the SVG states its printed size (scan pixels / scan DPI, times the
        # figure-scale conversion), so it opens and prints at size
        ss = size_scale if size_scale and size_scale > 0 else 1.0
        out["svg"] = svg_set_physical_size(
            out["svg"], pw / float(max(72, native_dpi)) * ss,
            ph / float(max(72, native_dpi)) * ss)
    else:  # cleanup — upscale the raster to the target DPI if asked
        if target_dpi and target_dpi > native_dpi:
            f = target_dpi / native_dpi
            rgba = rgba.resize((int(rgba.width * f), int(rgba.height * f)),
                               Image.LANCZOS)
    # figure-scale conversion: enlarge/shrink the whole decal for a different
    # scale (e.g. 3.75" -> 1/12 Classified = 1.5x)
    if size_scale and abs(size_scale - 1.0) > 1e-3:
        w = max(1, int(round(rgba.width * size_scale)))
        h = max(1, int(round(rgba.height * size_scale)))
        rgba = rgba.resize((w, h), Image.LANCZOS)
    out["rgba"] = rgba
    return out


def process_source(path, out_dir, mode="cleanup", separate=False, **opts):
    """Process every page of a source file and save results. Returns a list of
    saved file paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for label, img in iter_source_images(path):
        res = process_image(img, mode=mode, **opts)
        png = out_dir / f"{label}.png"
        res["rgba"].save(png)
        saved.append(str(png))
        if res.get("svg"):
            svg = out_dir / f"{label}.svg"
            svg.write_text(res["svg"], encoding="utf-8")
            saved.append(str(svg))
    return saved


def main(argv=None):
    """CLI: decals.py <src> <out_dir> [--mode cleanup|vector] [--keep-bg]
    [--separate] [--dpi N] [--native N] [--tol N] [--trim]"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print("usage: decals.py <src> <out_dir> [--mode m] [opts]")
        return 2
    src, out_dir = argv[0], argv[1]
    opts = dict(mode="cleanup", remove_bg=True, denoise=2, tol=52,
                target_dpi=600, native_dpi=300, do_trim=False, separate=False)
    i = 2
    while i < len(argv):
        a = argv[i]
        if a == "--mode":
            opts["mode"] = argv[i + 1]; i += 2
        elif a == "--keep-bg":
            opts["remove_bg"] = False; i += 1
        elif a == "--separate":
            opts["separate"] = True; i += 1
        elif a == "--trim":
            opts["do_trim"] = True; i += 1
        elif a == "--dpi":
            opts["target_dpi"] = int(argv[i + 1]); i += 2
        elif a == "--native":
            opts["native_dpi"] = int(argv[i + 1]); i += 2
        elif a == "--tol":
            opts["tol"] = int(argv[i + 1]); i += 2
        else:
            i += 1
    saved = process_source(src, out_dir, **opts)
    print(json.dumps({"saved": saved}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
