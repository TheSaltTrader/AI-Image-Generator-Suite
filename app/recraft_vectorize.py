"""Recraft Vectorize through fal.ai — a dedicated bitmap-to-SVG service, as
one more 'Redraw to vector' method.

fal.ai is pay-as-you-go (prepaid credits, no subscription); the model costs
about $0.01 per image. Endpoint (synchronous):

    POST https://fal.run/fal-ai/recraft/vectorize
    Authorization: Key <FAL_KEY>
    {"image_url": "<URL or data:image/png;base64,...>"}
 -> {"image": {"url": "<…svg>", "content_type": "image/svg+xml", …}}

Input limits: PNG/JPG/WEBP, < 5 MB, < 16 MP, longest side < 4096 px,
shortest side > 256 px. Each decal is sent on its own: enlarged (or
shrunk) to fit, on a pure magenta backing that is removed from the answer
so the decal stays transparent.

The key is stored ONLY in the Windows Credential Manager
('AIImageGeneratorSuite/fal'); FAL_KEY in the environment wins. It never
goes into settings, logs, the repo or a release.
"""
import base64
import io
import os
import re

import numpy as np
from PIL import Image

import vector_redraw

ENDPOINT = "https://fal.run/fal-ai/recraft/vectorize"
CRED_TARGET = "AIImageGeneratorSuite/fal"
PRICE_PER_IMAGE = 0.01
KEY_RGB = (255, 0, 255)            # the backing, removed from the answer
MIN_SIDE, MAX_SIDE = 320, 2048     # what is sent (fal: >256, <4096)


def get_key():
    return (os.environ.get("FAL_KEY") or vector_redraw.cred_read(CRED_TARGET)
            or "").strip() or None


def set_key(key):
    return vector_redraw.cred_write(CRED_TARGET, (key or "").strip())


def decal_palette(crop_rgba):
    """The decal's own inks (the scan's colours, near-white = white ink)."""
    import decals
    # merge 60 / min_share 2%: the light blends of a halftone red (pinks)
    # are not inks of their own — they made Recraft trace 790 shapes on
    # one DANGER! (v2.23.3)
    return decals.palette_of(crop_rgba.convert("RGBA"), colors=10, merge=60,
                             min_share=0.02, erode=1)


def _prepare(crop_rgba, pal=None):
    """The decal FLATTENED (its own inks only, crisp edges, no halftone
    mottle — decals.flatten_decal) on magenta, sized for the service.
    Sending the raw scan made Recraft trace the noise: wobbly banner
    edges, 1,000+ shapes with gradients on a DANGER!, an invented grey
    fill (v2.23.3). Returns (png bytes, scale = sent px per crop px)."""
    import decals
    crop = crop_rgba.convert("RGBA")
    W, H = crop.size
    # enlarge small decals (more detail to trace), keep inside the limits
    s = max(MIN_SIDE / float(min(W, H)), min(4.0, MAX_SIDE / float(max(W, H))))
    s = min(s, MAX_SIDE / float(max(W, H)))
    nw, nh = max(1, int(round(W * s))), max(1, int(round(H * s)))
    if pal is None:
        pal = decal_palette(crop)
    big = decals.flatten_decal(crop, pal, size=(nw, nh),
                               band=max(1, int(round(s))))
    a = np.asarray(big)
    hard = a[..., 3] >= 128
    rgb = np.empty(a.shape[:2] + (3,), np.uint8)
    rgb[...] = KEY_RGB
    rgb[hard] = a[..., :3][hard]
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue(), nw / float(W)


_FILL = re.compile(r'fill\s*[:=]\s*"?\s*(#[0-9a-fA-F]{3,6}|rgb\([^)]*\))', re.I)


def _rgb_of(value):
    v = value.strip().lower()
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        try:
            return tuple(int(h[k:k + 2], 16) for k in (0, 2, 4))
        except ValueError:
            return None
    m = re.match(r"rgb\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)", v)
    if m:
        return tuple(int(float(x)) for x in m.groups())
    return None


def _is_key(rgb):
    r, g, b = rgb
    return r >= 180 and b >= 180 and g <= 110


def _gradient_colours(svg_text):
    """{gradient id: the mean colour of its stops}."""
    out = {}
    for gid, body in re.findall(
            r'<(?:linear|radial)Gradient\b[^>]*\bid="([^"]+)"[^>]*>(.*?)</(?:linear|radial)Gradient>',
            svg_text, flags=re.S | re.I):
        cols = [c for c in (_rgb_of(v) for v in re.findall(
            r'stop-color\s*[:=]\s*"?\s*(#[0-9a-fA-F]{3,6}|rgb\([^)]*\))', body, re.I))
            if c is not None]
        if cols:
            out[gid] = tuple(int(round(sum(c[k] for c in cols) / len(cols)))
                             for k in range(3))
    return out


