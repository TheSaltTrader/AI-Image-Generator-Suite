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
THIN_MAX_SIDE = 4000                # a long thin decal's long side may reach this


def get_key():
    return (os.environ.get("FAL_KEY") or vector_redraw.cred_read(CRED_TARGET)
            or "").strip() or None


def set_key(key):
    return vector_redraw.cred_write(CRED_TARGET, (key or "").strip())


def _kmeans_palette(crop_rgba, k=10, sample=30000, merge=40, min_share=0.004):
    """The decal's inks by k-means over its solid pixels (2 px inside the
    outline). Median-cut spent its slots on near-identical blacks and
    mixed a small red brush (2.4% of an orca decal) into a muddy brown;
    k-means with k-means++ seeding keeps it. Clusters are ordered by
    size, merged within `merge`, dropped under `min_share`; near-white
    becomes pure white (white ink). Returns (N, 3) uint8 or None."""
    import decals
    a = np.asarray(crop_rgba.convert("RGBA"))
    solid = decals.erode_mask(a[..., 3] >= 250, 2)
    if solid.sum() < 50:
        solid = a[..., 3] >= 250
    px = a[..., :3][solid].astype(np.float32)
    if len(px) < 10:
        return None
    rng = np.random.default_rng(7)
    if len(px) > sample:
        px = px[rng.choice(len(px), sample, replace=False)]
    k = min(k, len(px))
    cent = [px[rng.integers(len(px))]]
    d2 = ((px - cent[0]) ** 2).sum(1)
    for _ in range(1, k):
        if d2.sum() <= 0:
            break
        cent.append(px[rng.choice(len(px), p=d2 / d2.sum())])
        d2 = np.minimum(d2, ((px - cent[-1]) ** 2).sum(1))
    C = np.array(cent, np.float32)
    for _ in range(15):
        lab = ((px[:, None, :] - C[None, :, :]) ** 2).sum(2).argmin(1)
        newc = np.array([px[lab == i].mean(0) if (lab == i).any() else C[i]
                         for i in range(len(C))], np.float32)
        if np.abs(newc - C).max() < 0.5:
            C = newc
            break
        C = newc
    lab = ((px[:, None, :] - C[None, :, :]) ** 2).sum(2).argmin(1)
    counts = np.bincount(lab, minlength=len(C))
    kept = []
    for i in np.argsort(-counts):
        if counts[i] < min_share * len(px) and len(kept) >= 2:
            continue
        c = C[i]
        # film-tinted white ink (232,252,253) is white: chroma up to 30
        if float(c.min()) >= 215 and float(c.max() - c.min()) <= 30:
            c = np.array([255, 255, 255], np.float32)
        if all(float(np.abs(c - q).sum()) > merge for q in kept):
            kept.append(c)
    return np.array([np.round(c) for c in kept], np.uint8) if kept else None


def decal_palette(crop_rgba):
    """The decal's own inks (the scan's colours, near-white = white ink).
    16 clusters (with 10 the banner's red and its darker halftone shade
    shared a cluster whose mean was maroon — v2.24.1); small clusters are
    kept unless they are a BLEND of two other inks (a pink between red
    and white, a purple-brown between black and red): dropping every
    cluster under 2% also dropped the orca's small red brush."""
    pal = _kmeans_palette(crop_rgba)
    if pal is None or len(pal) <= 2:
        return pal
    P0 = [np.array(c, np.float32) for c in pal]
    # near-identical inks are one ink: two blacks (4,3,5) and (18,19,21)
    # split a title's letters between them, and each half looked like a
    # thin sliver of "another ink" to the rim cleanup (v2.24.3)
    def _lum(c):
        return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    P = []
    for c in P0:
        # dark inks closer than 60 are one black too (20 vs 48: the lighter
        # one is the blend ring inside every letter)
        if all(float(np.sqrt(((c - q) ** 2).sum())) >= (
                60 if (_lum(c) < 70 and _lum(q) < 70) else 40) for q in P):
            P.append(c)
    if len(P) <= 2:
        return np.array([np.round(k) for k in P], np.uint8)
    keep = [P[0], P[1]]                  # the two commonest inks always stay
    for c in P[2:]:
        blend = False
        refs = keep + [p for p in P if p is not c]
        for ai in range(len(refs)):
            for bi in range(ai + 1, len(refs)):
                a, b = refs[ai], refs[bi]
                ab = b - a
                n2 = float((ab * ab).sum())
                if n2 < 1:
                    continue
                t = float(((c - a) * ab).sum()) / n2
                if 0.15 <= t <= 0.85 and float(np.sqrt(((a + t * ab - c) ** 2).sum())) < 28:
                    blend = True
                    break
            if blend:
                break
        if not blend:
            keep.append(c)
    return np.array([np.round(k) for k in keep], np.uint8)


