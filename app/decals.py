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


def _carrier_alpha(a_rgb, carrier, tol=52, soft=18):
    """Alpha (0=carrier/transparent, 255=keep) that removes the carrier colour
    while KEEPING white ink and coloured art.

    White ink sits close to a light carrier in plain distance, so we key on the
    carrier's TINT direction: the carrier is a light, faintly-tinted colour, so
    a pixel is carrier only when it is light AND shares that tint. Neutral white
    (no tint) and saturated art (different tint) are kept."""
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
              quantize_colors=16, presmooth=True):
    """Trace flat art to vectors and rasterise back to a crisp transparent PNG.
    Returns (svg_text, rgba_result). Needs vtracer + PyMuPDF.

    Quality for commercial flat art comes from feeding the tracer CLEAN, FLAT
    colours: a scan has JPEG mush and thousands of near-duplicate colours that
    trace into speckle. So we median-smooth, then quantise the opaque art to a
    small palette (solid fills), before tracing with a high speckle filter."""
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
            return magenta or halo

        def _clean_path(m):
            fill = re.search(r'fill="(#[0-9A-Fa-f]{6})"', m.group(0))
            return "" if (fill and _drop(fill.group(1))) else m.group(0)

        svg_clean = re.sub(r"<path\b[^>]*?/>", _clean_path, svg_text)
        csvg = Path(td) / "clean.svg"
        csvg.write_text(svg_clean, encoding="utf-8")
        doc = pymupdf.open(str(csvg))
        page = doc[0]
        scale = target_px / max(1, page.rect.width)
        pm = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=True)
        r = Image.frombytes("RGBA", (pm.width, pm.height), pm.samples)
    return svg_clean, r


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
        out["svg"], rgba = vectorize(rgba, target_px=target_px)
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