def clean_svg(svg_text, sent_w, sent_h, scale, pal=None, keep_key=False):
    """Make Recraft's answer a decal: every fill — gradients included,
    by the mean of their stops — becomes the nearest of the decal's own
    inks or the magenta backing; backing shapes are dropped; the drawing
    is mapped back onto the crop (viewBox in crop pixels)."""
    grads = _gradient_colours(svg_text)
    P = [tuple(int(v) for v in c) for c in pal] if pal is not None else []

    def snap(c):
        if not P:
            return c, _is_key(c)
        cands = P + [KEY_RGB]
        best = min(cands, key=lambda q: sum((q[k] - c[k]) ** 2 for k in range(3)))
        return best, best == KEY_RGB

    def drop(m):
        tag = m.group(0)
        g = re.search(r'fill\s*=\s*"url\(#([^)"]+)\)"', tag)
        c = None
        if g:
            c = grads.get(g.group(1))
        else:
            f = _FILL.search(tag)
            if f:
                c = _rgb_of(f.group(1))
        if c is None:
            return tag
        new, is_key = snap(c)
        if is_key or _is_key(c):
            if not keep_key:
                return ""
            new = KEY_RGB
        hexc = "#%02x%02x%02x" % new
        tag = re.sub(r'fill\s*=\s*"[^"]*"', f'fill="{hexc}"', tag, count=1)
        tag = re.sub(r'fill\s*:\s*[^;"]+', f'fill:{hexc}', tag)
        return tag
    body = re.sub(r"<(path|rect|polygon|circle|ellipse)\b[^>]*?/>", drop,
                  svg_text, flags=re.S | re.I)
    body = re.sub(r"<(path|rect|polygon|circle|ellipse)\b[^>]*?>\s*</\1>", drop,
                  body, flags=re.S | re.I)
    body = re.sub(r"<defs\b.*?</defs>", "", body, flags=re.S | re.I)
    inner = _inner(body)
    vb = _viewbox(svg_text) or (0.0, 0.0, float(sent_w), float(sent_h))
    cw, ch = sent_w / scale, sent_h / scale
    # the service's own viewBox → crop pixels
    sx, sy = cw / vb[2], ch / vb[3]
    return ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{cw:.2f}" height="{ch:.2f}" viewBox="0 0 {cw:.3f} {ch:.3f}">'
            f'<g transform="scale({sx:.6f} {sy:.6f}) translate({-vb[0]:.3f} {-vb[1]:.3f})">'
            f'{inner}</g></svg>')