def _prepare(crop_rgba, pal=None, dpi=None):
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
    if min(W, H) * s < MIN_SIDE:
        # a long thin decal (a one-line caption): fal.ai takes up to 4096
        # px a side, so the long side may grow past MAX_SIDE to bring the
        # short one to MIN_SIDE (the clean trace drew "Bad Mother Tattoos
        # & Customs" with grey patches and a pink counter)
        s = min(MIN_SIDE / float(min(W, H)), THIN_MAX_SIDE / float(max(W, H)))
    nw, nh = max(1, int(round(W * s))), max(1, int(round(H * s)))
    # fal.ai refuses a side under MIN_SIDE: rounding made a thin mark 255
    if min(nw, nh) < MIN_SIDE:
        import math
        nw = max(nw, int(math.ceil(W * s)))
        nh = max(nh, int(math.ceil(H * s)))
    if min(nw, nh) < 256:
        # a long thin mark cannot be enlarged to fal.ai's 256 px minimum
        # within its 4096 px maximum: the clean trace draws it (no call)
        raise FalError("the decal is too thin for Recraft (under 256 px)")
    if pal is None:
        pal = decal_palette(crop)
    # vote away flecks and notches up to ~0.15 mm (scan dust on a banner's
    # stripe edges, red/white flecks on an orca) before Recraft traces them
    px_per_mm = (float(dpi) * s / 25.4) if dpi else (s * 300 / 25.4)
    big = decals.flatten_decal(crop, pal, size=(nw, nh),
                               band=max(1, int(round(s))),
                               smooth_r=max(1, int(round(0.08 * px_per_mm))),
                               rim_r=max(1, int(round(0.12 * px_per_mm))),
                               straight_ppm=px_per_mm)
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
    # specks Recraft invents: a small patch of one ink inside a region of
    # another, where the picture SENT shows that other ink (red dots on
    # the orca's white belly, snapped to the red)
    specks = _invented_specks(rc, sent, pal)
    if exposed.sum() <= 0.002 * key.size and not specks.any():
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
    opaque = inside & ~key & (rk[..., 3] > 128)
    # pin-holes: clear specks shut inside the ink (under ~0.05% of the
    # picture) are anti-aliasing leftovers, not windows — closed; the edge
    # band below gives them the colour around them
    import decals as _d2
    holes = ~opaque
    lab, stats = _d2._label_runs(holes, diag=False)
    if stats:
        edge_ids = set(np.unique(np.concatenate([lab[0, :], lab[-1, :],
                                                 lab[:, 0], lab[:, -1]])).tolist())
        tiny = max(12, int(0.0005 * holes.size))
        for lid, (x0, y0, x1, y1, area) in stats.items():
            if lid not in edge_ids and area <= tiny:
                sl = (slice(max(0, y0 - 2), y1 + 3), slice(max(0, x0 - 2), x1 + 3))
                sub = lab[sl] == lid
                opaque[sl][sub] = True
                # the colour around it — left as the backing it snapped
                # to the nearest ink: magenta → red dots on a white belly
                ring = _d2.dilate_mask(sub, 2) & ~sub & opaque[sl]
                ring &= ~key[sl]
                if ring.any():
                    cols, cnt = np.unique(a[sl][..., :3][ring].reshape(-1, 3),
                                          axis=0, return_counts=True)
                    fill = cols[cnt.argmax()]
                    patch = a[sl]
                    patch[..., :3][sub] = fill
    if specks.any():
        sm2 = np.asarray(sent.convert("RGB").resize((a.shape[1], a.shape[0]),
                                                    Image.NEAREST))
        a[..., :3][specks] = sm2[specks]
    a[..., 3] = np.where(opaque, 255, 0).astype(np.uint8)
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


