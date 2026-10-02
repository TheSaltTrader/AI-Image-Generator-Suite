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


def _prepare(crop_rgba):
    """The decal on magenta, sized for the service. Returns (png bytes,
    scale = sent px per crop px)."""
    crop = crop_rgba.convert("RGBA")
    W, H = crop.size
    # enlarge small decals (more detail to trace), keep inside the limits
    s = max(MIN_SIDE / float(min(W, H)), min(4.0, MAX_SIDE / float(max(W, H))))
    s = min(s, MAX_SIDE / float(max(W, H)))
    nw, nh = max(1, int(round(W * s))), max(1, int(round(H * s)))
    big = crop.resize((nw, nh), Image.LANCZOS)
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


def clean_svg(svg_text, sent_w, sent_h, scale):
    """Remove the magenta backing shapes, then map the drawing back onto
    the crop: viewBox in crop pixels (the service draws in sent pixels)."""
    def drop(m):
        tag = m.group(0)
        f = _FILL.search(tag)
        if f:
            c = _rgb_of(f.group(1))
            if c is not None and _is_key(c):
                return ""
        return tag
    body = re.sub(r"<(path|rect|polygon|circle|ellipse)\b[^>]*?/>", drop,
                  svg_text, flags=re.S | re.I)
    body = re.sub(r"<(path|rect|polygon|circle|ellipse)\b[^>]*?>\s*</\1>", drop,
                  body, flags=re.S | re.I)
    inner = _inner(body)
    vb = _viewbox(svg_text) or (0.0, 0.0, float(sent_w), float(sent_h))
    cw, ch = sent_w / scale, sent_h / scale
    # the service's own viewBox → crop pixels
    sx, sy = cw / vb[2], ch / vb[3]
    return ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{cw:.2f}" height="{ch:.2f}" viewBox="0 0 {cw:.3f} {ch:.3f}">'
            f'<g transform="scale({sx:.6f} {sy:.6f}) translate({-vb[0]:.3f} {-vb[1]:.3f})">'
            f'{inner}</g></svg>')


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
    png, scale = _prepare(crop_rgba)
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
    return clean_svg(svg, sent_w, sent_h, scale)


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