def finalize(svg_text, sent_w, sent_h, scale, pal=None, crop=None, sent=None):
    """Recraft paints in layers: a clear window inside a decal is a
    backing-coloured shape ON TOP of the ink beneath (the orca's ellipse
    was a black ellipse with a magenta one over it). Removing the backing
    shapes would expose that ink — so: render the answer with the
    backing kept and with it removed; if removing it exposes paint, the
    decal is traced again locally from Recraft's own clean render (flat
    regions, no stacking); otherwise Recraft's shapes are kept as they
    are. (MuPDF ignores SVG masks, so a mask is not an option.)"""
    import decals
    keyed = clean_svg(svg_text, sent_w, sent_h, scale, pal, keep_key=True)
    clean = clean_svg(svg_text, sent_w, sent_h, scale, pal, keep_key=False)
    w = max(64, min(1600, int(round(sent_w))))
    rk = np.asarray(vector_redraw.render_svg(keyed, w))
    rc = np.asarray(vector_redraw.render_svg(clean, w))
    if rk.shape != rc.shape:
        return clean
    r_, g_, b_ = (rk[..., 0].astype(int), rk[..., 1].astype(int),
                  rk[..., 2].astype(int))
    # the backing AND its anti-aliased blends with an ink (black+magenta =
    # purple): red and blue both well above green. Reds (B < G) and
    # whites/greys (equal) are not caught. The blends snapped to a brown
    # or white rim along the outlines.
    key = ((r_ - g_ > 50) & (b_ - g_ > 50)) & (rk[..., 3] > 128)
    exposed = key & (rc[..., 3] > 128)
    # …and wherever the picture SENT was the clear backing, the answer
    # must be clear too: Recraft fills a gap between shapes with a blend
    # of the backing and the ink beside it (snapped to the ink)
    if sent is not None:
        sm = np.asarray(sent.convert("RGB").resize((rc.shape[1], rc.shape[0]),
                                                   Image.NEAREST)).astype(int)
        was_key = (sm[..., 0] >= 200) & (sm[..., 1] <= 70) & (sm[..., 2] >= 200)
        # ignore a 2-px band at edges (anti-aliasing)
        import decals as _d
        was_key = _d.erode_mask(was_key, 2)
        exposed = exposed | (was_key & (rc[..., 3] > 128))
    if exposed.sum() <= 0.002 * key.size:
        return clean
    # re-trace from the keyed render. WHICH areas are clear comes from the
    # scan's own outline (smoothed), not from Recraft's layers: Recraft
    # fills a gap between shapes with a blend of the backing and the ink
    # beside it, which snaps to the ink (a black wedge between the orca
    # and its ring). The colours come from Recraft's clean render.
    from PIL import ImageFilter
    a = rk.copy()
    if crop is not None:
        sa = crop.convert("RGBA").split()[3].resize((a.shape[1], a.shape[0]),
                                                    Image.LANCZOS)
        sa = sa.filter(ImageFilter.GaussianBlur(max(1.0, w / 800.0)))
        inside = np.asarray(sa) >= 128
    else:
        inside = ~(key | (rk[..., 3] < 128))
    # clear where EITHER says clear: the scan (a gap Recraft filled) or
    # Recraft (a sliver of backing the scan's wider outline kept — it
    # snapped to a brown/white rim)
    # and where Recraft drew nothing at all, nothing is filled in (a wrong
    # answer must not be completed into a right-looking one)
    a[..., 3] = np.where(inside & ~key & (rk[..., 3] > 128), 255, 0).astype(np.uint8)
    rgba = Image.fromarray(a, "RGBA")
    if pal is not None:
        # the band of edge blends is as wide as the enlargement (black-to-
        # backing anti-aliasing otherwise snapped to a red-brown rim)
        band = max(2, int(round(rgba.width / float(max(1, crop.width)))))            if crop is not None else 2
        rgba = decals.flatten_decal(rgba, pal, s=1, band=band,
                                    drop_orphans=True)
    # cutout: shapes with real holes, not stacked layers — dropping the
    # backing then exposes nothing (stacked painted a black base under the
    # gap, and the gap came out black again)
    svg2, _ras = decals.vectorize(rgba, target_px=w, quantize_colors=0,
                                  presmooth=False, drop_halo=False,
                                  filter_speckle=4, hierarchical="cutout")
    cw, ch = sent_w / scale, sent_h / scale
    sx = cw / float(rgba.width)
    sy = ch / float(rgba.height)
    return ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{cw:.2f}" height="{ch:.2f}" viewBox="0 0 {cw:.3f} {ch:.3f}">'
            f'<g transform="scale({sx:.6f} {sy:.6f})">{_inner(svg2)}</g></svg>')


def _inner(svg_text):
    m = re.search(r"<svg\b[^>]*>(.*)</svg>", svg_text, re.S | re.I)
    return m.group(1) if m else ""


def _viewbox(svg_text):
    m = re.search(r"<svg\b[^>]*>", svg_text, re.S | re.I)
    if not m:
        return None
    tag = m.group(0)
    vb = re.search(r'viewBox\s*=\s*"([^"]+)"', tag)
    if vb:
        try:
            x, y, w, h = [float(t) for t in re.split(r"[\s,]+", vb.group(1).strip())]
            if w > 0 and h > 0:
                return (x, y, w, h)
        except ValueError:
            pass
    wm = re.search(r'\swidth\s*=\s*"([\d.]+)', tag)
    hm = re.search(r'\sheight\s*=\s*"([\d.]+)', tag)
    if wm and hm:
        return (0.0, 0.0, float(wm.group(1)), float(hm.group(1)))
    return None


class FalError(RuntimeError):
    pass


def _detail(r):
    """fal.ai's own reason ({"detail": ...}), short."""
    try:
        d = r.json().get("detail")
        if d:
            return str(d)[:160]
    except Exception:
        pass
    return (r.text or f"HTTP {r.status_code}")[:160]