def _invented_specks(render, sent, pal, max_frac=0.0004):
    """Pixels of small patches (each under max_frac of the picture) where
    the render's ink differs from the SENT picture's and the patch is
    ringed by one ink only — the one the sent picture has there. A patch
    on a border between two inks (Recraft smoothing an edge) is left."""
    import decals
    H, W = render.shape[:2]
    none = np.zeros((H, W), bool)
    if sent is None or pal is None or len(pal) < 2:
        return none
    P = np.asarray(pal, np.int32)

    def _idx(rgb):
        px = rgb.reshape(-1, 3).astype(np.int32)
        d = ((px[:, None, :] - P[None, :, :]) ** 2).sum(2)
        return d.argmin(1).reshape(rgb.shape[:2])
    sm = np.asarray(sent.convert("RGB").resize((W, H), Image.NEAREST))
    s_key = (sm[..., 0].astype(int) >= 200) & (sm[..., 1] <= 70) & (sm[..., 2] >= 200)
    opaque = render[..., 3] > 128
    ri = _idx(render[..., :3])
    si = _idx(sm)
    diff = opaque & ~s_key & (ri != si)
    if not diff.any():
        return none
    out = none.copy()
    lab, stats = decals._label_runs(diff, diag=True)
    limit = max(16, int(max_frac * H * W))
    for lid, (x0, y0, x1, y1, area) in (stats or {}).items():
        if area > limit:
            continue
        sl = (slice(max(0, y0 - 2), y1 + 3), slice(max(0, x0 - 2), x1 + 3))
        sub = lab[sl] == lid
        ring = decals.dilate_mask(sub, 2) & ~sub
        if (ring & ~opaque[sl]).any():
            continue  # touches the outline
        rv_ = ri[sl][ring]
        if rv_.size == 0 or (rv_ != rv_[0]).any():
            continue  # on a border between inks
        if (si[sl][sub] == rv_[0]).mean() >= 0.8:
            out[sl][sub] = True
    return out


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


def vectorize_decal(crop_rgba, key, timeout=180.0, session=None, dpi=None):
    """One decal through Recraft Vectorize. Returns the SVG in crop-pixel
    coordinates. Raises FalError with an 'account' flag in the message
    for key / balance problems (the run then stops)."""
    import requests
    http = session or requests
    pal = decal_palette(crop_rgba)
    png, scale = _prepare(crop_rgba, pal, dpi=dpi)
    if len(png) >= 5 * 1024 * 1024:
        raise FalError("the decal picture is over 5 MB")
    sent_w = int(round(crop_rgba.width * scale))
    sent_h = int(round(crop_rgba.height * scale))
    vectorize_decal.last_cached = False
    import api_cache
    ckey = api_cache.bytes_key(png, ENDPOINT)
    hit = api_cache.load("recraft", ckey)
    if hit is not None and "<svg" in hit.get("svg", ""):
        # the very same picture was vectorized before: free
        vectorize_decal.last_cached = True
        api_cache.STATS["hits"] += 1
        return _finish(hit["svg"], sent_w, sent_h, scale, pal, crop_rgba, png)
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
    api_cache.STATS["misses"] += 1
    api_cache.save("recraft", ckey, {"svg": svg})
    return _finish(svg, sent_w, sent_h, scale, pal, crop_rgba, png)


def _finish(svg, sent_w, sent_h, scale, pal, crop_rgba, png):
    """Recraft's answer cleaned and checked against the picture sent."""
    sent_im = Image.open(io.BytesIO(png))
    out = finalize(svg, sent_w, sent_h, scale, pal, crop=crop_rgba,
                   sent=sent_im)
    try:
        out = restore_missing(out, sent_im, scale, crop_rgba.size)
    except Exception:
        pass
    return out


