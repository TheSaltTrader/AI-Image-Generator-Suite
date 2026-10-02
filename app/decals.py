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
import re
import warnings
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
def iter_sources(path):
    """Yield (label, PIL.Image RGB, dpi) for every page/image in a source
    file — the resolution comes from the FILE, never from a setting: a PDF
    page says how big its scanned picture is drawn (pixels / inches), an
    image file carries a DPI tag (a 72/96 placeholder counts as unknown),
    and a photo has none (dpi=None). Supports PDF (each page's embedded
    image at its native pixels) and ordinary raster formats."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in PDF_EXTS:
        import pymupdf  # noqa
        doc = pymupdf.open(str(path))
        for pno in range(doc.page_count):
            page = doc[pno]
            imgs = page.get_images(full=True)
            got, dpi = None, None
            if imgs:
                # the largest embedded image at native pixels (scanners embed
                # one full-page JPEG); fall back to rendering the page
                best = max(imgs, key=lambda im: doc.extract_image(im[0])["width"]
                           * doc.extract_image(im[0])["height"])
                info = doc.extract_image(best[0])
                got = Image.open(io.BytesIO(info["image"])).convert("RGB")
                try:
                    rects = page.get_image_rects(best[0])
                    if rects and rects[0].width > 0:
                        dpi = int(round(info["width"] / (rects[0].width / 72.0)))
                except Exception:
                    dpi = None
            if got is None:
                pm = page.get_pixmap(dpi=300)
                got = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
                dpi = 300
            label = path.stem if doc.page_count == 1 else f"{path.stem}_p{pno + 1}"
            yield label, got, dpi
        doc.close()
    else:
        im = Image.open(path)
        dpi = None
        try:
            d = im.info.get("dpi")
            if d and float(d[0]) > 96:          # 72/96 = nobody set it
                dpi = int(round(float(d[0])))
        except Exception:
            dpi = None
        yield path.stem, im.convert("RGB"), dpi


def parse_pages(text):
    """'all' / '' → None (every page); '2' → {2}; '1,3' → {1, 3};
    '2-4' → {2, 3, 4}; mixes like '1, 3-5' work. Page numbers start at 1.
    Raises ValueError on anything else, so the user is told."""
    t = (text or "").strip().lower()
    if t in ("", "all", "*"):
        return None
    out = set()
    for part in t.replace(";", ",").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            if a < 1 or b < a:
                raise ValueError(part)
            out.update(range(a, b + 1))
        else:
            n = int(part)
            if n < 1:
                raise ValueError(part)
            out.add(n)
    if not out:
        return None
    return out


def iter_source_images(path):
    """(label, image) pairs — iter_sources without the resolution."""
    for label, img, _dpi in iter_sources(path):
        yield label, img


# ---------------------------------------------------------------- photos
def looks_like_photo(img):
    """True when the picture's border is NOT the sheet: a scan's border is
    the carrier/paper (bright, near-neutral); a photo's border is a table,
    a cloth, a floor (dark or coloured)."""
    c = detect_carrier(img)
    gray = sum(c) / 3.0
    chroma = max(c) - min(c)
    return gray < 150 or chroma > 60


def find_sheet(img, down=4, min_frac=0.15):
    """Corners of the sheet in a photo — (TL, TR, BR, BL) in pixel
    coordinates — found as the largest region that differs from the
    border colour (the table), i.e. the sheet with its stickers. None when
    no such region covers at least `min_frac` of the picture."""
    rgb = img.convert("RGB")
    carrier = detect_carrier(rgb)
    small = rgb.resize((max(1, rgb.width // down), max(1, rgb.height // down)),
                       Image.BILINEAR)
    a = np.asarray(small).astype(np.int32)
    dist = np.sqrt(((a - np.array(carrier, np.int32)) ** 2).sum(2))
    mask = dist > 70
    # close small gaps (sticker edges, texture) so the sheet is one region
    mask = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255)
                      .filter(ImageFilter.MaxFilter(5))
                      .filter(ImageFilter.MinFilter(5))) > 0
    comps = _components(mask)
    if not comps:
        return None
    H, W = mask.shape
    best, best_area = None, 0
    for (x0, y0, x1, y1) in comps:
        area = (x1 - x0 + 1) * (y1 - y0 + 1)
        if area > best_area:
            best, best_area = (x0, y0, x1, y1), area
    if best_area < min_frac * H * W:
        return None
    x0, y0, x1, y1 = best
    sub = mask[y0:y1 + 1, x0:x1 + 1]
    ys, xs = np.where(sub)
    xs = xs + x0
    ys = ys + y0
    # the four extreme points of the region = the sheet's corners
    s = xs + ys
    d = xs - ys
    tl = (xs[s.argmin()], ys[s.argmin()])
    br = (xs[s.argmax()], ys[s.argmax()])
    tr = (xs[d.argmax()], ys[d.argmax()])
    bl = (xs[d.argmin()], ys[d.argmin()])
    f = float(down)
    return tuple((int(round(x * f)), int(round(y * f))) for x, y in (tl, tr, br, bl))


def straighten(img, corners, inset=0.01):
    """Perspective-correct the sheet between `corners` (TL, TR, BR, BL) to
    an axis-aligned picture of the sheet alone. `inset` pulls each corner
    toward the centre by that share of the sheet, so the sliver of table
    the corner search leaves along an edge is not carried over."""
    pts = [tuple(float(v) for v in c) for c in corners]
    cx = sum(p[0] for p in pts) / 4.0
    cy = sum(p[1] for p in pts) / 4.0
    pts = [(x + (cx - x) * inset, y + (cy - y) * inset) for x, y in pts]
    (tlx, tly), (trx, try_), (brx, bry), (blx, bly) = pts

    def dist(a, b):
        return float(np.hypot(a[0] - b[0], a[1] - b[1]))

    w = max(8, int(round((dist((tlx, tly), (trx, try_))
                          + dist((blx, bly), (brx, bry))) / 2.0)))
    h = max(8, int(round((dist((tlx, tly), (blx, bly))
                          + dist((trx, try_), (brx, bry))) / 2.0)))
    # PIL's QUAD maps (upper-left, lower-left, lower-right, upper-right)
    return img.convert("RGB").transform(
        (w, h), Image.QUAD,
        (tlx, tly, blx, bly, brx, bry, trx, try_), Image.BICUBIC)


PHOTO_WHITE = (250, 250, 250)


def _sheet_mode(a):
    """The brightest colour that covers at least 2% of picture `a`
    (float HxWx3) — on a photographed sheet, the sheet itself. None when
    nothing covers that much."""
    q = (a // 16).astype(np.int32)
    idx = q[..., 0] * 256 + q[..., 1] * 16 + q[..., 2]
    cnt = np.bincount(idx.ravel(), minlength=4096) / float(idx.size)
    sig = np.nonzero(cnt >= 0.02)[0]
    if sig.size == 0:
        return None
    bright = max(sig, key=lambda i: (i // 256) + (i // 16) % 16 + i % 16)
    return np.array([(bright // 256) * 16 + 8, ((bright // 16) % 16) * 16 + 8,
                     (bright % 16) * 16 + 8], np.float32)


def normalize_photo(img, white=250):
    """Flatten a photographed sheet's lighting and set the sheet to white,
    so the picture can be keyed like a scan on white paper. The sheet
    colour is the brightest colour that covers a fair share of the picture;
    the illumination field is the block-wise mean of sheet-coloured pixels
    (holes where decals lie are filled from the lit sheet around them),
    and every pixel is scaled by white / field — which also removes the
    light's colour cast. Returns (image, sheet_colour) — the input and None
    when no sheet colour stands out."""
    rgb = img.convert("RGB")
    a = np.asarray(rgb).astype(np.float32)
    H, W = a.shape[:2]
    mode = _sheet_mode(a)
    if mode is None:
        return rgb, None
    gray = a.mean(2)
    tint = a - gray[..., None]
    tmode = mode - mode.mean()
    cand = ((np.abs(tint - tmode).sum(2) < 36) & (gray > mode.mean() - 50)
            & (gray < mode.mean() + 60))
    s = max(16, min(H, W) // 24)
    bh, bw = max(1, H // s), max(1, W // s)
    field = np.full((bh, bw, 3), np.nan, np.float32)
    for by in range(bh):
        for bx in range(bw):
            y0, x0 = by * s, bx * s
            y1 = H if by == bh - 1 else y0 + s
            x1 = W if bx == bw - 1 else x0 + s
            m = cand[y0:y1, x0:x1]
            if m.mean() >= 0.1:
                field[by, bx] = a[y0:y1, x0:x1][m].mean(0)
    # fill the holes (decals) from the nearest lit sheet around them
    for _ in range(bh + bw):
        nan = np.isnan(field[..., 0])
        if not nan.any():
            break
        padded = np.pad(field, ((1, 1), (1, 1), (0, 0)), constant_values=np.nan)
        neigh = np.stack([padded[1:-1, :-2], padded[1:-1, 2:],
                          padded[:-2, 1:-1], padded[2:, 1:-1]])
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            fill = np.nanmean(neigh, axis=0)
        field = np.where(nan[..., None], fill, field)
    field[np.isnan(field[..., 0])] = mode
    for _ in range(2):                       # a little smoothing
        padded = np.pad(field, ((1, 1), (1, 1), (0, 0)), mode="edge")
        field = (padded[1:-1, 1:-1] * 4 + padded[1:-1, :-2] + padded[1:-1, 2:]
                 + padded[:-2, 1:-1] + padded[2:, 1:-1]) / 8.0
    fimg = Image.fromarray(np.clip(field, 1, 255).astype(np.uint8), "RGB")
    f = np.asarray(fimg.resize((W, H), Image.BILINEAR)).astype(np.float32)
    out = np.clip(a * (float(white) / np.maximum(f, 1.0)), 0, 255)
    return Image.fromarray(out.astype(np.uint8), "RGB"), tuple(int(v) for v in mode)


def prepare_photo(img, rotate=0, auto_crop=True, normalize=True):
    """A photographed sheet made scan-like: the sheet found and straightened
    (when the border is not the sheet), its lighting flattened and the sheet
    set to white, then rotated by `rotate` degrees (0/90/180/270, counter-
    clockwise like PIL) so the text reads upright. Returns (image, note) —
    note says what happened ('' for a scan)."""
    note = ""
    out = img
    if auto_crop and looks_like_photo(img):
        corners = find_sheet(img)
        if corners is not None:
            out = straighten(img, corners)
            note = (f"photo: cropped to the sheet ({out.width}×{out.height} px)")
            W, H = img.size
            edge = any(x <= 0.01 * W or x >= 0.99 * W or y <= 0.01 * H
                       or y >= 0.99 * H for x, y in corners)
            if edge:
                note += ("; the sheet runs to the picture's edge — shoot with "
                         "table showing all round it")
        else:
            note = "photo: no sheet found — using the whole picture"
        # normalize=False when the user named the paper colour to key:
        # flattening would turn that paper white
        out, sheet = normalize_photo(out) if normalize else (out, None)
        if sheet is not None:
            note += "; lighting flattened, sheet set to white"
    r = int(rotate) % 360
    if r:
        out = out.rotate(r, expand=True)
        note = (note + "; " if note else "") + f"rotated {r}°"
    return out, note


def _blockiness(gray):
    """JPEG block energy: differences across 8-px column boundaries vs
    differences everywhere; ~1.0 for a clean picture, >1.3 heavily
    compressed (a phone photo sent through a messaging app)."""
    d = np.abs(np.diff(gray, axis=1))
    if d.shape[1] < 16:
        return 1.0
    at = float(d[:, 7::8].mean())
    everywhere = float(d.mean())
    return at / max(1e-6, everywhere)


def assess_source(img, native_dpi=300, gap_px=None, raw=None, kind=None,
                  photo=False, orientation=None, dpi_known=True, carrier=None):
    """Judge a page BEFORE converting: what it is (scan or photo), how
    much detail it holds, how clean, and how each redraw method is likely
    to fare — with a plain recommendation. `img` is the prepared page
    (a photo already cropped, flattened and rotated); `raw` the untouched
    file picture, used for the compression measure (JPEG blocks only line
    up on the original pixels); `orientation` the rotation applied (None =
    not known). Returns a dict:
      kind, size, dpi, letters_px, sharpness, blockiness, light_std,
      neutral, opaque_share, decals, scores {trace, vision, reimagine}
      (0..1 = share of decals expected usable), report (text lines)."""
    rgb = img.convert("RGB")
    W, H = rgb.size
    if kind is None:
        kind = "photo" if (photo or looks_like_photo(rgb)) else "scan"
    photo = photo or kind == "photo"
    paper = carrier                         # a colour the user named
    if carrier is None:
        carrier = PHOTO_WHITE if photo else detect_carrier(rgb)
    neutral = is_neutral_carrier(carrier)
    g = np.asarray(rgb.convert("L")).astype(np.float32)
    lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1]
           + g[1:-1, :-2] + g[1:-1, 2:])
    sharpness = float(lap.var())
    graw = (np.asarray(raw.convert("L")).astype(np.float32)
            if raw is not None else g)
    block = _blockiness(graw)
    if photo and raw is not None:
        # the flattening multiplied the picture by white / sheet brightness,
        # and edge energy by its square: undo that so a dim, soft photo is
        # not called sharp
        mode = _sheet_mode(np.asarray(raw.convert("RGB")).astype(np.float32))
        if mode is not None:
            gain = 250.0 / max(40.0, float(mode.mean()))
            sharpness /= gain * gain
    a = np.asarray(rgb).astype(np.int32)
    dist = np.sqrt(((a - np.array(carrier, np.int32)) ** 2).sum(2))
    bg = dist < 52
    light_std = float(g[bg].std()) if bg.mean() > 0.05 else 0.0
    if gap_px is None:
        gap_px = max(4, int(round(native_dpi / 25.4)))      # 1 mm
    res = process_image(rgb, mode="cleanup", remove_bg=True, denoise=0,
                        tol=52, target_dpi=native_dpi, native_dpi=native_dpi,
                        exact=True, tidy_matte=True, photo=photo,
                        carrier=paper)
    rgba = res["rgba"]
    al = np.asarray(rgba)[..., 3] > 96
    opaque_share = float(al.mean())
    boxes = segment_decals(rgba, gap=gap_px)
    pieces = segment_decals(rgba, gap=0, min_side=6, pad=0)
    # letter-like marks: small pieces that are neither specks nor whole
    # decals — their median size is how big the text is on this page
    letterish = []
    for (x0, y0, x1, y1) in pieces:
        w, h = x1 - x0, y1 - y0
        small, big = min(w, h), max(w, h)
        if 9 <= small and big <= 90 and big <= 4 * small:
            letterish.append(small)
    letters_px = float(np.median(letterish)) if len(letterish) >= 5 else None
    # --- scores: what share of the decals should come out usable ---------
    # (calibrated on the user's 300 dpi film scans: letters ≈ 20 px,
    #  sharpness ≈ 250, JPEG-in-PDF blockiness ≈ 1.35)
    def ramp(v, lo, hi):
        if v is None:
            return 0.5
        return float(min(1.0, max(0.0, (v - lo) / float(hi - lo))))

    detail = ramp(letters_px, 12.0, 55.0)          # letter size in px
    focus = ramp(sharpness, 40.0, 300.0)           # per-pixel edge energy
    clean = 1.0 - ramp(block, 1.4, 1.9)            # compression
    even = 1.0 - ramp(light_std, 8.0, 30.0)        # lighting
    sep = 1.0 if 0.02 <= opaque_share <= 0.6 else 0.35
    trace = 0.15 + 0.85 * (0.45 * detail + 0.25 * focus + 0.15 * clean
                           + 0.15 * even) * sep
    vision = 0.10 + 0.90 * (0.55 * detail + 0.20 * focus + 0.10 * clean
                            + 0.15 * even) * sep
    reimagine = 0.05 + 0.6 * (0.3 * detail + 0.4 * focus + 0.3 * even) * sep
    scores = {"trace": round(trace, 2), "vision": round(vision, 2),
              "reimagine": round(reimagine, 2)}
    # --- the report ------------------------------------------------------
    lines = []
    mm_w = W / float(native_dpi) * 25.4
    lines.append(f"{kind.upper()}: {W}×{H} px at {native_dpi} dpi "
                 + (f"(≈ {mm_w:.0f} mm wide)" if dpi_known else
                    f"(assumed — ≈ {mm_w:.0f} mm wide if so)"))
    if orientation is None:
        lines.append("Orientation: not checked (no vision key) — the page "
                     "is used as it is")
    elif orientation:
        lines.append(f"Orientation: text was not upright — the page is "
                     f"turned {orientation}° (vision model)")
    else:
        lines.append("Orientation: upright (vision model)")
    if letters_px is None:
        lines.append("Detail: too few marks to judge letter size")
    else:
        mm = letters_px / float(native_dpi) * 25.4
        lines.append(f"Detail: small marks ≈ {letters_px:.0f} px "
                     f"({mm:.1f} mm) "
                     + ("— fine for text" if letters_px >= 45 else
                        "— marginal for text" if letters_px >= 28 else
                        "— too small for text (letters will garble)"))
    lines.append(f"Sharpness: {sharpness:.0f} "
                 + ("(sharp)" if sharpness >= 200 else
                    "(soft)" if sharpness >= 80 else "(blurred)"))
    if block >= 1.6:
        lines.append(f"Compression: heavy JPEG blocking ({block:.2f}) — "
                     "transfer the picture at full quality")
    if paper is not None:
        lines.append("Backing: the paper colour you picked (#%02x%02x%02x) "
                     "is keyed to transparent" % tuple(int(v) for v in paper))
    if photo:
        lines.append("Lighting: " + ("even" if light_std < 10 else
                                     "uneven" if light_std < 25 else
                                     "glare / strong gradient — the "
                                     "background cannot be keyed cleanly")
                     + f" (σ {light_std:.0f}, after flattening)")
        if paper is None:
            lines.append("Backing: a photographed sheet is keyed as white — "
                         "white-ink decals cannot be separated and are left out")
    elif paper is None:
        lines.append("Backing: " + ("white paper — white-ink decals cannot be "
                                    "separated and are left out" if neutral else
                                    "tinted film — white ink is kept"))
    if opaque_share > 0.6:
        lines.append(f"Background removal kept {opaque_share:.0%} of the page "
                     "— the backing is not being recognised (crop the "
                     "picture to the sheet, or raise the sensitivity)")
    lines.append(f"Decals found: {len(boxes)}")
    lines.append("Expected usable: clean trace ≈ {:.0%}, vision model ≈ {:.0%}, "
                 "re-imagine ≈ {:.0%}".format(trace, vision, reimagine))
    rec = []
    if letters_px is not None and letters_px < 45:
        want = int(round(native_dpi * 50.0 / max(letters_px, 1.0)))
        want = min(2400, max(600, int(round(want / 300.0)) * 300))
        if photo:
            rec.append("photograph each decal close up (fill the frame) "
                       "or scan the sheet at %d dpi for readable text" % want)
        else:
            rec.append(f"rescan at {want} dpi for readable text")
    if photo and (light_std >= 25 or block >= 1.6):
        rec.append("retake square-on in even light, full-size transfer")
    if sharpness < 80:
        rec.append("the picture is blurred — refocus / rescan")
    if photo and not dpi_known:
        rec.append("type the sheet's real width (mm) so printed sizes are right")
    if opaque_share > 0.6:
        rec.append("crop the picture to the sheet")
    if not rec:
        rec.append("good to go — run Preview one decal first")
    lines.append("Recommendation: " + "; ".join(rec))
    return {"kind": kind, "size": (W, H), "dpi": native_dpi,
            "letters_px": letters_px, "sharpness": sharpness,
            "blockiness": block, "light_std": light_std, "neutral": neutral,
            "opaque_share": opaque_share, "decals": len(boxes),
            "orientation": orientation, "scores": scores, "report": lines}


def dpi_from_width(px_width, width_mm):
    """Pixels per inch of a picture whose real width is width_mm."""
    try:
        mm = float(width_mm)
        return (float(px_width) / (mm / 25.4)) if mm > 0 else None
    except (TypeError, ValueError):
        return None


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
        if ntC >= 12:
            # white ink on a tinted film still picks up some of the film's
            # tint in the scan — (225,244,238) on a (223,254,255) film — so
            # a fixed "no tint" cut keys it away. Relative to the film it
            # is clear: the film reads 1.0 on its own tint (5th pct 0.84),
            # white ink 0.3-0.45 (it covers the film). Smoothed over 3x3
            # against scan noise; an opening drops 1-px leftovers.
            # (v2.22.2 — the whale sheets)
            # direction matters: film blended with a coloured edge also loses
            # film tint, but turns toward that colour — white ink keeps the
            # film's direction (cosine ~0.99) at 0.3-0.45 of its size
            pb = _box_mean(proj.astype(np.float32), 1)
            tn = np.sqrt((tintP * tintP).sum(2))
            cos = (tintP * tintC).sum(2) / (tn * ntC + 1e-6)
            cb = _box_mean(cos.astype(np.float32), 1)
            tb = _box_mean((tn / ntC).astype(np.float32), 1)
            rel = (light > grayC - 12) & (((pb < 0.7) & (cb > 0.9)) | (tb < 0.25))
            rel_img = Image.fromarray((rel * 255).astype(np.uint8))
            rel = np.asarray(rel_img.filter(ImageFilter.MinFilter(3))
                             .filter(ImageFilter.MaxFilter(3))) > 0
            # a thin light band hugging DARK ink is the blend where black
            # letters meet the film (less film tint, so it passed as white):
            # it drew a white rim round the sheet's black lettering
            # (v2.24.3). Thicker white survives — its core is kept.
            dark = light < 90
            core = erode_mask(rel, 2)
            rim = rel & ~dilate_mask(core, 2) & dilate_mask(dark, 2)
            rel = rel & ~rim
            is_white = is_white | rel
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
              quantize_colors=16, presmooth=True, drop_halo=True,
              hierarchical="stacked"):
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
            str(src), str(svg), colormode="color", hierarchical=hierarchical,
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

        Hp, Wp = rgb.shape[:2]
        seen = {"first": True}

        def _base_layer(tag):
            # vtracer's stacked mode starts with ONE path covering the whole
            # (padded) canvas: the background. On big canvases its colour
            # comes out averaged (#730073, not magenta), so it is recognised
            # by being first and spanning the canvas, not by colour.
            if not seen["first"]:
                return False
            seen["first"] = False
            d = re.search(r'd="M0 0 C([^"]*)"', tag)
            if not d:
                return False
            nums = [float(v) for v in
                    re.findall(r"-?\d+(?:\.\d+)?", d.group(1)[:400])]
            return bool(nums) and max(nums) >= 0.95 * max(Wp, Hp)

        def _clean_path(m):
            if _base_layer(m.group(0)):
                return ""
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


def _label_runs(mask, diag=True):
    """Label the connected regions of a boolean mask by runs (union-find,
    like _components) and return (labels int32 array with 0 = background,
    {label: [x0, y0, x1, y1, area]}). diag=True joins diagonal neighbours
    (ink); diag=False joins only edge neighbours (background), so a thin
    diagonal line of ink still encloses what lies inside it."""
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
        ends = np.where(d == -1)[0]          # half-open: [x0, x1)
        cur = []
        for x0, x1 in zip(starts, ends):
            rid = len(parent)
            parent.append(rid)
            for px0, px1, pid in prev:
                if diag:
                    if px0 <= x1 and x0 <= px1:
                        union(pid, rid)
                elif px0 < x1 and x0 < px1:
                    union(pid, rid)
            cur.append((int(x0), int(x1), rid))
            runs.append((y, int(x0), int(x1), rid))
        prev = cur
    labels = np.zeros((H, W), np.int32)
    ids = {}
    stats = {}
    for y, x0, x1, rid in runs:
        r = find(rid)
        lab = ids.get(r)
        if lab is None:
            lab = len(ids) + 1
            ids[r] = lab
            stats[lab] = [x0, y, x1 - 1, y, 0]
        labels[y, x0:x1] = lab
        st = stats[lab]
        st[0] = min(st[0], x0)
        st[2] = max(st[2], x1 - 1)
        st[3] = max(st[3], y)
        st[4] += x1 - x0
    return labels, stats


def fill_enclosed_holes(rgba, min_side=14, min_area=120, carrier=None,
                        letter_max_px=None):
    """Turn transparent regions that lie INSIDE a decal — not joined to the
    outside — opaque white: the window of a gauge, the digits and dashes
    on a black block, the centre of a ring. On a white-keyed sheet (white
    paper, a photo) those were white ink or white backing, keyed away
    with the background. A hole below `min_side` px on its short side or
    `min_area` px² is left alone (speckle), and so is a letter's counter
    (a hole a quarter or more of its host piece's height).
    With `carrier` (a TINTED film), a hole is filled only when it is white
    ink: its average colour shows clearly less of the film's tint than the
    film itself (under 0.9 of it) — a clear window shows the film at full
    tint (1.0) and stays clear; white ink covering the film reads 0.6-0.85.
    Returns (rgba, holes_filled)."""
    a = np.asarray(rgba).copy()
    tC = n2 = None
    if carrier is not None:
        C = np.array(carrier, np.float32)
        tC = C - C.mean()
        n2 = float((tC * tC).sum())
        if n2 < 144:                    # barely tinted: no reliable test
            tC = None
    clear = a[..., 3] <= 96
    H, W = clear.shape
    labels, stats = _label_runs(clear, diag=False)
    if not stats:
        return rgba, 0
    edge = set(np.unique(np.concatenate([
        labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]])).tolist())
    # the opaque pieces, to know what each hole sits inside: a letter's
    # counter sits in a piece hardly bigger than itself; a gauge's window
    # sits in a block many times its size
    ink_labels, ink_stats = _label_runs(~clear, diag=True)
    n = 0
    for lab, (x0, y0, x1, y1, area) in stats.items():
        if lab in edge:
            continue
        hw, hh = x1 - x0 + 1, y1 - y0 + 1
        if min(hw, hh) < min_side or area < min_area:
            continue
        # the piece just left of the hole's top-left run encloses it
        row = labels[y0]
        xs = np.nonzero(row[x0:x1 + 1] == lab)[0]
        lx = x0 + int(xs[0]) - 1 if xs.size else x0 - 1
        host = ink_labels[y0, lx] if lx >= 0 else 0
        if host:
            px0, py0, px1, py1, _pa = ink_stats[host]
            pw, ph = px1 - px0 + 1, py1 - py0 + 1
            # a letter's counter stands at least a quarter of the letter's
            # height (A, R, e: ~0.3; D, B: ~0.45; O: ~0.6); touching bold
            # letters make the host a whole word, so judge by height only.
            # The digits, dashes and discs inside a printed block are a few
            # percent of it and come back.
            # judged against the host's SHORT side = the letter height for a
            # word in either orientation (a vertical word is tall); a long
            # thin hole (a stripe in a panel) is never a counter
            compact = max(hw, hh) <= 3 * min(hw, hh)
            letter_sized = (letter_max_px is None
                            or min(pw, ph) <= letter_max_px)
            if (letter_sized and compact
                    and max(hw, hh) >= 0.25 * min(pw, ph)):
                continue                      # a counter, not a window
        sub = labels[y0:y1 + 1, x0:x1 + 1] == lab
        if tC is not None:
            rgb = a[y0:y1 + 1, x0:x1 + 1, :3][sub].astype(np.float32).mean(0)
            t = rgb - rgb.mean()
            if float((t * tC).sum()) / n2 >= 0.9:
                continue                      # clear film: stays clear
        a[y0:y1 + 1, x0:x1 + 1][sub] = (255, 255, 255, 255)
        n += 1
    if not n:
        return rgba, 0
    return Image.fromarray(a, "RGBA"), n


def text_geometry(crop_rgba):
    """Is this decal lettering only? Letter- or word-like pieces (6 px to
    70% of the crop high, no taller than 6× their width nor wider than 12×
    their height, each under 45% of the ink) are grouped into rows by their
    vertical centre; when the rows (two or more pieces, or one word-shaped
    piece) hold at least 85% of the ink, the decal is text. Returns a dict:
    text (bool), lines [(x0, y0, x1, y1) per row, top to bottom, in crop
    px], share (ink share in rows), letter_px (median piece height)."""
    a = np.asarray(crop_rgba.convert("RGBA"))
    ink = a[..., 3] > 96
    H, W = ink.shape
    total = int(ink.sum())
    none = {"text": False, "lines": [], "share": 0.0, "letter_px": 0}
    if total < 20:
        return none
    labels, stats = _label_runs(ink, diag=True)
    letters = []
    for lab, (x0, y0, x1, y1, area) in stats.items():
        w, h = x1 - x0 + 1, y1 - y0 + 1
        if area < 4:
            continue
        # a letter (each well under half the ink), or a whole word when
        # bold letters touch — wide, not tall — which may be all the ink
        # when the decal is a single caption line
        if h < 6:
            continue
        # a word of touching letters has the gaps between and inside the
        # letters: it fills well under 3/4 of its box. A solid bar or
        # panel (the white GI JOE banner) fills it — that is not text
        # (v2.23.1: the banner was replaced by "G.I.JOE" in Arial)
        fill = area / float(max(1, w * h))
        # 0.88: a word whose letters a faint halo joins fills ~0.78 of its
        # box (v2.24.1: the first DANGER! was missed); a bar fills ~1.0
        word = w >= 2.5 * h and w <= 12 * h and fill < 0.88
        letter = h <= 6 * w and w <= 3 * h and area <= 0.45 * total
        if word or letter:
            letters.append((x0, y0, x1, y1, area, w, h, lab))
    if not letters:
        return none
    hs = sorted(p[6] for p in letters)
    med_h = hs[len(hs) // 2]
    letters.sort(key=lambda p: (p[1] + p[3]) / 2.0)
    rows = []
    for p in letters:
        cy = (p[1] + p[3]) / 2.0
        if rows and abs(cy - rows[-1]["cy"]) <= 0.6 * med_h:
            r = rows[-1]
            r["items"].append(p)
            r["cy"] = sum((q[1] + q[3]) / 2.0 for q in r["items"]) / len(r["items"])
        else:
            rows.append({"cy": cy, "items": [p]})
    # a row is two or more pieces (letters, words) or one word-shaped
    # piece (at least 2.5× as wide as high: touching bold letters)
    rows = [r for r in rows
            if len(r["items"]) >= 2
            or (r["items"][0][5] >= 2.5 * r["items"][0][6])]
    # a "row" much lower than the letters is a stripe's fragments, not a
    # line of lettering (REMOVAL's stripes beside the word)
    rows = [r for r in rows
            if max(p[3] for p in r["items"]) - min(p[1] for p in r["items"])
            + 1 >= 0.5 * med_h]
    if not rows:
        return none
    # a piece in a row whose colour is not the lettering's (REMOVAL's red
    # triangle in line with its white word) is a graphic, not a letter
    rgb = a[..., :3].astype(np.float32)
    for r in rows:
        cols = []
        for p in r["items"]:
            sub = labels[p[1]:p[3] + 1, p[0]:p[2] + 1] == p[7]
            cols.append(rgb[p[1]:p[3] + 1, p[0]:p[2] + 1][sub].mean(0))
        weights = np.array([p[4] for p in r["items"]], np.float32)
        ref = (np.array(cols) * weights[:, None]).sum(0) / max(1.0, weights.sum())
        keep_it = [p for p, c in zip(r["items"], cols)
                   if float(np.sqrt(((c - ref) ** 2).sum())) <= 80]
        r["items"] = keep_it or r["items"]
    in_rows = sum(p[4] for r in rows for p in r["items"])
    share = in_rows / float(total)
    lines = []
    for r in rows:
        it = r["items"]
        lines.append((min(p[0] for p in it), min(p[1] for p in it),
                      max(p[2] for p in it) + 1, max(p[3] for p in it) + 1))
    lines.sort(key=lambda b: b[1])
    ids = [p[7] for r in rows for p in r["items"]]
    # each row widened to all ink of the lettering's colour lying in it:
    # worn letters break into bits that are not letter-shaped ("KEEP" was
    # found as "KE"); ink of another colour in the row (REMOVAL's red
    # triangle) stays out
    mask = np.isin(labels, ids)
    if mask.any():
        tcol = rgb[mask].mean(0)
        near = ink & (np.sqrt(((rgb - tcol) ** 2).sum(2)) <= 80)
        wide = []
        for (x0, y0, x1, y1) in lines:
            band = np.zeros_like(ink)
            band[y0:y1, :] = True
            lab_in = np.unique(labels[near & band])
            lab_in = lab_in[lab_in > 0]
            add = np.zeros_like(ink)
            for lid in lab_in.tolist():
                st_ = stats.get(lid)
                # a piece lying (almost) wholly inside the row's band
                if st_ and st_[1] >= y0 - 2 and st_[3] <= y1 + 1:
                    add |= (labels == lid)
            mask |= add & near
            ys, xs = np.nonzero((mask & band))
            if xs.size:
                wide.append((int(xs.min()), y0, int(xs.max()) + 1, y1))
            else:
                wide.append((x0, y0, x1, y1))
        lines = wide
    return {"text": share >= 0.85, "lines": lines, "share": share,
            "letter_px": med_h, "mask": mask}


def _group_boxes(boxes, gap, small_side, touch=6, word_pieces=3,
                 lab_img=None, lab_ids=None, down=1):
    """Size-aware grouping of raw pieces into decals, in two steps.

    1. Small pieces (longest side < small_side: letters, specks) join EACH
       OTHER within `gap` px first, so letters become their words.
    2. Then pieces join when within `gap` and at least one of them is
       still small — a speck or a 1-2 piece mark joins the graphic beside
       it — while a WORD (a cluster of `word_pieces`+ letters) counts as
       big: it joins another big piece only when they all but touch
       (`touch` px), like two big decals (the fragments a faint decal
       breaks into). Closeness of two non-small pieces is judged on their
       INK (lab_img), not their rectangles: a word inside the wide box of
       an orca's ring was "touching" it, so the two DANGER!s beside the
       orcas were cut out as part of the orca decals and never shared in
       the DANGER! copies (v2.24.2). A word (3+ letters) still joins the
       graphic it captions within `gap` when its ink is that close.
       Repeats until nothing changes."""
    # each item: x0, y0, x1, y1, piece count, set of label ids (pixels)
    # [6] = "a word": a cluster of letters from step 1 that has not yet
    # joined a graphic
    items = [[b[0], b[1], b[2], b[3], 1,
              {lab_ids[i]} if lab_ids is not None else set(), False]
             for i, b in enumerate(boxes)]

    def ink_near(a, b, g):
        """Do the INKS of two clusters come within g px? Two big decals
        whose rectangles overlap — a word beside an orca whose ring spans
        a wide box — are not touching unless their pixels are (v2.24.2)."""
        if lab_img is None or not a[5] or not b[5]:
            return True
        cg = max(1, int(np.ceil(g / float(down))))
        x0 = max(0, min(a[0], b[0]) // down - cg)
        y0 = max(0, min(a[1], b[1]) // down - cg)
        x1 = min(lab_img.shape[1], max(a[2], b[2]) // down + cg + 1)
        y1 = min(lab_img.shape[0], max(a[3], b[3]) // down + cg + 1)
        win = lab_img[y0:y1, x0:x1]
        ma = np.isin(win, list(a[5]))
        mb = np.isin(win, list(b[5]))
        if not ma.any() or not mb.any():
            return True
        return bool((dilate_mask(ma, cg) & mb).any())

    def small(it):
        return max(it[2] - it[0], it[3] - it[1]) < small_side

    def near(a, b, g):
        return (a[0] - g <= b[2] and b[0] - g <= a[2]
                and a[1] - g <= b[3] and b[1] - g <= a[3])

    def merge(a, b, word=False):
        return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]),
                max(a[3], b[3]), a[4] + b[4], a[5] | b[5], word]

    # step 1: small with small → words (a cluster keeps growing while any
    # of its letters is near a small piece)
    origin_small = [small(it) for it in items]
    clusters = [it[:] for it, sm in zip(items, origin_small) if sm]
    bigs = [it[:] for it, sm in zip(items, origin_small) if not sm]
    changed = True
    while changed and len(clusters) > 1:
        changed = False
        for i in range(len(clusters)):
            if clusters[i] is None:
                continue
            for j in range(i + 1, len(clusters)):
                if clusters[j] is None:
                    continue
                if near(clusters[i], clusters[j], gap):
                    clusters[i] = merge(clusters[i], clusters[j], word=True)
                    clusters[j] = None
                    changed = True
        clusters = [c for c in clusters if c is not None]
    for c in clusters:
        c[6] = c[4] >= word_pieces
    # step 2: everything together; a word counts as big
    allb = bigs + clusters

    def is_small(it):
        return small(it) and it[4] < word_pieces

    changed = True
    while changed and len(allb) > 1:
        changed = False
        n = len(allb)
        for i in range(n):
            if allb[i] is None:
                continue
            for j in range(i + 1, n):
                if allb[j] is None:
                    continue
                a, b = allb[i], allb[j]
                either_small = is_small(a) or is_small(b)
                # a word (letters clustered in step 1) still captions the
                # graphic beside it within `gap` — but only if its INK is
                # that close; two original big pieces must all but touch
                wordy = a[6] or b[6]
                # two LINE-HIGH pieces (a line of lettering whose letters
                # the reduced mask joined: "LGIJ1" and "84 WHALE" of one
                # title, 8 px apart) join within `gap` too, on their ink
                liney = (min(a[2] - a[0], a[3] - a[1]) <= 2 * small_side and
                         min(b[2] - b[0], b[3] - b[1]) <= 2 * small_side)
                g = gap if (either_small or wordy or liney) else min(gap, touch)
                # a line-high piece lying mostly INSIDE another's box ("CH"
                # under "UNLAT", inside the UNLATCH decal's box): one decal —
                # two overlapping crops would each draw the other's ink
                inside = False
                ix = min(a[2], b[2]) - max(a[0], b[0])
                iy = min(a[3], b[3]) - max(a[1], b[1])
                if ix > 0 and iy > 0:
                    sa = (a[2] - a[0]) * (a[3] - a[1])
                    sb = (b[2] - b[0]) * (b[3] - b[1])
                    inner = a if sa <= sb else b
                    if (min(inner[2] - inner[0], inner[3] - inner[1])
                            <= 2 * small_side
                            and ix * iy >= 0.5 * max(1, min(sa, sb))):
                        # …and its ink close (a DANGER! inside an orca's
                        # ring box is far from the orca's ink: stays apart)
                        inside = ink_near(a, b, 2 * gap)
                if inside or (near(a, b, g) and (either_small or ink_near(a, b, g))):
                    allb[i] = merge(a, b)
                    allb[j] = None
                    changed = True
        allb = [b for b in allb if b is not None]
    return [tuple(b[:4]) for b in allb]


def segment_decals(rgba, gap=16, min_side=24, pad=6, down=4, small_side=48,
                   bridge=3):
    """Split a cleaned sheet (RGBA, transparent background) into the boxes of
    its individual decals, in reading order (rows top→bottom, left→right).

    Opaque pixels form connected pieces (tiny JPEG breaks bridged by
    `bridge` px); pieces are then grouped SIZE-AWARE: a piece joins a
    neighbour within `gap` px only when one of the two is small (longest
    side < `small_side` px — letters, specks of a logo), so captions stay
    with their logo while two big decals near each other stay separate.
    gap=0 keeps every piece on its own. Specks smaller than `min_side` px
    are dropped; each box gets `pad` px around it. Segmentation runs on a
    `down`× reduced mask for speed, then the boxes are tightened on the
    full-resolution alpha."""
    a = np.asarray(rgba)
    if a.ndim != 3 or a.shape[2] != 4:
        return [(0, 0, rgba.width, rgba.height)]
    opaque = a[..., 3] > 96
    H, W = opaque.shape
    down = max(1, int(down))
    hh, ww = max(1, H // down), max(1, W // down)
    m = opaque[:hh * down, :ww * down].reshape(hh, down, ww, down).any(axis=(1, 3))
    # the quarter-scale pooling already closes breaks under `down` px; only
    # dilate when a wider bridge was asked for (gap=0 must keep pieces apart)
    r = int(bridge) // down
    md = (np.asarray(Image.fromarray(m.astype(np.uint8) * 255)
                     .filter(ImageFilter.MaxFilter(2 * r + 1))) > 0) if r >= 1 else m
    raw, raw_ids = [], []
    lab_img, _lab_stats = _label_runs(md, diag=True)
    # each component's own label, matched by its box: the first inked
    # pixel inside a box can belong to a NEIGHBOUR (the W of "AWAY" got
    # the A's label, so the word's halves looked far apart and split)
    by_box = {}
    for lid, (lx0, ly0, lx1, ly1, _ar) in (_lab_stats or {}).items():
        by_box.setdefault((lx0, ly0, lx1, ly1), lid)
    for x0, y0, x1, y1 in _components(md):
        X0, Y0 = x0 * down, y0 * down
        X1, Y1 = min(W, (x1 + 1) * down), min(H, (y1 + 1) * down)
        sub = opaque[Y0:Y1, X0:X1]
        if not sub.any():
            continue
        ys, xs = np.where(sub)
        raw.append((X0 + int(xs.min()), Y0 + int(ys.min()),
                    X0 + int(xs.max()) + 1, Y0 + int(ys.max()) + 1))
        # this component's label in the reduced map
        lid = by_box.get((x0, y0, x1, y1))
        if lid is None:
            cy, cx = np.nonzero(md[y0:y1 + 1, x0:x1 + 1])
            lid = int(lab_img[y0 + int(cy[0]), x0 + int(cx[0])]) if cy.size else 0
        raw_ids.append(int(lid))
    if gap and gap > 0:
        raw = _group_boxes(raw, int(gap), int(small_side), lab_img=lab_img,
                           lab_ids=raw_ids, down=down)
    out = []
    for bx0, by0, bx1, by1 in raw:
        bw, bh = bx1 - bx0, by1 - by0
        # a thin but LONG mark (a printed line on the sheet) is a decal;
        # only pieces small both ways are specks
        if max(bw, bh) < min_side or (min(bw, bh) < min_side
                                      and max(bw, bh) < 2 * min_side):
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


def dilate_mask(mask, r):
    """Boolean dilation by a (2r+1) square — a running sum, O(1) per pixel.
    Pillow's MaxFilter is O(r^2) per pixel AND holds Python's lock for the
    whole call: a 103x103 one froze the window for 178 s (v2.22.7)."""
    if r <= 0:
        return mask.astype(bool)
    return _box_mean(mask.astype(np.float32), int(r)) > 1e-9


def erode_mask(mask, r):
    """Boolean erosion by a (2r+1) square (see dilate_mask). Pixels whose
    window runs off the picture count the outside as empty."""
    if r <= 0:
        return mask.astype(bool)
    m = mask.astype(np.float32)
    H, W = m.shape
    pad = np.pad(m, int(r))
    full = _box_mean(pad, int(r))[int(r):int(r) + H, int(r):int(r) + W]
    # _box_mean divides by the clipped area; inside the padded frame the
    # area is always full, so a mean of 1 means every pixel was set
    return full > 1 - 1e-6


def _majority(lab, keep, n_labels, r=1):
    """For each kept pixel, the most frequent label in its (2r+1) square
    among kept pixels — a mode filter by running sums (small arrays)."""
    best = np.full(lab.shape, -1.0, np.float32)
    out = lab.copy()
    for i in range(n_labels):
        cnt = _box_mean(((lab == i) & keep).astype(np.float32), r)
        take = cnt > best
        out[take] = i
        best = np.maximum(best, cnt)
    return np.where(keep, out, lab)


def _drop_edge_rims(idx, keep, n_labels, r, lum=None, px_mm=None):
    """Scan blends snapped to an ink, removed. Returns (idx, keep).
    1. Thin slivers of one colour hugging the decal's OUTER edge (thinner
       than 2r+1 px) are made clear: the blend between the outline ink
       and the film, snapped to white or red (the orca's broken white and
       red rims, which Recraft then drew faithfully).
    2. Small patches with no thick core: in the edge band they are made
       clear; inside the decal they take the colour around them (a red
       seam where the orca's fin overlaps its black ring — misregistered
       print). A colour running along most of the outer edge is a real
       border, and a long thin line (a pinstripe) is over the size limit:
       both are kept."""
    outside = ~keep
    near_out = dilate_mask(outside, r + 1)
    band = dilate_mask(outside, 3 * r + 2)
    perim = keep & dilate_mask(outside, 1)
    n_perim = max(1, int(perim.sum()))
    out = keep.copy()
    idx = idx.copy()
    limit = (12 * r) ** 2
    total = max(1, int(out.sum()))
    if px_mm is None:
        px_mm = r / 0.12            # rim_r is 0.12 mm
    for i in range(n_labels):
        m = (idx == i) & out
        if not m.any():
            continue
        # a HALO: a lighter colour lying almost wholly in the edge band and
        # hugging darker ink along much of its boundary (the white outline
        # round "Bad Mother Tattoos & Customs", thicker than a rim and
        # joined into big pieces) is film, made clear. White ink proper
        # lies away from the edge (an orca's belly) or hugs no darker ink
        # (REMOVAL stripes on clear film).
        if lum is not None:
            darker = [j for j in range(n_labels)
                      if j != i and lum[j] < lum[i] - 40 and (idx == j).any()]
            if darker:
                dk = out & np.isin(idx, darker)
                bnd = m & dilate_mask(~m, 1)
                nb = max(1, int(bnd.sum()))
                hug = (bnd & dilate_mask(dk, 1)).sum() / float(nb)
                in_band = (m & band).sum() / float(max(1, int(m.sum())))
                # mean thickness (area per boundary pixel, x2): a halo is
                # the scan's 0.2 mm film edge; a printed outline is heavier
                thick_mm = 2.0 * m.sum() / float(nb) / float(px_mm)
                if hug >= 0.3 and in_band >= 0.7 and thick_mm <= 0.3:
                    out &= ~m
                    continue
        if (m & perim).sum() > 0.5 * n_perim:
            continue  # a real border all round
        # a LIGHTER colour that is only ever small pieces (halos round black
        # letters, a film-filled counter of an "A" or "4") and a minor share
        # of the decal is film read as ink: all of it is made clear. White
        # ink proper (an orca's belly, REMOVAL stripes) has large areas.
        if lum is not None and any(lum[j] < lum[i] - 40 for j in range(n_labels)
                                   if j != i and (idx == j).any()):
            if m.sum() < 0.25 * total:
                _lab0, st0 = _label_runs(m, diag=True)
                if st0 and max(v[4] for v in st0.values()) <= limit:
                    out &= ~m
                    continue
        thick = dilate_mask(erode_mask(m, r), r)
        # a fringe HUGS another ink; a letter in a second black (two
        # near-identical blacks in a palette) touches only the film and
        # must stay (the title "LGIJ184" lost letters without this)
        # …and a fringe is LIGHTER than the ink it hugs (ink fading into
        # clear film): a thin black stroke beside a white rim is lettering
        if lum is not None:
            darker = [j for j in range(n_labels) if lum[j] < lum[i] - 40]
            other = out & np.isin(idx, darker) if darker else np.zeros_like(out)
        else:
            other = out & (idx != i)
        near_other = dilate_mask(other, r + 1)
        rim = m & ~thick & near_out & near_other
        out &= ~rim
        m &= ~rim
        lab, stats = _label_runs(m, diag=True)
        for lid, (x0, y0, x1, y1, area) in (stats or {}).items():
            if area > limit:
                continue
            sl = (slice(max(0, y0 - 1), y1 + 2), slice(max(0, x0 - 1), x1 + 2))
            sub = lab[sl] == lid
            ring = dilate_mask(sub, 1) & ~sub
            n_ring = max(1, int(ring.sum()))
            hugs = (ring & other[sl]).sum() >= 0.25 * n_ring
            # wholly on the edge — or a small light patch TOUCHING the film
            # wedged against darker ink (the white where the orca's tail
            # and fin cross its ring): film showing through, made clear
            if hugs and ((sub & ~band[sl]).sum() <= 0.05 * area
                         or (lum is not None and (sub & near_out[sl]).any())):
                out[sl][sub] = False
                continue
            if (sub & thick[sl]).sum() > 0.05 * area:
                continue  # has a solid core: a real dot or mark
            if not hugs:
                continue  # its own mark, not a seam between inks
            ring = ring & out[sl]
            nb = idx[sl][ring]
            nb = nb[nb != i]
            if nb.size:
                cnt = np.bincount(nb, minlength=n_labels)
                # a STRAY: its ink is used nowhere near (the red seam where
                # the fin crosses the ring, far from the red brush); a mark
                # whose ink is close by is a detail (the navy wedges in a
                # CAUTION icon, beside the icon's navy ring) and stays
                R = 10 * r
                wy = slice(max(0, y0 - R), y1 + R + 1)
                wx = slice(max(0, x0 - R), x1 + R + 1)
                same = ((idx[wy, wx] == i) & out[wy, wx]).sum() - area
                if same < area:
                    idx[sl][sub] = cnt.argmax()
    # the cut leaves the vote's square steps along the outline: round them
    if (out != keep).any():
        rr = max(1, int(round(1.5 * r)))
        soft = _box_mean(_box_mean(out.astype(np.float32), rr), rr) >= 0.5
        # only where something was cut: a thin stroke elsewhere (fine
        # lettering) must not be thinned by the rounding
        near_cut = dilate_mask(keep & ~out, 2 * rr)
        out = out & np.where(near_cut, soft, True)   # rounds; never brings a cut pixel back
    return idx, out


def _mask_polygons(mask):
    """Outlines of a boolean mask as closed polygons [(N,2) float arrays]
    (outer edges and holes alike; fill them even-odd), via vtracer's
    polygon mode."""
    import io as _io
    import re as _re
    import vtracer
    im = Image.fromarray(np.where(mask, 0, 255).astype(np.uint8)).convert("RGB")
    buf = _io.BytesIO()
    im.save(buf, format="PNG")
    svg = vtracer.convert_raw_image_to_svg(
        buf.getvalue(), img_format="png", colormode="binary",
        mode="polygon", filter_speckle=0)
    polys = []
    for tag in _re.findall(r"<path\b[^>]*>", svg):
        fm = _re.search(r'fill="#([0-9a-fA-F]{6})"', tag)
        if fm and fm.group(1).lower() != "000000":
            continue
        dm = _re.search(r'\bd="([^"]*)"', tag)
        if not dm:
            continue
        tx = ty = 0.0
        tm = _re.search(r"translate\(\s*([-\d.]+)[ ,]+([-\d.]+)", tag)
        if tm:
            tx, ty = float(tm.group(1)), float(tm.group(2))
        for sub in _re.split(r"(?=M)", dm.group(1)):
            pts = _re.findall(r"(-?[\d.]+)\s*,\s*(-?[\d.]+)", sub)
            if len(pts) >= 3:
                a = np.asarray(pts, np.float64)
                a[:, 0] += tx
                a[:, 1] += ty
                polys.append(a)
    return polys


def _simple_shape(poly, tol, max_vertices=8, min_iou=0.93):
    """A piece that is really a simple polygon (a flag stripe: a slanted
    parallelogram) as that polygon: Douglas-Peucker at `tol` keeps at most
    `max_vertices` corners and still covers the piece (IoU >= min_iou).
    None otherwise."""
    from PIL import ImageDraw
    pts0 = np.asarray(poly, np.float64)
    if len(pts0) < 4:
        return None
    # spikes off (a scratch poking from a stripe): the piece opened by
    # ~tol before its corners are found — Douglas-Peucker took the spike's
    # tip as a corner
    gx0, gy0 = pts0.min(0) - 4
    gx1, gy1 = pts0.max(0) + 4
    GW, GH = int(gx1 - gx0) + 1, int(gy1 - gy0) + 1
    gim = Image.new("1", (GW, GH), 0)
    ImageDraw.Draw(gim).polygon([(x - gx0, y - gy0) for x, y in pts0], fill=1)
    gm = np.asarray(gim, bool)
    ro = max(1, int(round(tol)))
    opened = dilate_mask(erode_mask(gm, ro), ro)
    cand = [q for q in _mask_polygons(opened)] if opened.any() else []
    if not cand:
        return None
    pts = max(cand, key=lambda q: len(q)) + np.array([gx0, gy0])
    n = len(pts)
    far = int(np.argmax(((pts - pts[0]) ** 2).sum(1)))
    keys = {0, far}
    P = np.vstack([pts, pts[:1]])
    stack = [(0, far), (far, n)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        a, b = P[i], P[j]
        d = b - a
        L = float(np.hypot(d[0], d[1])) or 1e-9
        seg = P[i + 1:j]
        dist = np.abs((seg[:, 0] - a[0]) * d[1] - (seg[:, 1] - a[1]) * d[0]) / L
        k = int(np.argmax(dist))
        if dist[k] > tol:
            m = i + 1 + k
            keys.add(m)
            stack += [(i, m), (m, j)]
            if len(keys) > max_vertices:
                return None
    simple = P[sorted(keys)]
    if len(simple) < 3:
        return None
    x0, y0 = pts0.min(0) - 2
    x1, y1 = pts0.max(0) + 2
    W, H = int(x1 - x0) + 1, int(y1 - y0) + 1
    a_ = Image.new("1", (W, H), 0)
    ImageDraw.Draw(a_).polygon([(x - x0, y - y0) for x, y in pts0], fill=1)
    b_ = Image.new("1", (W, H), 0)
    ImageDraw.Draw(b_).polygon([(x - x0, y - y0) for x, y in simple], fill=1)
    A, B = np.asarray(a_, bool), np.asarray(b_, bool)
    if (A & B).sum() / float(max(1, (A | B).sum())) < min_iou:
        return None
    return simple


def _perfect_star(poly, min_iou=0.85):
    """A piece shaped like a five-pointed star, rebuilt as a crisp star with
    straight edges: its five tips (the farthest points, at least 40 degrees
    apart) and the five notches between them (the nearest points), joined
    by straight lines — the star keeps its own shape and place (the GI JOE
    star is tilted, not regular). None when the piece is not a star."""
    from PIL import ImageDraw
    pts = np.asarray(poly, np.float64)
    if len(pts) < 10:
        return None
    x0, y0 = pts.min(0) - 2
    x1, y1 = pts.max(0) + 2
    W, H = int(x1 - x0) + 1, int(y1 - y0) + 1
    if W < 12 or H < 12:
        return None
    im = Image.new("1", (W, H), 0)
    ImageDraw.Draw(im).polygon([(x - x0, y - y0) for x, y in pts], fill=1)
    m = np.asarray(im, bool)
    ys, xs = np.nonzero(m)
    if xs.size < 50:
        return None
    cx, cy = xs.mean() + x0, ys.mean() + y0
    # dense outline
    nxt = np.roll(pts, -1, 0)
    dense = []
    for a, b in zip(pts, nxt):
        k = max(1, int(np.ceil(np.hypot(*(b - a)))))
        t = np.arange(k)[:, None] / float(k)
        dense.append(a + (b - a) * t)
    P = np.vstack(dense)
    d = np.hypot(P[:, 0] - cx, P[:, 1] - cy)
    ang = np.arctan2(P[:, 1] - cy, P[:, 0] - cx)
    tips = []
    avail = np.ones(len(P), bool)
    for _ in range(5):
        if not avail.any():
            return None
        j = int(np.argmax(np.where(avail, d, -1)))
        tips.append(j)
        avail &= np.abs(np.angle(np.exp(1j * (ang - ang[j])))) > np.radians(40)
    tips.sort(key=lambda j: ang[j])
    star = []
    for t_i in range(5):
        ja, jb = tips[t_i], tips[(t_i + 1) % 5]
        a_a, a_b = ang[ja], ang[jb]
        span = (a_b - a_a) % (2 * np.pi)
        rel = (ang - a_a) % (2 * np.pi)
        sel = (rel > 0.15 * span) & (rel < 0.85 * span)
        if not sel.any():
            return None
        jn = int(np.argmin(np.where(sel, d, np.inf)))
        star.append(tuple(P[ja]))
        star.append(tuple(P[jn]))
    # a star's notches sit well inside its tips
    dt = np.array([d[j] for j in tips])
    dn = np.array([np.hypot(x - cx, y - cy) for x, y in star[1::2]])
    if dn.max() > 0.75 * dt.min():
        return None
    im2 = Image.new("1", (W, H), 0)
    ImageDraw.Draw(im2).polygon([(x - x0, y - y0) for x, y in star], fill=1)
    m2 = np.asarray(im2, bool)
    iou = (m & m2).sum() / float(max(1, (m | m2).sum()))
    if iou < min_iou:
        return None
    return np.asarray(star)


def _straighten_poly(pts, tol, min_len, stats=None, force=False):
    """Long runs of a closed outline that wobble ACROSS a straight line
    (within tol either side, scan raggedness) become that straight line;
    curves (deviation all to one side) and short runs are kept."""
    if len(pts) < 6:
        return pts
    # resample every pixel: vtracer's stair corners alternate sides even
    # along a curve, so only a dense outline shows a curve's one-sided bend
    nxt = np.roll(pts, -1, 0)
    seglen = np.hypot(*(nxt - pts).T)
    parts = []
    for a, b, L in zip(pts, nxt, seglen):
        k = max(1, int(np.ceil(L)))
        t = np.arange(k)[:, None] / float(k)
        parts.append(a + (b - a) * t)
    pts = np.vstack(parts)
    n = len(pts)
    # start at the point farthest from the first, so the loop splits well
    far = int(np.argmax(((pts - pts[0]) ** 2).sum(1)))
    keys = {0, far}
    stack = [(0, far), (far, n)]
    P = np.vstack([pts, pts[:1]])
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        a, b = P[i], P[j]
        d = b - a
        L = float(np.hypot(d[0], d[1])) or 1e-9
        seg = P[i + 1:j]
        dist = np.abs((seg[:, 0] - a[0]) * d[1] - (seg[:, 1] - a[1]) * d[0]) / L
        k = int(np.argmax(dist))
        if dist[k] > tol:
            m = i + 1 + k
            keys.add(m)
            stack += [(i, m), (m, j)]
    keys = sorted(keys) + [n]
    out = []
    for i, j in zip(keys[:-1], keys[1:]):
        a, b = P[i], P[j]
        d = b - a
        L = float(np.hypot(d[0], d[1])) or 1e-9
        seg = P[i + 1:j]
        straight = bool(force)
        if not force and L >= min_len and len(seg):
            sd = ((seg[:, 0] - a[0]) * d[1] - (seg[:, 1] - a[1]) * d[0]) / L
            # a CURVE bows: a parabola fitted along the run has a clear
            # sagitta; scan raggedness (dents and specks, often all on one
            # side) does not. The earlier mean-vs-RMS test called a dented
            # straight edge a curve (GI JOE banner: 9-27% straight)
            tt = ((seg[:, 0] - a[0]) * d[0] + (seg[:, 1] - a[1]) * d[1]) / (L * L)
            if len(seg) >= 3:
                coef = np.polyfit(np.clip(tt, 0.0, 1.0), sd, 2)
                sag = abs(float(coef[0])) / 4.0
            else:
                sag = 0.0
            straight = sag <= 0.25 * tol
        out.append(P[i:i + 1])
        if not straight:
            out.append(seg)
        if stats is not None:
            stats[0] += L if straight else 0.0
            stats[1] += max(L, float(j - i))
    return np.vstack(out)


def geometric_svg(crop_rgba, pal, px_per_mm, w_in, h_in, long_px=2048,
                  tol_mm=0.4, min_mm=0.6, min_straight=0.5, min_iou=0.95,
                  smooth_mm=0.15, speck_mm=0.45, round_mm=0.2, dp_mm=0.2,
                  min_layer_iou=0.95):
    """A decal drawn in straight lines (a GI JOE banner: slanted block
    letters, a striped flag) rebuilt directly as straight-sided polygons
    — Recraft traced the scan's ragged edges however clean the picture
    sent was. The decal is flattened (own inks, no flecks or rims), each
    ink's outline is straightened (runs that wobble across a line within
    tol_mm become that line; curves stay), and when at least
    `min_straight` of the outline length is straight runs and the result
    still covers the scan (IoU >= min_iou), the SVG is returned (crop-
    pixel coordinates, the dominant ink painted under the others so no
    hairline gaps show). None = not a straight-line decal."""
    from PIL import ImageDraw
    crop = crop_rgba.convert("RGBA")
    W, H = crop.size
    if pal is None or len(pal) == 0 or min(W, H) < 8:
        return None
    s = max(1.0, min(4.0, long_px / float(max(W, H))))
    nw, nh = int(round(W * s)), int(round(H * s))
    ppm = px_per_mm * s
    big = flatten_decal(crop, pal, size=(nw, nh), band=max(1, int(round(s))),
                        smooth_r=max(1, int(round(smooth_mm * ppm))),
                        rim_r=max(1, int(round(0.12 * ppm))))
    a = np.asarray(big)
    keep = a[..., 3] > 0
    speck = (speck_mm * ppm) ** 2

    def _area(q):
        x, y = q[:, 0], q[:, 1]
        return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
    if keep.sum() < 50:
        return None
    P = np.asarray(pal, np.int32)
    px = a[..., :3].astype(np.int32).reshape(-1, 3)
    idx = ((px[:, None, :] - P[None, :, :]) ** 2).sum(2).argmin(1).reshape(keep.shape)
    tol = max(1.0, tol_mm * ppm)
    min_len = max(8.0, min_mm * ppm)
    acc = [0.0, 0.0]
    layers = []
    areas = [int(((idx == i) & keep).sum()) for i in range(len(P))]
    order = sorted([i for i in range(len(P)) if areas[i] > 0], key=lambda i: -areas[i])
    redrawn = np.zeros(keep.shape, bool)
    union_polys = []
    layer_ious = []
    rr = max(1, int(round(round_mm * ppm)))
    dp = max(1.0, dp_mm * ppm)
    for i in [None] + order[1:]:
        mask = keep if i is None else ((idx == i) & keep)
        # missing paint: a small CLEAR hole inside an ink (a scratch across
        # the banner's white stripe) is filled; a hole holding another ink
        # (the star in the white letters) is not
        holes = ~mask
        hl, hst = (_label_runs(holes, diag=False) if i is None
                   else (None, None))       # the outline only: a layer's
        # "hole" can be another ink's area (the tick came out navy)
        if hst:
            edge_ids = set(np.unique(np.concatenate([hl[0, :], hl[-1, :],
                                                     hl[:, 0], hl[:, -1]])).tolist())
            lim = (1.2 * ppm) ** 2
            for lid, (hx0, hy0, hx1, hy1, harea) in hst.items():
                if lid in edge_ids:
                    continue
                # small, or a long thin scratch (up to 0.6 mm wide, 3 mm
                # long: the tick across the banner's white stripe)
                long_ = max(hx1 - hx0, hy1 - hy0) + 1
                thin = harea / float(long_) <= 1.0 * ppm and long_ <= 4.0 * ppm
                if harea > lim and not thin:
                    continue
                sub = hl[hy0:hy1 + 1, hx0:hx1 + 1] == lid
                if harea > lim:
                    # a bigger thin hole is a scratch only when it crosses
                    # from one ink into another; a letter's counter (the
                    # hole of an O) sits inside one ink and stays clear
                    sl_ = (slice(max(0, hy0 - 2), hy1 + 3), slice(max(0, hx0 - 2), hx1 + 3))
                    sub2 = hl[sl_] == lid
                    ring = dilate_mask(sub2, 2) & ~sub2 & keep[sl_]
                    inks = idx[sl_][ring]
                    if inks.size == 0:
                        continue
                    cnt = np.bincount(inks, minlength=len(P))
                    if (cnt >= 0.05 * inks.size).sum() < 2:
                        continue
                if (sub & keep[hy0:hy1 + 1, hx0:hx1 + 1]).sum() <= 0.1 * harea:
                    mask[hy0:hy1 + 1, hx0:hx1 + 1] |= sub
        # rounded, not voted: a soft mask has no square steps
        mask = _box_mean(_box_mean(mask.astype(np.float32), rr), rr) >= 0.5
        polys = []
        gate_polys = []
        for poly in _mask_polygons(mask):
            if _area(poly) < speck:
                continue                    # a fleck of scan dust
            # a thin dash of scan edge (white bits along the top of the
            # banner's red stripe): under 0.25 mm thick and 1 mm2
            per = float(np.hypot(*(np.roll(poly, -1, 0) - poly).T).sum())
            ar = _area(poly)
            if ar < ppm * ppm and 2.0 * ar / max(1.0, per) < 0.25 * ppm:
                continue
            # is it straight-line art? (curve-aware, for the decision)
            _straighten_poly(poly, tol, min_len, stats=acc)
            # the detail gate judges the edge-straightened outline; the
            # exact shapes below (a stripe as its parallelogram, the star)
            # are cleaner than the scan and would fail it for that
            gate_polys.append(_straighten_poly(poly, dp, min_len))
            # the drawing: straight runs straight (within 0.3 mm — notches
            # in the banner letters' edges went), curves (a round symbol in
            # a panel) kept on the rounded outline
            # the fewest corners that still fit: a stripe is its 4-corner
            # parallelogram, without a scratch's spike or a notch
            shp = None
            # stripes (inks) up to 8 corners; the outline's pieces — the
            # letters of a logo, angular block capitals — up to 16, so they
            # come out with straight edges and sharp corners like the clean
            # logo art the user supplied as the quality target
            for nv in ((4, 5, 6, 8) if i is not None else (4, 6, 8, 10, 12, 14, 16)):
                for tmul in (1.0, 2.0, 4.0):
                    shp = _simple_shape(poly, tol * tmul, max_vertices=nv,
                                        min_iou=0.93 if i is not None else 0.92)
                    if shp is not None:
                        break
                if shp is not None:
                    break
            if shp is None and i is not None:
                shp = _perfect_star(poly)
            if shp is None and i is None:
                # the outline (letters joined to the flag): its straight runs
                # straightened at the coarser tol, so the scan's notches in
                # the letter edges (up to ~0.4 mm) go; curves still stay
                shp = _straighten_poly(poly, tol, min_len * 0.5)
            polys.append(shp if shp is not None
                         else _straighten_poly(poly, dp, min_len))
        if i is None:
            union_polys = polys
            img = Image.new("1", (nw, nh), 0)
            acc_m = np.zeros(keep.shape, bool)
            for q in polys:
                im = Image.new("1", (nw, nh), 0)
                ImageDraw.Draw(im).polygon([tuple(p) for p in q], fill=1)
                acc_m ^= np.asarray(im, bool)
            redrawn = acc_m
        else:
            layers.append((i, polys))
            # each ink must still match the scan's: fine detail (thin
            # lines and arrows in a panel's symbols) is rounded away by
            # this path, so such decals go to the normal drawing
            lm = np.zeros(keep.shape, bool)
            for q in gate_polys:
                im = Image.new("1", (nw, nh), 0)
                ImageDraw.Draw(im).polygon([tuple(p) for p in q], fill=1)
                lm ^= np.asarray(im, bool)
            orig = (idx == i) & keep
            u = (lm | orig).sum()
            layer_ious.append((lm & orig).sum() / float(max(1, u)))
    inter = (redrawn & keep).sum()
    union = (redrawn | keep).sum()
    geometric_svg.last = (acc[0] / max(1e-6, acc[1]), inter / max(1.0, float(union)),
                          min(layer_ious) if layer_ious else 1.0)
    if layer_ious and min(layer_ious) < min_layer_iou:
        return None
    if acc[1] <= 0 or acc[0] / acc[1] < min_straight:
        return None
    if union == 0 or inter / float(union) < min_iou:
        return None

    def path_d(polys):
        out = []
        for q in polys:
            q = q / s
            out.append("M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in q) + " Z")
        return " ".join(out)

    def hexc(i):
        return "#%02x%02x%02x" % tuple(int(v) for v in P[i])
    parts = [f'<path fill="{hexc(order[0])}" fill-rule="evenodd" '
             f'd="{path_d(union_polys)}"/>']
    # each ink over the base gets a hairline of its own colour so the base
    # never shows along its edges (white rims along the flag's stripes)
    sw = dp / s
    for i, polys in layers:
        if polys:
            parts.append(f'<path fill="{hexc(i)}" fill-rule="evenodd" '
                         f'stroke="{hexc(i)}" stroke-width="{sw:.3f}" '
                         f'stroke-linejoin="round" d="{path_d(polys)}"/>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{w_in:.4f}in" height="{h_in:.4f}in" '
            f'viewBox="0 0 {W} {H}">' + "".join(parts) + "</svg>")


def straighten_edges(idx, keep, n_labels, px_per_mm, tol_mm=0.25,
                     min_mm=1.5):
    """Ragged straight edges made straight (a banner's bars and letter
    stems wobble ~0.3 mm in the scan, and Recraft traced the wobble). Each
    ink's outline, and the decal's outline, is redrawn with its long
    wobbling runs replaced by straight lines; curves and corners stay.
    Returns (idx, keep)."""
    from PIL import ImageDraw
    H, W = keep.shape
    tol = max(1.0, tol_mm * px_per_mm)
    min_len = max(8.0, min_mm * px_per_mm)

    def redraw(mask):
        acc = np.zeros((H, W), bool)
        for poly in _mask_polygons(mask):
            sp = _straighten_poly(poly, tol, min_len)
            im = Image.new("1", (W, H), 0)
            ImageDraw.Draw(im).polygon([tuple(p) for p in sp], fill=1)
            acc ^= np.asarray(im, bool)
        return acc

    try:
        new_keep = redraw(keep)
        areas = [((idx == i) & keep).sum() for i in range(n_labels)]
        out = np.full((H, W), -1, np.int32)
        for i in sorted(range(n_labels), key=lambda i: -areas[i]):
            if areas[i] == 0:
                continue
            own = (idx == i) & keep
            # an ink claims only pixels near where it was: a thin ring (a
            # letter's white halo) straightened cut chords across the
            # letters and painted their tops white
            out[redraw(own) & dilate_mask(own, int(tol) + 1)] = i
    except Exception:
        return idx, keep
    # pixels no ink claims keep their old ink, or the nearest one
    gap = new_keep & (out < 0)
    out[gap & (idx >= 0) & keep] = idx[gap & (idx >= 0) & keep]
    for _ in range(4):
        gap = new_keep & (out < 0)
        if not gap.any():
            break
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            src = np.roll(np.roll(out, dy, 0), dx, 1)
            take = gap & (out < 0) & (src >= 0)
            out[take] = src[take]
    new_keep &= out >= 0
    return np.where(new_keep, out, idx), new_keep


def flatten_decal(rgba, pal, s=1, alpha_cut=160, size=None, band=None,
                  drop_orphans=False, smooth_r=1, rim_r=0, straight_ppm=0):
    """A decal made clean and flat for a tracer: enlarged (s times, or to
    `size`), every pixel snapped to the palette (the scan's own inks), the
    edge blends given the colour of the ink just inside (a band of `band`
    px, default s), a 3x3 majority pass against halftone mottling, the
    alpha hard. Returns RGBA."""
    W, H = rgba.size
    if size is None:
        size = (W * s, H * s)
    if band is None:
        band = s
    up = rgba.resize(size, Image.LANCZOS)
    ua = np.asarray(up)
    alpha = ua[..., 3]
    keep = alpha >= alpha_cut
    if pal is None:
        flat = ua[..., :3].copy()
    else:
        P = np.asarray(pal, np.int32)
        px = ua[..., :3].astype(np.int32).reshape(-1, 3)
        idx = ((px[:, None, :] - P[None, :, :]) ** 2).sum(2).argmin(1)
        idx = idx.reshape(alpha.shape).astype(np.int32)
        # edge blends (the outer scan pixel and soft alpha) take the index
        # of a solid neighbour; a real outline's inside is the outline
        solid = erode_mask(keep, band) & (alpha >= 250)
        edge = keep & ~solid
        have = solid.copy()
        for _ in range(3 * band):
            if not (edge & ~have).any():
                break
            grown = idx.copy()
            got = have.copy()
            for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0),
                           (1, 1), (1, -1), (-1, 1), (-1, -1)):
                src_have = np.roll(np.roll(have, dy, 0), dx, 1)
                src_idx = np.roll(np.roll(idx, dy, 0), dx, 1)
                take = edge & ~got & src_have
                grown[take] = src_idx[take]
                got |= take
            idx, have = grown, got
        if drop_orphans:
            # slivers that never touch solid ink are blends, not decal
            keep = keep & have
        # majority vote over a (2r+1) square: halftone mottle goes, and with
        # a larger r (about 0.15 mm) small notches and flecks — dust and
        # scratches in the scan — are voted away while corners stay
        idx = _majority(idx, keep, len(pal), r=max(1, int(smooth_r)))
        if rim_r and rim_r > 0:
            P = np.asarray(pal, np.float32)
            lum = (0.299 * P[:, 0] + 0.587 * P[:, 1] + 0.114 * P[:, 2]).tolist()
            idx, keep = _drop_edge_rims(idx, keep, len(pal), int(rim_r), lum=lum)
        if straight_ppm and straight_ppm > 0:
            idx, keep = straighten_edges(idx, keep, len(pal), float(straight_ppm))
        flat = np.zeros(alpha.shape + (3,), np.uint8)
        flat[keep] = np.asarray(pal, np.uint8)[np.clip(idx[keep], 0, len(pal) - 1)]
    return Image.fromarray(np.dstack([flat, (keep * 255).astype(np.uint8)]), "RGBA")


def _trace_piece(rgba, pal, s, target_px, alpha_cut=160):
    """One decal: flattened (flatten_decal) at s times and traced with the
    white kept. Returns (svg, raster) like vectorize()."""
    flat = flatten_decal(rgba, pal, s=s, alpha_cut=alpha_cut)
    return vectorize(flat, target_px=target_px,
                     filter_speckle=max(4, 2 * s), quantize_colors=0,
                     presmooth=False, drop_halo=False)


def trace_sheet(rgba, target_px, alpha_cut=160, colors=16, cancelled=None):
    """Vectorize a whole cleaned sheet faithfully (the 'Vectorize' mode),
    ONE DECAL AT A TIME: the tracer holds Python's lock for its whole
    call, and a whole page at 2-3x froze the window for seconds. Per
    decal every call stays short. The palette is the sheet's own inks
    (palette_of, near-white = white ink); each decal is enlarged 2-3x so
    thin strokes survive, snapped, edge blends re-coloured from inside,
    traced with the white kept (the alpha says what is background).
    Returns (sheet_svg in scan-pixel coordinates, raster at target_px)."""
    rgba = rgba.convert("RGBA")
    W, H = rgba.size
    pal = palette_of(rgba, colors=colors, merge=40, min_share=0.004, erode=1)
    k = float(target_px) / max(1, W)
    sheet = Image.new("RGBA", (max(1, int(round(W * k))),
                               max(1, int(round(H * k)))), (0, 0, 0, 0))
    parts = []
    boxes = segment_decals(rgba, gap=6, min_side=4, pad=4)
    for (x0, y0, x1, y1) in boxes:
        if cancelled and cancelled():
            return None
        crop = rgba.crop((x0, y0, x1, y1))
        cw, ch = crop.size
        s = 3 if max(cw, ch) * 3 <= 3600 else (2 if max(cw, ch) * 2 <= 3600 else 1)
        pw = max(1, int(round(cw * k)))
        svg, ras = _trace_piece(crop, pal, s, target_px=pw, alpha_cut=alpha_cut)
        ph = max(1, int(round(ch * k)))
        if ras.size != (pw, ph):
            ras = ras.resize((pw, ph), Image.LANCZOS)
        px, py = int(round(x0 * k)), int(round(y0 * k))
        part = ras.crop((0, 0, max(1, min(pw, sheet.width - px)),
                         max(1, min(ph, sheet.height - py))))
        if px < sheet.width and py < sheet.height:
            sheet.alpha_composite(part, (px, py))
        parts.append(f'<g transform="translate({x0} {y0}) scale({1.0 / s:.6f})">'
                     f'{svg_inner(svg)}</g>')
    sheet_svg = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
                 f'width="{W}" height="{H}" viewBox="0 0 {W} {H}">\n'
                 + "\n".join(parts) + "\n</svg>\n")
    return sheet_svg, sheet


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
def _copy_sig(rgba, box, size, crop_fn=None):
    """A decal for copy matching: cropped tight to its ink, on mid grey,
    shrunk to `size` and blurred so halftone dots and JPEG noise wash out."""
    c = (crop_fn(box) if crop_fn else rgba.crop(box)).convert("RGBA")
    bb = c.split()[3].point(lambda v: 255 if v > 96 else 0).getbbox()
    if bb:
        c = c.crop(bb)
    bg = Image.new("RGBA", c.size, (128, 128, 128, 255))
    bg.alpha_composite(c)
    g = bg.convert("RGB").resize(size, Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(1.2))
    return np.asarray(g).astype(np.float32)


def _ncc(a, b):
    """Best normalised correlation of a and b over 1-px shifts."""
    best = -1.0
    xa = a - a.mean()
    na = float(np.sqrt((xa * xa).sum())) + 1e-6
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            bb = np.roll(np.roll(b, dy, 0), dx, 1)
            yb = bb - bb.mean()
            v = float((xa * yb).sum() / (na * (float(np.sqrt((yb * yb).sum())) + 1e-6)))
            best = max(best, v)
    return best


def find_copies(rgba, boxes, size_tol=0.06, min_ncc=0.80, min_px=12,
                crop_fn=None):
    """Groups of decals that look like the SAME design printed several
    times on a sheet — straight, turned 180 degrees or mirrored: same size
    within `size_tol`, and blurred pictures correlating at `min_ncc` or
    more (measured on the user's sheets: real copies 0.86-1.0 through
    halftone noise, different designs under 0.5 — but different words of
    one size can reach 0.93, so redraw_sheet checks every placement
    against the copy's own scan before using it). Groups are formed
    around one reference (no chaining). Returns [[(index, how), ...]]
    with how in {"", "turn", "mirror"}, reference first; 2+ members only."""
    n = len(boxes)
    sizes = [(b[2] - b[0], b[3] - b[1]) for b in boxes]
    taken = [False] * n
    groups = []
    for i in range(n):
        if taken[i] or min(sizes[i]) < min_px:
            continue
        wi, hi = sizes[i]
        f = 48.0 / max(wi, hi)
        size = (max(8, int(round(wi * f))), max(8, int(round(hi * f))))
        a = None
        group = [(i, "")]
        for j in range(i + 1, n):
            if taken[j] or min(sizes[j]) < min_px:
                continue
            wj, hj = sizes[j]
            if abs(wi - wj) > size_tol * max(wi, wj) or \
                    abs(hi - hj) > size_tol * max(hi, hj):
                continue
            if a is None:
                a = _copy_sig(rgba, boxes[i], size, crop_fn)
            b = _copy_sig(rgba, boxes[j], size, crop_fn)
            v, how = max((_ncc(a, b), ""), (_ncc(a, b[::-1, ::-1].copy()), "turn"),
                         (_ncc(a, b[:, ::-1].copy()), "mirror"))
            if v >= min_ncc:
                group.append((j, how))
        if len(group) >= 2:
            for idx, _h in group:
                taken[idx] = True
            groups.append(group)
    return groups


def straight_bars(rgba, min_elong=6.0, min_len=12):
    """Long thin pieces (the stripes beside TURRET INFO, RAMP ISNTR,
    UNLATCH) rebuilt as perfectly straight bars: each piece's centre line
    and thickness from its pixels (principal axis), collinear pieces of one
    stripe broken by wear joined into one. Returns (svg parts in crop px,
    mask of the pixels the bars replace)."""
    a = np.asarray(rgba.convert("RGBA"))
    ink = a[..., 3] > 96
    H, W = ink.shape
    none = ([], np.zeros_like(ink))
    if not ink.any():
        return none
    labels, stats = _label_runs(ink, diag=True)
    bars = []
    for lid, (x0, y0, x1, y1, area) in (stats or {}).items():
        if area < min_len:
            continue
        sub = labels[y0:y1 + 1, x0:x1 + 1] == lid
        ys, xs = np.nonzero(sub)
        pts = np.stack([xs + x0, ys + y0], 1).astype(np.float64)
        c = pts.mean(0)
        u, s_, vt = np.linalg.svd(pts - c, full_matrices=False)
        d = vt[0]
        t = (pts - c) @ d
        n = (pts - c) @ vt[1]
        length = float(t.max() - t.min() + 1)
        width = max(1.0, float(area) / length)
        if length < min_len or length / width < min_elong:
            continue
        # straight edges: the off-axis spread must be bar-like, not a curve
        if float(np.abs(n).max()) > 1.6 * width + 2:
            continue
        col = a[..., :3][y0:y1 + 1, x0:x1 + 1][sub].mean(0)
        bars.append(dict(c=c, d=d, t0=float(t.min()), t1=float(t.max()),
                         w=width, col=col, ids=[lid]))
    if not bars:
        return none
    # join collinear pieces of one stripe (worn breaks)
    merged = True
    while merged:
        merged = False
        for i in range(len(bars)):
            for j in range(i + 1, len(bars)):
                A, B = bars[i], bars[j]
                if abs(float(A["d"] @ B["d"])) < 0.998:      # ~3.6 degrees
                    continue
                d = A["d"] if float(A["d"] @ B["d"]) > 0 else A["d"]
                nrm = np.array([-d[1], d[0]])
                if abs(float((B["c"] - A["c"]) @ nrm)) > max(A["w"], B["w"]):
                    continue
                tb0 = float((B["c"] - A["c"]) @ d) + (B["t0"] if float(A["d"] @ B["d"]) > 0 else -B["t1"])
                tb1 = float((B["c"] - A["c"]) @ d) + (B["t1"] if float(A["d"] @ B["d"]) > 0 else -B["t0"])
                gap = max(tb0 - A["t1"], A["t0"] - tb1)
                if gap > 4 * max(A["w"], B["w"]) + 3:
                    continue
                la, lb = A["t1"] - A["t0"], tb1 - tb0
                A["t0"], A["t1"] = min(A["t0"], tb0), max(A["t1"], tb1)
                A["w"] = (A["w"] * la + B["w"] * lb) / max(1e-6, la + lb)
                A["col"] = (A["col"] * la + B["col"] * lb) / max(1e-6, la + lb)
                A["ids"] += B["ids"]
                bars.pop(j)
                merged = True
                break
            if merged:
                break
    parts = []
    used = np.isin(labels, [i for b in bars for i in b["ids"]])
    for b in bars:
        d = b["d"]
        nrm = np.array([-d[1], d[0]]) * (b["w"] / 2.0)
        p0 = b["c"] + d * b["t0"]
        p1 = b["c"] + d * (b["t1"] + 1)
        q = [p0 + nrm, p1 + nrm, p1 - nrm, p0 - nrm]
        hexc = "#%02x%02x%02x" % tuple(int(round(v)) for v in b["col"])
        parts.append('<path fill="%s" d="M%s Z"/>' % (hexc, " L".join(
            "%.2f,%.2f" % (x, y) for x, y in q)))
    return parts, used


def decal_owner_map(rgba, boxes, down=4):
    """Which decal every opaque pixel belongs to: the sheet's ink pieces
    (labelled on the `down`x reduced mask, like segment_decals) each go to
    the SMALLEST box that holds them whole, else the box they overlap
    most. Returns an int32 map (-1 = none) at full size, or None. A decal's
    crop then holds its own ink only: a DANGER! inside an orca's box was
    drawn into the orca by Recraft and came back, mirrored, as a ghost
    beside the orca's copy (v2.24.3)."""
    try:
        a = np.asarray(rgba)
        opaque = a[..., 3] > 96
        H, W = opaque.shape
        hh, ww = max(1, H // down), max(1, W // down)
        m = np.zeros((hh, ww), bool)
        m[:, :] = opaque[:hh * down, :ww * down].reshape(
            hh, down, ww, down).any(axis=(1, 3))
        lab, stats = _label_runs(m, diag=True)
        owner_of = np.full(int(lab.max()) + 1, -1, np.int32)
        areas = [(b[2] - b[0]) * (b[3] - b[1]) for b in boxes]
        for lid, (x0, y0, x1, y1, _area) in (stats or {}).items():
            X0, Y0 = x0 * down, y0 * down
            X1, Y1 = (x1 + 1) * down, (y1 + 1) * down
            best, best_key = -1, None
            for i, b in enumerate(boxes):
                ix = min(X1, b[2]) - max(X0, b[0])
                iy = min(Y1, b[3]) - max(Y0, b[1])
                if ix <= 0 or iy <= 0:
                    continue
                whole = (b[0] <= X0 + down and b[1] <= Y0 + down
                         and X1 - down <= b[2] and Y1 - down <= b[3])
                key = (0, areas[i]) if whole else (1, -ix * iy)
                if best_key is None or key < best_key:
                    best, best_key = i, key
            owner_of[lid] = best
        small = np.where(lab > 0, owner_of[lab], -1).astype(np.int32)
        full = np.full((H, W), -1, np.int32)
        full[:hh * down, :ww * down] = np.repeat(np.repeat(small, down, 0),
                                                 down, 1)
        return full
    except Exception:
        return None


def own_crop(rgba, box, owner=None, index=None):
    """The decal at `box` with every other decal's ink made clear."""
    c = rgba.crop(box)
    if owner is None or index is None:
        return c
    x0, y0, x1, y1 = box
    sub = owner[y0:y1, x0:x1] == index
    if sub.all():
        return c
    arr = np.array(c.convert("RGBA"))
    arr[..., 3] = np.where(sub, arr[..., 3], 0)
    return Image.fromarray(arr, "RGBA")


def redraw_sheet(rgba, refine, native_dpi=300, size_scale=1.0, target_dpi=300,
                 keep_palette=True, palette_colors=16, work_px=1024,
                 min_work_px=640, progress=None, cancelled=None, gap=16,
                 min_side=24, vector_fn=None, limit=None, text_fn=None,
                 reuse_copies=True, stats=None):
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

    vector_fn  optional vector_fn(crop_rgba, palette, w_in, h_in) ->
               (svg_text, raster_rgba) | None. When it returns a drawing
               (e.g. a vision model's SVG), that is used for the decal
               instead of the refine→trace path; None falls back to it.
               The svg is expected in the crop's pixel coordinates with
               width/height in inches; the raster at w_in*target_dpi wide.
    limit      redraw only the first `limit` decals (a preview)
    text_fn    optional text_fn(crop_rgba, palette, w_in, h_in, geom) ->
               (svg_text, raster_rgba) | None, tried FIRST on a decal that
               text_geometry() says is lettering only: the words are read
               and re-set in a real font instead of traced. None falls
               through to vector_fn / the trace.

    Returns None when cancelled, else a dict:
      svg    the whole sheet as one SVG — every decal in its place, width/
             height in inches (size_scale applied), so it prints at size
      rgba   the sheet rasterised at target_dpi (transparent)
      items  one dict per decal: box (in scan px), svg, rgba, size_in"""
    boxes = segment_decals(rgba, gap=gap, min_side=min_side)
    if not boxes:
        return {"svg": None, "rgba": None, "items": []}
    if limit:
        boxes = boxes[:int(limit)]
    W, H = rgba.size
    owner = decal_owner_map(rgba, boxes)
    box_index = {tuple(b): i for i, b in enumerate(boxes)}

    def _own(box):
        return own_crop(rgba, box, owner, box_index.get(tuple(box)))
    k = float(size_scale) * float(target_dpi) / float(native_dpi)  # scan px -> out px
    sheet_w_in = W / float(native_dpi) * size_scale
    sheet_h_in = H / float(native_dpi) * size_scale
    sheet = Image.new("RGBA", (max(1, int(round(W * k))),
                               max(1, int(round(H * k)))), (0, 0, 0, 0))
    parts, items = [], []
    st = stats if stats is not None else {}
    st.setdefault("copies", 0)
    st.setdefault("copy_groups", 0)

    def _geom(box):
        x0, y0, x1, y1 = box
        cw, ch = x1 - x0, y1 - y0
        w_in = cw / float(native_dpi) * size_scale
        h_in = ch / float(native_dpi) * size_scale
        return (cw, ch, w_in, h_in, max(1, int(round(w_in * target_dpi))),
                max(1, int(round(h_in * target_dpi))))

    def _try_text(crop, w_in, h_in):
        """The lettering of a decal set in type, flat or vertical: returns
        (svg in crop px, mask of the lettering pixels, whole) — whole when
        the decal is lettering only — or None. Partly-lettering decals
        (REMOVAL with its stripes and triangle, UNLATCH, RAMP ACCESS) get
        their words typeset; the caller draws the rest."""
        # both ways are scored (RAMP ACCESS, vertical, sits in a box only
        # 1.3x taller than wide and was set sideways)
        tries = [(crop, 0), (crop.rotate(-90, expand=True), -90)]
        # the way the lettering runs is the way more of the ink lines up
        # in rows (RAMP ACCESS: 0.82 turned, 0.52 as is)
        scored = []
        for c2, ang in tries:
            g = text_geometry(c2)
            if g["lines"] and g["share"] >= 0.3:
                scored.append((g["share"], c2, ang, g))
        scored.sort(key=lambda t: -t[0])
        for _sh, c2, ang, g in scored[:1]:   # only the way the lettering runs
            if g["text"]:
                tc, gt = c2, g
            else:
                # the lettering alone, with the rows already found (finding
                # them again on the masked crop lost rows: REMOVAL 0.47)
                arr = np.array(c2.convert("RGBA"))
                arr[..., 3] = np.where(g["mask"], arr[..., 3], 0)
                tc = Image.fromarray(arr, "RGBA")
                gt = dict(g, text=True, share=1.0)
            wi, hi = (w_in, h_in) if ang == 0 else (h_in, w_in)
            got = text_fn(tc, palette_of(tc, colors=palette_colors), wi, hi, gt)
            if cancelled and cancelled():
                return "cancel"
            if got is None:
                continue
            svg_t = got[0]
            mask = g["mask"]
            if ang == -90:
                Hc = float(crop.height)
                wm0 = re.search(r'data-words="[^"]*"', svg_t)
                svg_t = ('<svg xmlns="http://www.w3.org/2000/svg" '
                         + (wm0.group(0) + ' ' if wm0 else '')
                         + f'version="1.1" width="{w_in:.4f}in" '
                         f'height="{h_in:.4f}in" viewBox="0 0 '
                         f'{crop.width} {crop.height}"><g transform='
                         f'"matrix(0 -1 1 0 0 {Hc:.3f})">'
                         + svg_inner(svg_t) + '</g></svg>')
                mask = np.rot90(mask, 1)     # back to the crop's frame
            return svg_t, mask, bool(g["text"])
        return None

    def _draw_one(box, crop=None, allow_text=True):
        """One decal drawn by the chosen method. Returns (svg in crop-pixel
        coordinates, raster, source) or None when cancelled."""
        x0, y0, x1, y1 = box
        crop = _own(box) if crop is None else crop
        cw, ch, w_in, h_in, px_w, px_h = _geom(box)
        if text_fn is not None and allow_text:
            import vector_redraw
            tx = _try_text(crop, w_in, h_in)
            if tx == "cancel":
                return None
            if tx is not None:
                svg_t, mask, whole = tx
                # ink the lettering does not cover (stripes beside a word)
                # is always drawn — even on a decal that is mostly words
                # the rest of the decal (stripes, a triangle) drawn as
                # usual, the typeset words laid over it
                arr = np.array(crop.convert("RGBA"))
                if mask.shape == arr.shape[:2]:
                    arr[..., 3] = np.where(dilate_mask(mask, 3), 0, arr[..., 3])
                rest = Image.fromarray(arr, "RGBA")
                # stripes: perfectly straight bars (user: "the lines under
                # TURRET INFO … should be straight high quality lines")
                bar_parts, bar_mask = straight_bars(rest)
                if bar_parts:
                    arr2 = np.array(rest)
                    arr2[..., 3] = np.where(dilate_mask(bar_mask, 2), 0, arr2[..., 3])
                    rest = Image.fromarray(arr2, "RGBA")
                left = int((np.asarray(rest)[..., 3] > 96).sum())
                total_ink = int((np.asarray(crop.convert("RGBA"))[..., 3] > 96).sum())
                inner = "".join(bar_parts)
                # what is left (a triangle, a box) drawn as usual — a few
                # specks round typeset words are not worth a drawing
                if left >= max(20, int(0.04 * total_ink)):
                    drawn = _draw_one(box, crop=rest, allow_text=False)
                    if drawn is None:
                        return None
                    inner += svg_inner(drawn[0])
                inner += svg_inner(svg_t)
                wm = re.search(r'data-words="[^"]*"', svg_t)
                svg = ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
                       + (wm.group(0) + ' ' if wm else '')
                       + f'width="{w_in:.4f}in" height="{h_in:.4f}in" '
                       f'viewBox="0 0 {crop.width} {crop.height}">'
                       + inner + '</svg>')
                return svg, vector_redraw.render_svg(svg, px_w), "text"
        # straight-line art (a GI JOE banner: block letters, a striped
        # flag) is rebuilt from straightened outlines — no service traces
        # a ragged scan edge straight (v2.24.3, user: "no straight lines
        # on the letters or flag")
        try:
            # the clustered palette (one entry per ink; palette_of split
            # the banner's red and navy into near-twins)
            import recraft_vectorize
            gpal = recraft_vectorize.decal_palette(crop)
            if gpal is None or len(gpal) == 0:
                gpal = palette_of(crop, colors=palette_colors)
            geo = geometric_svg(crop, gpal, float(native_dpi) / 25.4,
                                w_in, h_in)
        except Exception:
            geo = None
        if geo is not None:
            import vector_redraw
            st["geometric"] = st.get("geometric", 0) + 1
            return geo, vector_redraw.render_svg(geo, px_w), "geometric"
        if vector_fn is not None:
            # a drawing from a description or a vectorizing service
            got = vector_fn(crop, palette_of(crop, colors=palette_colors),
                            w_in, h_in)
            if cancelled and cancelled():
                return None                     # stop now, no trace fallback
            if got is not None:
                return got[0], got[1], "vector"
        # the work canvas: about 4x the scan crop, clamped to [min_work_px,
        # work_px] on the long edge (diffusion models draw badly on tiny
        # canvases and invent on huge ones), both sides multiples of 8,
        # aspect kept. The enlargement itself happens in refine (engine-side
        # RealESRGAN when available), so the crop goes over at native size.
        long = float(max(cw, ch))
        sc = min(float(work_px), max(float(min_work_px), long * 4.0)) / long
        wr = max(64, int(round(cw * sc / 8)) * 8)
        hr = max(64, int(round(ch * sc / 8)) * 8)
        on_white = Image.alpha_composite(
            Image.new("RGBA", crop.size, (255, 255, 255, 255)), crop).convert("RGB")
        out = refine(on_white, (wr, hr)) if refine is not None \
            else on_white.resize((wr, hr), Image.LANCZOS)
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
        inner = erode_mask(sil, band_r)     # not MinFilter: see dilate_mask

        def _light(arr):
            return (arr.mean(2) >= 200) & ((arr.max(2) - arr.min(2)) < 40)

        ai = np.asarray(out).astype(np.int32)
        light = _light(ai)
        pal = palette_of(crop, colors=palette_colors)
        if keep_palette and pal is not None:
            out = snap_palette(out, pal)
            light |= _light(np.asarray(out).astype(np.int32))
        # …but WHITE INK is light too: where the scan itself is light the
        # pixel is ink, not fringe (thin white text used to vanish here)
        scan_light = _light(np.asarray(on_white.resize((wr, hr), Image.BILINEAR))
                            .astype(np.int32))
        keep = sil.copy()
        keep[(sil & ~inner) & light & ~scan_light] = False
        redrawn = out.convert("RGBA")
        redrawn.putalpha(Image.fromarray((keep * 255).astype(np.uint8)))
        # cutout: shapes with real holes, so dropping the backing exposes
        # no ink painted underneath (stacked layers left a dark wash over
        # an orca whose ring encloses clear film)
        svg, ras = vectorize(redrawn, target_px=px_w, quantize_colors=0,
                             presmooth=False, drop_halo=False,
                             hierarchical="cutout")
        # into crop-pixel coordinates like the other methods
        svg_c = ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
                 f'width="{w_in:.4f}in" height="{h_in:.4f}in" '
                 f'viewBox="0 0 {cw} {ch}"><g transform="scale({cw / wr:.6f} '
                 f'{ch / hr:.6f})">{svg_inner(svg)}</g></svg>')
        return svg_c, ras, "trace"

    def _fit(drawn, box, how=""):
        """A drawing (svg in its OWN crop-pixel coordinates) made to fit
        `box`: scaled to it, turned 180 degrees or mirrored for such a
        copy. Returns (own svg in box pixels, inner for the sheet with
        its scale, raster at the box's print size)."""
        svg, ras, _src = drawn
        cw, ch, w_in, h_in, px_w, px_h = _geom(box)
        vb = re.search(r'viewBox="\s*[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)', svg)
        bw, bh = (float(vb.group(1)), float(vb.group(2))) if vb else (cw, ch)
        inner = svg_inner(svg)
        if how == "turn":
            inner = f'<g transform="rotate(180 {bw / 2:.3f} {bh / 2:.3f})">{inner}</g>'
            ras = ras.rotate(180)
        elif how == "mirror":
            inner = f'<g transform="translate({bw:.3f} 0) scale(-1 1)">{inner}</g>'
            ras = ras.transpose(Image.FLIP_LEFT_RIGHT)
        if ras.size != (px_w, px_h):
            ras = ras.resize((px_w, px_h), Image.LANCZOS)
        sx, sy = cw / bw, ch / bh
        own = ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
               f'width="{w_in:.4f}in" height="{h_in:.4f}in" '
               f'viewBox="0 0 {cw} {ch}"><g transform="scale({sx:.6f} {sy:.6f})">'
               f'{inner}</g></svg>')
        return own, inner, ras, sx, sy

    def _place(drawn, box, source, how=""):
        """Put a drawing at `box` (see _fit) on the sheet and in the items."""
        x0, y0, x1, y1 = box
        cw, ch, w_in, h_in, px_w, px_h = _geom(box)
        own, inner, ras, sx, sy = _fit(drawn, box, how)
        # the sheet is put together at the end (_compose), after typeset
        # lettering has been carried over to its look-alike copies
        items.append(dict(box=(x0, y0, x1, y1), svg=own, rgba=ras,
                          size_in=(w_in, h_in), source=source,
                          _inner=inner, _sx=sx, _sy=sy, _drawn=drawn))

    def _compose():
        for it in items:
            x0, y0 = it["box"][0], it["box"][1]
            px, py = int(round(x0 * k)), int(round(y0 * k))
            part = it["rgba"]
            if px + part.width > sheet.width or py + part.height > sheet.height:
                part = part.crop((0, 0, max(1, min(part.width, sheet.width - px)),
                                  max(1, min(part.height, sheet.height - py))))
            if px < sheet.width and py < sheet.height:
                sheet.alpha_composite(part, (px, py))
            parts.append(f'<g transform="translate({x0} {y0}) scale('
                         f'{it["_sx"]:.6f} {it["_sy"]:.6f})">{it["_inner"]}</g>')
            for key in ("_inner", "_sx", "_sy", "_drawn"):
                it.pop(key, None)

    def _carry_text():
        """A word typeset on one decal is set on its look-alikes that were
        not typeset themselves (worn AWAYs the model would not read, or
        copies in another copy group): same size within 8%, scans that
        correlate (0.6+, straight or turned), and the typeset word must
        fit THAT decal's scan (overlap 0.7+, colour within 110)."""
        import vector_redraw
        typeset = [it for it in items if it["source"] == "text"]
        if not typeset:
            return 0
        n = 0
        # one word, one look: typeset decals with the same words and size
        # share the version that fits them all best
        def _words(it):
            m = re.search(r'data-words="([^"]*)"', it["_drawn"][0] or "")
            return m.group(1) if m else None
        groups_w = {}
        # every decal carrying typeset words — group copies placed from a
        # typeset drawing too (they are marked "copy")
        for it in items:
            w_ = _words(it)
            if w_:
                groups_w.setdefault(w_, []).append(it)
        for w_, grp in groups_w.items():
            if len(grp) < 2:
                continue
            best_c, best_tot = None, -1e9
            for cand in grp:
                tot, fits = 0.0, []
                for it in grp:
                    b = it["box"]
                    cb = cand["box"]
                    if abs((b[2] - b[0]) - (cb[2] - cb[0])) > 0.08 * max(b[2] - b[0], cb[2] - cb[0]):
                        tot -= 1.0
                        continue
                    try:
                        own, inner, ras, sx, sy = _fit(cand["_drawn"], b, "")
                        _ok, iou, col = vector_redraw.check_against_scan(own, _own(b))
                    except Exception:
                        iou, col = 0.0, 999.0
                    tot += iou - col / 400.0
                if tot > best_tot:
                    best_c, best_tot = cand, tot
            for it in grp:
                if it is best_c:
                    continue
                b = it["box"]
                try:
                    own, inner, ras, sx, sy = _fit(best_c["_drawn"], b, "")
                    _ok, iou, col = vector_redraw.check_against_scan(own, _own(b))
                except Exception:
                    continue
                if iou >= 0.7 and col <= 110:
                    it.update(svg=own, rgba=ras, _inner=inner, _sx=sx, _sy=sy,
                              _drawn=best_c["_drawn"])
        for it in items:
            if it["source"] == "text":
                continue
            b = it["box"]
            w, h = b[2] - b[0], b[3] - b[1]
            best = None
            for t in typeset:
                tb = t["box"]
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
                if abs(w - tw) > 0.08 * max(w, tw) or abs(h - th) > 0.08 * max(h, th):
                    continue
                try:
                    f = 48.0 / max(w, h)
                    size = (max(8, int(round(w * f))), max(8, int(round(h * f))))
                    sa = _copy_sig(rgba, b, size, _own)
                    sb = _copy_sig(rgba, tb, size, _own)
                    v, how = max((_ncc(sa, sb), ""),
                                 (_ncc(sa, sb[::-1, ::-1].copy()), "turn"))
                except Exception:
                    continue
                if v < 0.6:
                    continue
                try:
                    own, inner, ras, sx, sy = _fit(t["_drawn"], b, how)
                    _ok, iou, col = vector_redraw.check_against_scan(own, _own(b))
                except Exception:
                    continue
                if iou >= 0.7 and col <= 110:
                    sc = iou - col / 400.0
                    if best is None or sc > best[0]:
                        best = (sc, own, inner, ras, sx, sy, t["_drawn"])
            if best is not None:
                _sc, own, inner, ras, sx, sy, drawn = best
                it.update(svg=own, rgba=ras, source="copy", _inner=inner,
                          _sx=sx, _sy=sy, _drawn=drawn)
                n += 1
        return n

    # repeated decals: draw a few copies, keep the best, place it on all
    groups = (find_copies(rgba, boxes, crop_fn=_own)
              if (reuse_copies and not limit) else [])
    member_of = {}
    for g in groups:
        for idx, rot in g:
            member_of[idx] = g
    done = set()
    n_draw = len(boxes)
    step = 0
    for i, box in enumerate(boxes):
        if i in done:
            continue
        if cancelled and cancelled():
            return None
        g = member_of.get(i)
        if not g:
            if progress:
                progress(step, n_draw)
            step += 1
            drawn = _draw_one(box)
            if drawn is None:
                return None
            _place(drawn, box, drawn[2])
            done.add(i)
            continue
        # a group: up to 3 candidates drawn, scored against their own scan
        import vector_redraw

        def _score(svg, box):
            try:
                _ok, iou, col = vector_redraw.check_against_scan(svg, _own(box))
                return iou - col / 400.0, iou, col
            except Exception:
                return 0.0, 0.0, 999.0

        cands = {}
        best, best_score, best_how = None, -1e9, ""
        for idx, how in g[:3]:
            if progress:
                progress(step, n_draw)
            step += 1
            drawn = _draw_one(boxes[idx])
            if drawn is None:
                return None
            cands[idx] = (drawn, how)
            sc = _score(drawn[0], boxes[idx])[0]
            if drawn[2] == "text":
                # clean type beats a trace of halftone lettering when it
                # passed its own check (the group then all reads the same)
                sc += 0.25
            if sc > best_score:
                best, best_score, best_how = drawn, sc, how
        rel = {"": {"": "", "turn": "turn", "mirror": "mirror"},
               "turn": {"": "turn", "turn": "", "mirror": "mirror"},
               "mirror": {"": "mirror", "turn": "mirror", "mirror": ""}}
        reused = 0
        for idx, how in g:
            # the best drawing turned/mirrored from ITS orientation to this one's
            t = rel[best_how][how] if best_how in rel and how in rel[best_how] else ""
            own, _inner, _ras, _sx, _sy = _fit(best, boxes[idx], t)
            _sc, iou, col = _score(own, boxes[idx])
            # never swap in a different design: the best drawing must sit on
            # THIS copy's scan as well as a drawing should (else its own)
            # a typeset word is accepted at the text sweep's own level (the
            # same word sits a little differently on each halftone scan:
            # 4 of 10 DANGER! copies refused it at the graphics level)
            need_iou, max_col = (0.65, 110) if best[2] == "text" else (0.75, 90)
            if best is not None and iou >= need_iou and col <= max_col:
                is_self = cands.get(idx, (None,))[0] is best
                _place(best, boxes[idx], best[2] if is_self else "copy", how=t)
                if not is_self:
                    reused += 1
            else:
                drawn = cands.get(idx, (None,))[0]
                if drawn is None:
                    if progress:
                        progress(step, n_draw)
                    step += 1
                    drawn = _draw_one(boxes[idx])
                    if drawn is None:
                        return None
                _place(drawn, boxes[idx], drawn[2])
            done.add(idx)
        step += max(0, len(g) - 3)
        st["copy_groups"] += 1
        st["copies"] += reused
    try:
        st["text_carried"] = st.get("text_carried", 0) + _carry_text()
    except Exception:
        pass
    _compose()
    sheet_svg = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
                 f'width="{sheet_w_in:.4f}in" height="{sheet_h_in:.4f}in" '
                 f'viewBox="0 0 {W} {H}">\n' + "\n".join(parts) + "\n</svg>\n")
    return {"svg": sheet_svg, "rgba": sheet, "items": items}


# ---------------------------------------------------------------- pipeline
def _label(mask):
    """Label the 8-connected regions of a small boolean grid: returns
    (labels int32 array, 0 = background; count). Plain breadth-first
    search — meant for a reduced grid of a few hundred thousand cells."""
    H, W = mask.shape
    labels = np.zeros((H, W), np.int32)
    n = 0
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if labels[sy, sx]:
            continue
        n += 1
        labels[sy, sx] = n
        stack = [(sy, sx)]
        while stack:
            y, x = stack.pop()
            for dy in (-1, 0, 1):
                yy = y + dy
                if yy < 0 or yy >= H:
                    continue
                for dx in (-1, 0, 1):
                    xx = x + dx
                    if xx < 0 or xx >= W or labels[yy, xx] or not mask[yy, xx]:
                        continue
                    labels[yy, xx] = n
                    stack.append((yy, xx))
    return labels, n


def _drop_border_fringe(alpha, down=4):
    """Clear opaque pieces that are the table showing at the picture's
    edge — the strip or crescent a photo crop leaves along a bowed sheet
    edge. A piece goes when it HUGS the border (it touches the edge along
    at least half its length — a decal that merely reaches the edge
    touches it at one end), is long (over 20% of the picture) and is
    either thin (short side under 10%) or sparse (fills under 30% of its
    own box: a curved sliver). A printed block that reaches the edge is
    far thicker and fills its box; the small stickers beside a sliver are
    separate pieces and are never touched."""
    H, W = alpha.shape
    m = alpha > 96
    hh, ww = max(1, H // down), max(1, W // down)
    small = m[:hh * down, :ww * down].reshape(hh, down, ww, down).any(axis=(1, 3))
    labels, n = _label(small)
    if not n:
        return alpha
    edge = np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]])
    edge = edge[edge > 0]
    if edge.size == 0:
        return alpha
    edge_count = np.bincount(edge, minlength=n + 1)
    out = alpha.copy()
    kill = np.zeros_like(small)
    for i in np.nonzero(edge_count)[0].tolist():
        cells = labels == i
        ys, xs = np.nonzero(cells)
        wc, hc = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
        w, h = wc * down, hc * down
        fill = cells.sum() / float(wc * hc)
        hug = edge_count[i] >= 0.5 * max(wc, hc)
        long_ = max(w, h) > 0.2 * max(H, W)
        thin = min(w, h) < 0.10 * min(H, W)
        sparse = fill < 0.3
        if hug and long_ and (thin or sparse):
            kill |= cells
    if kill.any():
        full = np.zeros((H, W), bool)
        full[:hh * down, :ww * down] = np.repeat(np.repeat(kill, down, 0), down, 1)
        out[full] = 0
    return out


def process_image(img, mode="cleanup", remove_bg=True, carrier=None,
                  denoise=2, tol=52, target_dpi=600, native_dpi=300,
                  do_trim=False, size_scale=1.0, remove_lines=True,
                  balance=True, exact=False, tidy_matte=True, solidify=False,
                  smooth=False, photo=False, fill_holes=False):
    """Run one image through the pipeline. Returns a dict with 'rgba' (and
    'svg' for vector mode). size_scale rescales the result for a different
    figure scale (e.g. 1.5 to take a 3.75\" decal to 1/12 Classified).

    exact=True is the pixel-faithful path: the art's RGB is left BYTE-IDENTICAL
    to the scan (no denoise, no sharpen, no colour change) — only the alpha
    (background) is computed, and the only RGB change allowed is removing the
    scanner streak lines when remove_lines is on. Use it when fidelity must be
    perfect.

    photo=True is for a photographed sheet that prepare_photo has already
    flattened and set to white: the backing IS white (whatever the border
    holds), and thin dark strips along the picture edge — the table the
    crop could not shed — are dropped."""
    orig = _auto_orient(img).convert("RGB")
    if carrier is None:
        carrier = PHOTO_WHITE if photo else detect_carrier(orig)
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
        if photo:
            alpha = _drop_border_fringe(alpha)
        rgba = Image.fromarray(
            np.dstack([np.asarray(cleaned.convert("RGB")), alpha]), "RGBA")
        if tidy_matte:
            rgba = clean_matte(rgba)   # drop the faint carrier halo + speckle
        if fill_holes:
            # on a white-keyed sheet, a clear region shut inside a decal
            # was white ink/backing: make it white again (down to 0.3 mm —
            # the dashes on a gauge; letter counters are told apart by
            # their size against the letter)
            side = max(4, int(round(0.3 / 25.4 * max(72, native_dpi))))
            tinted = None if (photo or is_neutral_carrier(carrier)) else carrier
            rgba, holes = fill_enclosed_holes(rgba, min_side=side,
                                              min_area=int(1.5 * side * side),
                                              carrier=tinted,
                                              # a piece wider than 15 mm both
                                              # ways is no letter: its holes
                                              # are windows
                                              letter_max_px=int(round(
                                                  15 / 25.4 * max(72, native_dpi))))
    else:
        rgba = cleaned.convert("RGBA")
    if do_trim:
        rgba = trim(rgba)
    out = {"svg": None, "holes": locals().get("holes", 0)}
    if mode == "vector":
        # target pixels from the requested print DPI vs the scan's native DPI
        scale = max(1.0, float(target_dpi) / max(72, native_dpi))
        target_px = int(rgba.width * scale)
        pw, ph = rgba.size
        # the faithful trace: the scan's own palette, enlarged, hard edges
        # (the old 16-colour quantise of the whole page — mostly film —
        # turned red to maroon, dropped white ink as "halo" and lost thin
        # letters to the speckle filter: v2.22.7)
        out["svg"], rgba = trace_sheet(rgba, target_px=target_px)
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