def vectorize_decal(crop_rgba, key, timeout=180.0, session=None):
    """One decal through Recraft Vectorize. Returns the SVG in crop-pixel
    coordinates. Raises FalError with an 'account' flag in the message
    for key / balance problems (the run then stops)."""
    import requests
    http = session or requests
    pal = decal_palette(crop_rgba)
    png, scale = _prepare(crop_rgba, pal)
    if len(png) >= 5 * 1024 * 1024:
        raise FalError("the decal picture is over 5 MB")
    sent_w = int(round(crop_rgba.width * scale))
    sent_h = int(round(crop_rgba.height * scale))
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    headers = {"Authorization": f"Key {key}",
               "Content-Type": "application/json"}
    try:
        r = http.post(ENDPOINT, json={"image_url": data_uri}, headers=headers,
                      timeout=timeout)
    except Exception as e:
        # fal.ai refuses an account straight away and drops the connection
        # while a big picture is still uploading (seen as an SSL EOF):
        # ask again with a tiny picture to learn the real answer
        try:
            tiny = io.BytesIO()
            Image.new("RGB", (300, 300), KEY_RGB).save(tiny, format="PNG")
            r = http.post(ENDPOINT, headers=headers, timeout=60, json={
                "image_url": "data:image/png;base64,"
                + base64.b64encode(tiny.getvalue()).decode("ascii")})
        except Exception:
            raise FalError(f"could not reach fal.ai: {e.__class__.__name__}")
        if r.status_code == 200:
            raise FalError(f"the upload was cut off ({e.__class__.__name__})")
    if r.status_code in (401, 403):
        detail = _detail(r)
        if "lock" in detail.lower():
            raise FalError("account: fal.ai says the account is locked ("
                           + detail + ") — open fal.ai/dashboard: add credits "
                           "or finish the account check there, then retry")
        raise FalError("account: fal.ai refused the key (" + detail + ") — "
                       "check it with 🔑 fal key…")
    if r.status_code in (402,) or (r.status_code >= 400 and any(
            w in (r.text or "").lower() for w in ("balance", "credit",
                                                  "insufficient", "billing"))):
        raise FalError("account: fal.ai refused the call (credits used up?) — "
                       "top up at fal.ai/dashboard/billing")
    if r.status_code != 200:
        raise FalError(f"fal.ai answered {r.status_code}: {(r.text or '')[:200]}")
    try:
        url = r.json()["image"]["url"]
    except Exception:
        raise FalError("fal.ai's answer had no SVG")
    if url.startswith("data:"):
        svg = base64.b64decode(url.split(",", 1)[1]).decode("utf-8", "replace")
    else:
        g = http.get(url, timeout=timeout)
        if g.status_code != 200:
            raise FalError(f"could not fetch the SVG ({g.status_code})")
        svg = g.text
    if "<svg" not in svg:
        raise FalError("the result is not an SVG")
    return finalize(svg, sent_w, sent_h, scale, pal, crop=crop_rgba,
                    sent=Image.open(io.BytesIO(png)))


def make_vector_fn(key, target_dpi, stats=None, cancelled=None, log=None,
                   session=None):
    """The `vector_fn` for decals.redraw_sheet (same contract as the
    vision model's): returns (svg, raster) or None to fall back to the
    clean trace. Account problems raise so the run stops; a model call in
    flight is abandoned on Cancel. `stats`: calls / ok / fallback / cost."""
    import decals
    st = stats if stats is not None else {}
    for k in ("calls", "ok", "fallback"):
        st.setdefault(k, 0)
    st.setdefault("cost", 0.0)

    def vector_fn(crop, pal, w_in, h_in):
        if cancelled and cancelled():
            return None
        try:
            svg = vector_redraw.call_cancellable(
                lambda: vectorize_decal(crop, key, session=session), cancelled)
        except vector_redraw.Cancelled:
            return None
        except FalError as e:
            if str(e).startswith("account:"):
                raise RuntimeError(str(e)[len("account:"):].strip())
            if log:
                log("Recraft vectorize failed; clean trace used: %s" % e)
            st["fallback"] += 1
            return None
        except Exception as e:
            if log:
                log("Recraft vectorize call failed; clean trace used: %r" % (e,))
            st["fallback"] += 1
            return None
        st["calls"] += 1
        st["cost"] += PRICE_PER_IMAGE
        ok, iou, col = vector_redraw.check_against_scan(svg, crop)
        if not ok:
            if log:
                log(f"Recraft result rejected (overlap {iou:.2f}, colour "
                    f"{col:.0f}); clean trace used")
            st["fallback"] += 1
            return None
        svg = decals.svg_set_physical_size(svg, w_in, h_in)
        ras = vector_redraw.render_svg(svg, max(1, int(round(w_in * target_dpi))))
        st["ok"] += 1
        return svg, ras

    return vector_fn