def restore_missing(svg_text, sent, scale, crop_size, min_mm=0.25, dpi=None):
    """Nothing the decal has may go missing: Recraft drops a thin mark now
    and then (the "/" of "1/12" in a title). Ink present in the picture
    SENT but absent from the answer — pieces bigger than a speck, beyond
    the edge band where the two may differ by a pixel — is traced from
    the sent picture and added to the answer."""
    import decals
    W, H = sent.size
    s_rgb = np.asarray(sent.convert("RGB")).astype(int)
    # the backing AND its blends (pink with white, purple with black):
    # red and blue both well above green — they are not ink (a pink "C"
    # counter came back otherwise)
    s_key = ((s_rgb[..., 0] - s_rgb[..., 1] > 50)
             & (s_rgb[..., 2] - s_rgb[..., 1] > 50))
    s_ink = ~s_key
    r = np.asarray(vector_redraw.render_svg(svg_text, W).convert("RGBA"))
    if r.shape[:2] != s_ink.shape:
        r = np.asarray(Image.fromarray(r).resize((W, H)))
    r_ink = r[..., 3] > 128
    band = max(2, int(round(W / 600.0)))
    # sent ink with NO drawn ink near it (a hairline "/" is missing whole;
    # an edge that moved a pixel is not)
    missing = s_ink & ~decals.dilate_mask(r_ink, band)
    if not missing.any():
        return svg_text
    # a few missing marks are restored; an answer missing a large part of
    # the decal is WRONG and must fail its check (then the trace is used)
    if missing.sum() > 0.10 * max(1, int(s_ink.sum())):
        return svg_text
    # pieces grouped across 2 px gaps: a hairline comes through as a
    # dotted chain of 1-2 px bits (the "/" of "1/12")
    lab, stats = decals._label_runs(decals.dilate_mask(missing, 2), diag=True)
    min_px = max(12, int((0.002 * min(W, H)) ** 2))
    keep_ids = []
    for lid, st in (stats or {}).items():
        x0, y0, x1, y1, _a = st
        own_px = int((missing[y0:y1 + 1, x0:x1 + 1]
                      & (lab[y0:y1 + 1, x0:x1 + 1] == lid)).sum())
        if own_px >= min_px:
            keep_ids.append(lid)
    if not keep_ids:
        return svg_text
    # each missing piece in ONE colour (its commonest ink; splitting by
    # colour broke a hairline into 1 px bits the tracer drops), a pixel
    # thicker so it traces and prints; sent px → crop px
    paths = []
    for lid in keep_ids:
        x0, y0, x1, y1, _a = stats[lid]
        sl = (slice(max(0, y0 - 2), y1 + 3), slice(max(0, x0 - 2), x1 + 3))
        comp = decals.dilate_mask((lab[sl] == lid) & missing[sl], 1)
        cols = s_rgb[sl][comp & s_ink[sl]]
        if cols.size == 0:
            continue
        u, n = np.unique(cols.reshape(-1, 3), axis=0, return_counts=True)
        c = u[n.argmax()]
        # traced in its own small window (fast), placed back by offset
        m = np.pad(comp, 2)
        ox, oy = sl[1].start - 2, sl[0].start - 2
        d = []
        for q in decals._mask_polygons(m):
            d.append("M" + " L".join(f"{x + ox:.1f},{y + oy:.1f}"
                                     for x, y in q) + " Z")
        if d:
            paths.append('<path fill="#%02x%02x%02x" fill-rule="evenodd" d="%s"/>'
                         % (int(c[0]), int(c[1]), int(c[2]), " ".join(d)))
    if not paths:
        return svg_text
    k = 1.0 / float(scale)
    add = f'<g transform="scale({k:.6f} {k:.6f})">' + "".join(paths) + '</g>'
    i = svg_text.rfind("</svg>")
    if i < 0:
        return svg_text
    return svg_text[:i] + add + svg_text[i:]


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
                lambda: vectorize_decal(crop, key, session=session,
                                        dpi=crop.width / max(1e-6, w_in)),
                cancelled)
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
        if not getattr(vectorize_decal, "last_cached", False):
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
