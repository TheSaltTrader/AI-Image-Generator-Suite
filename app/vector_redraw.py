"""Decal redraw by a vision model: the scan crop is DESCRIBED and drawn as
clean vector art (real shapes, real text), instead of having its pixels
repainted. A diffusion model cannot reproduce 2 mm text or symbols; a vision
model looking at the decal can say "red rounded rectangle, white bold FUEL,
red arrow" and write exactly that as SVG.

Pieces (all importable without the app, testable without the network):

  get_api_key / set_api_key   the Anthropic key, kept in the Windows
                              Credential Manager (never in settings.json
                              or the repo); ANTHROPIC_API_KEY also works
  draw_decal(...)             one decal crop -> SVG text via the Claude API
                              (official anthropic SDK), with usage + cost
  text_to_paths(svg)          <text> -> glyph outlines (fontTools + a
                              Windows bold font), so the SVG prints anywhere
  check_against_scan(...)     does the drawing match the scan? (silhouette
                              overlap + colour layout) -> accept or fall back

The app's Decals tab wires these into decals.redraw_sheet as `vector_fn`.
"""

import base64
import ctypes
import ctypes.wintypes as wt
import html
import io
import os
import re
from pathlib import Path

import numpy as np
from PIL import Image

# ---------------------------------------------------------------- models
# (label, model id). Opus 5.5 is the default; Sonnet 5.5 is the cheaper
# option the user can pick. Prices are USD per million tokens (in, out),
# Anthropic first-party rates, checked 2026-10-01.
MODELS = [
    ("Claude Opus 5.5 — best quality", "claude-opus-5-5"),
    ("Claude Sonnet 5.5 — cheaper", "claude-sonnet-5-5"),
    # local, through Ollama (free, private; the tag after 'ollama:' is what
    # `ollama pull` takes). Sizes are the download; they need that much VRAM.
    ("Qwen3-VL 32B — local, best (21 GB)", "ollama:qwen3-vl:32b"),
    ("Qwen3-VL 8B — local, fast (6 GB)", "ollama:qwen3-vl:8b"),
    ("Gemma 3 27B — local (17 GB)", "ollama:gemma3:27b"),
    ("Mistral Small 3.2 — local (15 GB)", "ollama:mistral-small3.2"),
    ("Qwen2.5-VL 7B — local (6 GB)", "ollama:qwen2.5vl:7b"),
]
DEFAULT_MODEL = "claude-opus-5-5"
LOCAL_PREFIX = "ollama:"


def is_local(model_id):
    return str(model_id or "").startswith(LOCAL_PREFIX)


def local_tag(model_id):
    """'ollama:qwen3-vl:8b' → 'qwen3-vl:8b'."""
    m = str(model_id or "")
    return m[len(LOCAL_PREFIX):] if m.startswith(LOCAL_PREFIX) else m


def ollama_url():
    """Ollama's endpoint, honouring OLLAMA_HOST (often a bare host:port)."""
    h = (os.environ.get("OLLAMA_HOST") or "127.0.0.1:11434").strip()
    if not h.startswith(("http://", "https://")):
        h = "http://" + h
    return h.rstrip("/")


def ollama_vision_models(timeout=2):
    """Tags of the installed Ollama models that take images, or None when
    Ollama is not running (a plain [] means running, none installed)."""
    import requests
    try:
        r = requests.get(f"{ollama_url()}/api/tags", timeout=timeout)
        r.raise_for_status()
        out = []
        for m in r.json().get("models", []):
            caps = m.get("capabilities") or []
            name = m.get("name") or m.get("model") or ""
            fam = str((m.get("details") or {}).get("family", "")).lower()
            if "vision" in caps or "vl" in fam or "llava" in name or "vision" in name:
                out.append(name)
        return sorted(out)
    except Exception:
        return None


def ollama_pull(tag, progress=None, timeout=3600, on_bytes=None,
                cancelled=None):
    """`ollama pull <tag>` through the API. `progress(text)` gets each
    status line; `on_bytes(done, total, status)` gets the WHOLE download
    so far — Ollama reports each layer (digest) on its own, so the layers
    are summed. `cancelled()` → stop. Raises RuntimeError with Ollama's
    message on failure."""
    import json
    import requests
    totals, dones = {}, {}
    with requests.post(f"{ollama_url()}/api/pull", json={"model": tag, "stream": True},
                       stream=True, timeout=timeout) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if cancelled and cancelled():
                raise RuntimeError("cancelled")
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("error"):
                raise RuntimeError(f"Ollama: {d['error']}")
            st = d.get("status", "")
            tot, done = d.get("total"), d.get("completed")
            dig = d.get("digest") or st
            if tot:
                totals[dig] = int(tot)
                dones[dig] = int(done or 0)
            if on_bytes and totals:
                on_bytes(sum(dones.values()), sum(totals.values()), st)
            if progress:
                if tot and done is not None:
                    progress(f"{st} {100.0 * done / max(1, tot):.0f}% "
                             f"({done / 1e9:.1f} / {tot / 1e9:.1f} GB)")
                else:
                    progress(st)
    return True


def pull_eta_text(done, total, elapsed_s, start_done=0):
    """'37% · 7.8 / 21.0 GB · 42 MB/s · about 5 min left' — the rate is
    measured over this session's bytes (a resumed pull starts part-way)."""
    pct = 100.0 * done / max(1, total)
    got = max(0, done - start_done)
    rate = got / elapsed_s if elapsed_s > 0.5 else 0.0
    txt = f"{pct:.0f}% · {done / 1e9:.1f} / {total / 1e9:.1f} GB"
    if rate > 0:
        txt += f" · {rate / 1e6:.0f} MB/s"
        left = (total - done) / rate
        if done >= total:
            txt += " · verifying…"
        elif left < 60:
            txt += f" · about {max(1, int(left))} s left"
        elif left < 3600:
            txt += f" · about {int(round(left / 60))} min left"
        else:
            txt += f" · about {left / 3600:.1f} h left"
    else:
        txt += " · measuring speed…"
    return txt


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, i, o):
        self.input_tokens = int(i or 0)
        self.output_tokens = int(o or 0)
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _Resp:
    def __init__(self, text, model, usage):
        self.content = [_Block(text)]
        self.stop_reason = "end_turn"
        self.model = model
        self.usage = usage
        self._request_id = None


class OllamaVision:
    """A stand-in for the Anthropic client that sends the same asks
    (image + text, a system prompt) to a local Ollama vision model, so
    draw_decal / read_text_decal / ask_orientation run unchanged. Only
    what those use is implemented: client.messages.create(...) and
    client.beta.messages.create(...) (betas/fallbacks ignored). A server
    or model problem raises RuntimeError('Ollama: …') — the run stops
    instead of silently tracing every decal."""

    def __init__(self, base_url=None, timeout=600.0):
        self.base_url = (base_url or ollama_url()).rstrip("/")
        self.timeout = float(timeout)
        self.messages = self
        self.beta = self

    def create(self, model=None, max_tokens=4096, system=None, messages=(),
               **_ignored):
        import requests
        tag = local_tag(model)
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        for m in messages:
            texts, images = [], []
            content = m.get("content")
            if isinstance(content, str):
                texts.append(content)
            else:
                for part in content or []:
                    if part.get("type") == "text":
                        texts.append(part.get("text", ""))
                    elif part.get("type") == "image":
                        src = part.get("source") or {}
                        if src.get("type") == "base64" and src.get("data"):
                            images.append(src["data"])
            entry = {"role": m.get("role", "user"), "content": "\n".join(texts)}
            if images:
                entry["images"] = images
            msgs.append(entry)
        body = {"model": tag, "messages": msgs, "stream": False, "think": False,
                "options": {"temperature": 0, "num_predict": int(max_tokens)}}
        try:
            r = requests.post(f"{self.base_url}/api/chat", json=body,
                              timeout=self.timeout)
        except requests.RequestException as e:
            raise RuntimeError(f"Ollama: not reachable at {self.base_url} — "
                               f"start Ollama ({e.__class__.__name__})")
        if r.status_code != 200:
            try:
                err = r.json().get("error", r.text)
            except Exception:
                err = r.text
            if "not found" in str(err).lower():
                raise RuntimeError(f"Ollama: model {tag} is not installed — "
                                   f"press ⬇ Pull model (ollama pull {tag})")
            raise RuntimeError(f"Ollama: {str(err)[:200]}")
        d = r.json()
        text = ((d.get("message") or {}).get("content") or "")
        text = re.sub(r"(?s)<think>.*?</think>", "", text).strip()
        return _Resp(text, LOCAL_PREFIX + tag,
                     _Usage(d.get("prompt_eval_count"), d.get("eval_count")))
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
}
CACHE_READ_PRICE = {"claude-opus-5-5": 0.20, "claude-sonnet-5-5": 0.20}

# ---------------------------------------------------------------- the key
CRED_TARGET = "AIImageGeneratorSuite/anthropic"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME),
        ("CredentialBlobSize", wt.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wt.LPWSTR),
        ("UserName", wt.LPWSTR)]


def cred_read(target):
    """The secret stored under `target` in the Credential Manager (a
    generic credential, UTF-16 blob — the convention pywin32/keyring use),
    or None."""
    if os.name != "nt":
        return None
    adv = ctypes.windll.advapi32
    p = ctypes.POINTER(_CREDENTIAL)()
    if not adv.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        return None
    try:
        n = p.contents.CredentialBlobSize
        raw = bytes(bytearray(p.contents.CredentialBlob[i] for i in range(n)))
    finally:
        adv.CredFree(p)
    for enc in ("utf-16-le", "utf-8"):
        try:
            s = raw.decode(enc)
            if s and "\x00" not in s:
                return s.strip()
        except UnicodeDecodeError:
            continue
    return None


def cred_write(target, secret, username="api"):
    """Store (or with an empty secret, delete) a generic credential."""
    if os.name != "nt":
        return False
    adv = ctypes.windll.advapi32
    if not secret:
        adv.CredDeleteW(target, _CRED_TYPE_GENERIC, 0)
        return True
    blob = secret.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    c = _CREDENTIAL()
    c.Flags = 0
    c.Type = _CRED_TYPE_GENERIC
    c.TargetName = target
    c.Comment = "AI Image Generator Suite"
    c.CredentialBlobSize = len(blob)
    c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    c.Persist = _CRED_PERSIST_LOCAL_MACHINE
    c.AttributeCount = 0
    c.Attributes = None
    c.TargetAlias = None
    c.UserName = username
    return bool(adv.CredWriteW(ctypes.byref(c), 0))


def get_api_key():
    """ANTHROPIC_API_KEY if set, else the key saved by the app."""
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return k or cred_read(CRED_TARGET)


def set_api_key(key):
    return cred_write(CRED_TARGET, (key or "").strip())


def have_sdk():
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- cost
def estimate_cost(model, usage):
    """USD for one response's usage (input, output, cache read/write);
    a local model costs nothing."""
    if is_local(model):
        return 0.0
    pin, pout = PRICES.get(model, (4.0, 20.0))
    try:
        i = int(getattr(usage, "input_tokens", 0) or 0)
        o = int(getattr(usage, "output_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    except Exception:
        return 0.0
    return (i * pin + cw * pin * 1.25 + cr * CACHE_READ_PRICE.get(model, 0.2)
            + o * pout) / 1_000_000.0


# ---------------------------------------------------------------- the ask
SYSTEM = """You redraw one scanned waterslide decal (a sticker from a model-kit sheet) as clean vector art.

You get: the decal as an image (a scan crop with the carrier film removed and shown on a plain grey-blue backing, which is NOT part of the decal — never draw the backing), its exact printed size in inches, and the palette of colours it uses. White parts of the decal are white ink: draw them in white.

Reply with ONE complete <svg> element and nothing else — no explanation, no code fence.

Rules:
- viewBox="0 0 W H" where W and H are the PIXEL size of the image you were given, so your coordinates map 1:1 onto the image; set width and height to the inches given.
- Reproduce exactly what is there: the same shapes, the same words in the same letter case, the same positions and proportions. Do not add, remove, straighten or "improve" anything, and never invent words — if a part is unreadable, draw its shape as best you can.
- Use ONLY fills from the palette (hex values given). No gradients, no filters, no opacity, no stroke-only art unless the scan shows a line; a stroke must also use a palette colour.
- Text: use <text> elements with font-family="Arial, Helvetica, sans-serif" font-weight="bold", placed with x/y and text-anchor so the words sit where they are in the image, and textLength (with lengthAdjust="spacingAndGlyphs") set to the width the word spans in the image. Rotated text uses transform="rotate(...)".
- Keep it simple: rect, circle, ellipse, polygon, path, text. Round corners only where the scan shows them.
- The background stays empty (no full-size background rect) unless the decal itself is a filled panel.
- Be honest about legibility: if any word, number or symbol in the decal is too small, too blurred or too oddly rotated for you to read it with confidence, do NOT guess — reply with the single word UNSURE and nothing else. A faithful trace is used instead. Only draw what you can actually read."""


BACKING = (170, 185, 195)          # the stand-in for the carrier film
BACKING_HEX = "#%02x%02x%02x" % BACKING


class Cancelled(RuntimeError):
    """The user pressed Cancel while a model call was in flight."""


def call_cancellable(fn, cancelled=None, poll=0.1):
    """Run a blocking model call on a helper thread and wait for it in
    short steps, so Cancel takes effect at once instead of after the whole
    request (20-60 s at high effort). On cancel the call is abandoned —
    its answer, when it comes, is dropped — and Cancelled is raised.
    The call's own exception is re-raised as is."""
    import threading
    if cancelled is None:
        return fn()
    box = {}

    def run():
        try:
            box["ok"] = fn()
        except BaseException as e:          # handed back to the caller
            box["err"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    while t.is_alive():
        if cancelled():
            raise Cancelled("cancelled")
        t.join(poll)
    if "err" in box:
        raise box["err"]
    return box.get("ok")


class Unsure(RuntimeError):
    """The model said UNSURE: it could not read the decal — trace it."""


def _crop_png_b64(rgba, max_px=1024):
    """The crop on a muted grey-blue backing (so WHITE ink is visible to the
    model — on white it vanished and the model left white text out),
    enlarged for the model's eyes (it reads small text better) while the
    coordinates in the ask stay the crop's own pixels."""
    im = rgba.convert("RGBA")
    scale = 1.0
    if max(im.size) < max_px:
        scale = max_px / max(im.size)
        im = im.resize((max(1, int(round(im.width * scale))),
                        max(1, int(round(im.height * scale)))), Image.LANCZOS)
    on_back = Image.alpha_composite(
        Image.new("RGBA", im.size, BACKING + (255,)), im).convert("RGB")
    buf = io.BytesIO()
    on_back.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("ascii"), scale


def _extract_svg(text):
    m = re.search(r"<svg\b.*?</svg>", text, re.S | re.I)
    return m.group(0) if m else None


def draw_decal(crop_rgba, w_in, h_in, palette, hint="", model=DEFAULT_MODEL,
               client=None, api_key=None, timeout=180.0):
    """Ask the model to draw the decal. Returns a dict:
      svg        the SVG text (viewBox in crop pixels, size in inches)
      usage      the response usage object
      cost_usd   estimated cost of this call
      model      the model that answered
      request_id for support
    Raises on API errors (the caller decides whether to fall back)."""
    import anthropic
    if client is None:
        key = api_key or get_api_key()
        if not key:
            raise RuntimeError("no Anthropic API key — add one with the 🔑 "
                               "button")
        client = anthropic.Anthropic(api_key=key, timeout=timeout)
    W, H = crop_rgba.size
    b64, scale = _crop_png_b64(crop_rgba)
    pal_hex = ["#%02x%02x%02x" % tuple(int(v) for v in c) for c in palette] \
        if palette is not None else []
    ask = (f"Printed size: {w_in:.3f} in wide x {h_in:.3f} in high.\n"
           f"The grey-blue backing ({BACKING_HEX}) is not part of the decal.\n"
           f"Image pixel size for the viewBox: {W} x {H}"
           + (f" (the picture you see is enlarged {scale:.1f}x for "
              f"legibility; use {W} x {H} for your coordinates)." if scale != 1.0 else ".")
           + ("\nPalette (use only these fills): " + ", ".join(pal_hex)
              if pal_hex else "")
           + (f"\nAbout this decal: {hint}" if hint else ""))
    kwargs = dict(
        model=model, max_tokens=16000, system=SYSTEM,
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png", "data": b64}},
            {"type": "text", "text": ask}]}])
    try:
        # server-side refusal fallback (routes a policy decline to another
        # model inside the same call); harmless for decal art
        resp = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            **kwargs)
    except anthropic.BadRequestError:
        resp = client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        raise RuntimeError("the model declined to draw this decal")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    svg = _extract_svg(text)
    if not svg:
        if "UNSURE" in text.upper():
            raise Unsure("the model could not read this decal with confidence")
        raise RuntimeError("the model did not return an SVG")
    if "xmlns=" not in svg[:200]:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    if "viewBox" not in svg[:400]:
        svg = re.sub(r"<svg\b", f'<svg viewBox="0 0 {W} {H}"', svg, count=1)
    return {"svg": svg, "usage": resp.usage,
            "cost_usd": estimate_cost(getattr(resp, "model", model) or model,
                                      resp.usage),
            "model": getattr(resp, "model", model),
            "request_id": getattr(resp, "_request_id", None)}


def ask_orientation(img, model=DEFAULT_MODEL, client=None, api_key=None):
    """Which way is up? One small call per sheet: the model looks at the
    whole page (downscaled) and answers 0, 90, 180 or 270 = the counter-
    clockwise rotation that makes the text upright. Returns an int;
    0 when it cannot tell. The clean trace does not need this (geometry
    is geometry); the vision model reads text and does."""
    import anthropic
    if client is None:
        key = api_key or get_api_key()
        if not key:
            return 0
        client = anthropic.Anthropic(api_key=key, timeout=60.0)
    im = img.convert("RGB")
    if max(im.size) > 1024:
        s = 1024.0 / max(im.size)
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))),
                       Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    resp = client.messages.create(
        model=model, max_tokens=64,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png", "data": b64}},
            {"type": "text", "text":
                "This is a scan or photo of a sticker (decal) sheet. Which "
                "way does most of its text read? Reply with exactly one "
                "number and nothing else: 0 if the text is upright as "
                "shown; 90 if the picture must be rotated 90 degrees "
                "counter-clockwise to make the text upright; 180 if it is "
                "upside down; 270 if it must be rotated 90 degrees "
                "clockwise."}]}])
    if resp.stop_reason == "refusal":
        return 0
    text = "".join(b.text for b in resp.content
                   if getattr(b, "type", "") == "text").strip()
    m = re.search(r"\b(0|90|180|270)\b", text)
    return int(m.group(1)) if m else 0


def make_vector_fn(client, model, target_dpi, hint="", stats=None,
                   cancelled=None, log=None):
    """The `vector_fn` for decals.redraw_sheet: ask the model, outline the
    text, check the drawing against the scan, render it. Returns None for
    a decal that should fall back to the clean trace (a failed call, a
    drawing that drifted). A rejected API key raises so the whole run stops
    with a clear message. `stats` (a dict) collects calls/ok/fallback/cost."""
    import decals
    st = stats if stats is not None else {}
    for k in ("calls", "ok", "fallback"):
        st.setdefault(k, 0)
    st.setdefault("cost", 0.0)
    st.setdefault("unsure", 0)
    st.setdefault("checks", [])

    def vector_fn(crop, pal, w_in, h_in):
        if cancelled and cancelled():
            return None
        try:
            res = call_cancellable(
                lambda: draw_decal(crop, w_in, h_in, pal, hint=hint,
                                   model=model, client=client), cancelled)
        except Cancelled:
            return None
        except Unsure:
            # the honest answer: it could not read the decal — trace it
            st["calls"] += 1
            st["unsure"] += 1
            st["fallback"] += 1
            if log:
                log("vision redraw: the model was unsure of this decal; "
                    "clean trace used")
            return None
        except Exception as e:
            # an account-level stop (bad key, spending limit, no credit)
            # ends the whole run with a clear message — silently tracing
            # the remaining 140 decals is not what the user asked for
            msg = str(e)
            low = msg.lower()
            if low.startswith("ollama:"):
                raise RuntimeError(msg)
            try:
                import anthropic
                if isinstance(e, anthropic.AuthenticationError):
                    raise RuntimeError("the Anthropic API key was rejected — "
                                       "check it with 🔑 API key…")
                if isinstance(e, anthropic.PermissionDeniedError):
                    raise RuntimeError("the Anthropic API key is not allowed "
                                       "to use this model: " + msg[:200])
            except ImportError:
                pass
            if ("usage limit" in low or "spending limit" in low
                    or "credit balance" in low or "billing" in low
                    or "regain access" in low):
                raise RuntimeError(
                    "Anthropic stopped the calls: " + msg.split("message")[-1]
                    .strip(" ':{}\"")[:220]
                    + " — raise the limit at console.anthropic.com → Settings "
                      "→ Limits (or wait for the reset). No decal was drawn "
                      "by the model after this point.")
            if log:
                log("vision redraw call failed; clean trace used: %r" % (e,))
            st["fallback"] += 1
            return None
        st["calls"] += 1
        st["cost"] += float(res.get("cost_usd") or 0.0)
        svg = text_to_paths(res["svg"])
        if _corners_clear(crop):
            # no backdrop: the decal prints on clear film
            svg = strip_background(svg, crop.width, crop.height)
        ok, iou, col = check_against_scan(svg, crop)
        st["checks"].append((round(iou, 3), round(col, 1), ok))
        if not ok:
            if log:
                log(f"vision redraw rejected (overlap {iou:.2f}, colour "
                    f"{col:.0f}); clean trace used")
            st["fallback"] += 1
            return None
        svg = decals.svg_set_physical_size(svg, w_in, h_in)
        ras = render_svg(svg, max(1, int(round(w_in * target_dpi))))
        st["ok"] += 1
        return svg, ras

    return vector_fn


# ---------------------------------------------------------------- text sweep
READ_SYSTEM = """You read the lettering on one scanned decal (a sticker from a model-kit
sheet) exactly as printed. You never guess: if any character cannot be read
with confidence, you answer with the single word UNSURE and nothing else."""


def read_text_decal(crop_rgba, n_lines, palette=None, model=DEFAULT_MODEL,
                    client=None, api_key=None, timeout=120.0):
    """Ask the model for the words on a text-only decal. Returns a dict
    {"lines": [{"text", "colour", "weight", "italic"}], "align", "usage",
    "cost_usd"}; raises Unsure when it would not commit, RuntimeError on
    a refusal or an unusable answer, and the SDK's errors on API trouble."""
    import anthropic
    import json
    if client is None:
        key = api_key or get_api_key()
        if not key:
            raise RuntimeError("no Anthropic API key — add one with the 🔑 "
                               "button")
        client = anthropic.Anthropic(api_key=key, timeout=timeout)
    b64, scale = _crop_png_b64(crop_rgba)
    pal_hex = ["#%02x%02x%02x" % tuple(int(v) for v in c) for c in palette] \
        if palette is not None else []
    ask = (f"This decal is lettering only, printed on {n_lines} line(s). "
           "Read it exactly, top line first. Reply with JSON only, no prose:\n"
           '{"lines": [{"text": "...", "colour": "#rrggbb", '
           '"weight": "regular|bold|black", "italic": false}], '
           '"align": "left|center|right"}\n'
           f"The grey-blue backing ({BACKING_HEX}) is not part of the decal."
           + ("\nColours on this decal: " + ", ".join(pal_hex) if pal_hex else "")
           + "\nIf any character is not certain, reply UNSURE.")
    resp = client.messages.create(
        model=model, max_tokens=600, system=READ_SYSTEM,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png", "data": b64}},
            {"type": "text", "text": ask}]}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("the model declined to read this decal")
    text = "".join(b.text for b in resp.content
                   if getattr(b, "type", "") == "text").strip()
    if "UNSURE" in text.upper() and "{" not in text:
        raise Unsure("the model could not read this lettering with confidence")
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        raise RuntimeError("the model did not answer with JSON")
    try:
        data = json.loads(text[i:j + 1])
    except Exception as e:
        raise RuntimeError(f"unreadable JSON from the model: {e}")
    lines = data.get("lines") or []
    if not isinstance(lines, list) or not lines:
        raise Unsure("the model returned no lines")
    out = []
    for ln in lines:
        if not isinstance(ln, dict) or not str(ln.get("text", "")).strip():
            continue
        if "unsure" in str(ln.get("text", "")).lower():
            raise Unsure("the model was unsure of a line")
        out.append({"text": str(ln.get("text", "")).strip(),
                    "colour": str(ln.get("colour", "#000000")),
                    "weight": str(ln.get("weight", "bold")).lower(),
                    "italic": bool(ln.get("italic", False))})
    if not out:
        raise Unsure("the model returned no readable line")
    return {"lines": out, "align": str(data.get("align", "center")).lower(),
            "usage": resp.usage,
            "cost_usd": estimate_cost(getattr(resp, "model", model) or model,
                                      resp.usage)}


def _snap_hex(colour, palette):
    """The palette colour nearest the model's #rrggbb (the scan's own
    colour wins over the model's guess); the model's when no palette."""
    try:
        c = colour.lstrip("#")
        rgb = tuple(int(c[k:k + 2], 16) for k in (0, 2, 4))
    except Exception:
        rgb = (0, 0, 0)
    if palette is None or len(palette) == 0:     # may be a numpy array
        return "#%02x%02x%02x" % rgb
    best = min(list(palette),
               key=lambda q: sum((int(q[k]) - rgb[k]) ** 2 for k in range(3)))
    return "#%02x%02x%02x" % tuple(int(v) for v in best)


def typeset_lines(read, line_boxes, W, H, w_in, h_in, palette=None):
    """Set the words the model read into the rows the scan shows: each
    line's cap height comes from its row box (a little less when the line
    has descenders), its left/centre/right from the row and the align,
    its width from the row (within 0.7–1.35× the face's natural width so
    a misread letter cannot stretch a word absurdly), its colour snapped
    to the scan's palette. Returns the SVG in crop pixels with glyph
    outlines (no fonts needed), or None when the rows and the lines do
    not match."""
    lines = read["lines"]
    if len(lines) != len(line_boxes):
        return None
    align = read.get("align", "center")
    els = []
    for ln, (x0, y0, x1, y1) in zip(lines, line_boxes):
        text = ln["text"]
        weight = ln.get("weight", "bold")
        family = ("Arial Black" if weight == "black" else
                  "Arial Regular" if weight == "regular" else "Arial Bold")
        if ln.get("italic"):
            family += " Italic"
        fp = _find_font(family)
        if fp is None:
            return None
        font, glyphset, cmap, upem, hmtx, cap = _load_font(fp)
        box_h = max(1.0, float(y1 - y0))
        has_desc = any(ch in "gjpqy,;()" for ch in text)
        cap_px = box_h / 1.22 if has_desc else box_h
        size = cap_px * upem / float(max(1, cap))
        natural = sum(hmtx[cmap.get(ord(ch), cmap.get(ord("?")))][0]
                      for ch in text if cmap.get(ord(ch), cmap.get(ord("?")))
                      is not None) * size / float(upem)
        if natural <= 0:
            return None
        box_w = float(x1 - x0)
        shown = min(1.35 * natural, max(0.7 * natural, box_w))
        if align == "left":
            x, anchor = x0, "start"
        elif align == "right":
            x, anchor = x1, "end"
        else:
            x, anchor = (x0 + x1) / 2.0, "middle"
        y = y0 + cap_px
        fill = _snap_hex(ln.get("colour", "#000000"), palette)
        safe = html.escape(text, quote=False)
        els.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size:.2f}" '
                   f'font-family="{family}" text-anchor="{anchor}" '
                   f'textLength="{shown:.2f}" fill="{fill}">{safe}</text>')
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
           f'width="{w_in:.4f}in" height="{h_in:.4f}in" viewBox="0 0 {W} {H}">'
           + "".join(els) + "</svg>")
    out = text_to_paths(svg)
    if "<text" in out:
        return None
    return out


def strip_background(svg_text, W, H):
    """Remove any full-canvas <rect> the model drew as a backdrop (the
    backing colour, white, anything): a decal prints on clear film, so
    where it has no ink there must be NOTHING. Only rects that cover at
    least 95% of the viewBox go; a panel decal's own fill is kept by the
    caller (it only strips when the scan's corners are clear)."""
    def gone(m):
        tag = m.group(0)
        a = _attrs(tag)
        def dim(v, full):
            v = (v or "").strip()
            if v.endswith("%"):
                return full * _num(v, 0.0) / 100.0
            return _num(v, 0.0)
        x, y = dim(a.get("x", "0"), W), dim(a.get("y", "0"), H)
        w, h = dim(a.get("width", "0"), W), dim(a.get("height", "0"), H)
        if x <= 0.03 * W and y <= 0.03 * H and w >= 0.95 * W and h >= 0.95 * H:
            return ""
        return tag
    return re.sub(r"<rect\b[^>]*?/>|<rect\b[^>]*?>\s*</rect>", gone, svg_text,
                  flags=re.S | re.I)


def _corners_clear(crop_rgba):
    """True when the scan crop is transparent at its corners — the decal
    is not a filled panel, so a full-size rect in the drawing is a
    backdrop, not the decal."""
    a = np.asarray(crop_rgba.convert("RGBA"))
    H, W = a.shape[:2]
    k = max(1, min(W, H) // 20)
    corners = (a[:k, :k, 3], a[:k, -k:, 3], a[-k:, :k, 3], a[-k:, -k:, 3])
    return sum(1 for c in corners if c.mean() < 40) >= 3


def _account_stop(e):
    """Raise a clear RuntimeError for an account-level API failure (bad
    key, model not allowed, usage limit, no credit) or a local server /
    model problem, so a run stops instead of silently falling back decal
    after decal; return quietly otherwise."""
    msg = str(e)
    low = msg.lower()
    if low.startswith("ollama:"):
        raise RuntimeError(msg)
    try:
        import anthropic
        if isinstance(e, anthropic.AuthenticationError):
            raise RuntimeError("the Anthropic API key was rejected — "
                               "check it with 🔑 API key…")
        if isinstance(e, anthropic.PermissionDeniedError):
            raise RuntimeError("the Anthropic API key is not allowed "
                               "to use this model: " + msg[:200])
    except ImportError:
        pass
    if ("usage limit" in low or "spending limit" in low
            or "credit balance" in low or "billing" in low
            or "regain access" in low):
        raise RuntimeError(
            "Anthropic stopped the calls: " + msg.split("message")[-1]
            .strip(" ':{}\"")[:220]
            + " — raise the limit at console.anthropic.com → Settings "
              "→ Limits (or wait for the reset). No decal was drawn "
              "by the model after this point.")


def make_text_fn(client, model, target_dpi, stats=None, cancelled=None,
                 log=None):
    """The `text_fn` for decals.redraw_sheet: a lettering-only decal has
    its words read by the model and re-set in a real font at the rows the
    scan shows, then checked against the scan. None = let the normal path
    (vision drawing or clean trace) handle it. `stats` collects
    text (set in type), text_fallback, calls, cost."""
    import decals
    st = stats if stats is not None else {}
    for k in ("calls", "text", "text_fallback", "unsure"):
        st.setdefault(k, 0)
    st.setdefault("cost", 0.0)

    def text_fn(crop, pal, w_in, h_in, geom):
        if cancelled and cancelled():
            return None
        boxes = geom.get("lines") or []
        if not boxes:
            return None
        try:
            read = call_cancellable(
                lambda: read_text_decal(crop, len(boxes), palette=pal,
                                        model=model, client=client), cancelled)
        except Cancelled:
            return None
        except Unsure:
            st["calls"] += 1
            st["unsure"] += 1
            st["text_fallback"] += 1
            if log:
                log("text sweep: the model was unsure of this lettering; "
                    "normal path used")
            return None
        except Exception as e:
            _account_stop(e)
            if log:
                log("text sweep call failed; normal path used: %r" % (e,))
            st["text_fallback"] += 1
            return None
        st["calls"] += 1
        st["cost"] += float(read.get("cost_usd") or 0.0)
        W, H = crop.size
        svg = typeset_lines(read, boxes, W, H, w_in, h_in, palette=pal)
        if svg is None:
            st["text_fallback"] += 1
            if log:
                log("text sweep: %d line(s) read, %d row(s) on the scan; "
                    "normal path used" % (len(read["lines"]), len(boxes)))
            return None
        ok, iou, col = check_against_scan(svg, crop, min_iou=0.4)
        if not ok:
            st["text_fallback"] += 1
            if log:
                log(f"text sweep rejected (overlap {iou:.2f}, colour "
                    f"{col:.0f}); normal path used")
            return None
        svg = decals.svg_set_physical_size(svg, w_in, h_in)
        ras = render_svg(svg, max(1, int(round(w_in * target_dpi))))
        st["text"] += 1
        return svg, ras

    return text_fn


# ---------------------------------------------------------------- text → paths
_FONT_CANDIDATES = {
    "black": ["ariblk.ttf", "impact.ttf", "arialbd.ttf"],
    "bold": ["arialbd.ttf", "verdanab.ttf", "segoeuib.ttf", "calibrib.ttf"],
    "bolditalic": ["arialbi.ttf", "verdanaz.ttf", "segoeuiz.ttf", "arialbd.ttf"],
    "italic": ["ariali.ttf", "verdanai.ttf", "segoeuii.ttf", "arial.ttf"],
    "regular": ["arial.ttf", "verdana.ttf", "segoeui.ttf", "calibri.ttf"],
}


def _find_font(family=""):
    """A Windows font file for a family hint: 'Arial Black' → the black
    face, 'Arial Italic' / 'Arial Bold Italic' → italics, 'Arial' /
    'Arial Regular' → the regular face; anything else (the model's usual
    'Arial Bold', 'sans-serif') → bold, as before."""
    base = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    fam = (family or "").lower()
    if "black" in fam:
        kind = "black"
    elif "italic" in fam and "bold" in fam:
        kind = "bolditalic"
    elif "italic" in fam:
        kind = "italic"
    elif "regular" in fam or "light" in fam or fam.strip() in ("arial", "verdana"):
        kind = "regular"
    else:
        kind = "bold"
    for name in _FONT_CANDIDATES[kind] + _FONT_CANDIDATES["bold"]:
        p = base / name
        if p.exists():
            return p
    return None


_font_cache = {}


def _load_font(path):
    from fontTools.ttLib import TTFont
    key = str(path)
    if key not in _font_cache:
        f = TTFont(key)
        _font_cache[key] = (f, f.getGlyphSet(), f.getBestCmap(),
                            f["head"].unitsPerEm, f["hmtx"],
                            getattr(f["OS/2"], "sCapHeight", 0)
                            or int(0.72 * f["head"].unitsPerEm))
    return _font_cache[key]


def _attrs(tag):
    return {k: html.unescape(v) for k, v in
            re.findall(r'([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*"([^"]*)"', tag)}


def _num(v, default=0.0):
    try:
        return float(re.match(r"\s*(-?[0-9.]+)", v).group(1))
    except Exception:
        return default


def text_to_paths(svg_text, font_path=None):
    """Replace every <text>…</text> with glyph outlines so the SVG needs no
    fonts. Honours x, y, font-size, font-family (Arial Black vs bold),
    text-anchor, textLength, dominant-baseline=central/middle, fill and
    transform. Leaves the SVG unchanged when fontTools or a font is missing."""
    try:
        from fontTools.pens.svgPathPen import SVGPathPen
    except Exception:
        return svg_text

    def convert(m):
        tag, inner = m.group(1), m.group(2)
        a = _attrs(tag)
        text = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if not text:
            return ""
        fp = font_path or _find_font(a.get("font-family", ""))
        if fp is None:
            return m.group(0)
        font, glyphset, cmap, upem, hmtx, cap = _load_font(fp)
        size = _num(a.get("font-size", "16"), 16.0)
        s = size / float(upem)
        glyphs, advances = [], []
        for ch in text:
            g = cmap.get(ord(ch)) or cmap.get(ord("?"))
            if g is None:
                continue
            glyphs.append(g)
            advances.append(hmtx[g][0])
        total = sum(advances) * s
        if total <= 0:
            return ""
        sx = 1.0
        if a.get("textLength"):
            tl = _num(a["textLength"], total)
            if tl > 0:
                sx = tl / total
        x, y = _num(a.get("x", "0")), _num(a.get("y", "0"))
        anchor = a.get("text-anchor", "start")
        shown = total * sx
        if anchor == "middle":
            x -= shown / 2.0
        elif anchor == "end":
            x -= shown
        if a.get("dominant-baseline", "") in ("central", "middle"):
            y += cap * s / 2.0
        fill = a.get("fill", "#000000")
        tr = a.get("transform", "")
        parts = []
        cx = 0.0
        for g, adv in zip(glyphs, advances):
            pen = SVGPathPen(glyphset)
            glyphset[g].draw(pen)
            d = pen.getCommands()
            if d:
                parts.append(f'<path transform="translate({cx:.3f} 0)" d="{d}"/>')
            cx += adv
        outer = (f'<g transform="{tr} translate({x:.3f} {y:.3f}) '
                 f'scale({s * sx:.6f} {-s:.6f})" fill="{fill}">')
        return outer + "".join(parts) + "</g>"

    return re.sub(r"<text\b([^>]*)>(.*?)</text>", convert, svg_text, flags=re.S | re.I)


# ---------------------------------------------------------------- the check
_doc_cache = {}


def render_svg_region(svg_path, img_width_px, box, scale, backing=None):
    """The part of an SVG a zoomed preview shows, rendered CRISP at that
    zoom instead of enlarging a raster. `box` is (x0, y0, x1, y1) in the
    pixels of the raster the preview is based on (`img_width_px` wide);
    `scale` is screen pixels per raster pixel. Returns RGB on `backing`
    (the film colour) or RGBA when backing is None."""
    import pymupdf
    key = str(svg_path)
    try:
        mtime = os.path.getmtime(key)
    except OSError:
        mtime = 0
    doc = _doc_cache.get((key, mtime))
    if doc is None:
        for d in _doc_cache.values():
            try:
                d.close()
            except Exception:
                pass
        _doc_cache.clear()
        doc = pymupdf.open(key)
        _doc_cache[(key, mtime)] = doc
    page = doc[0]
    k = img_width_px / max(1.0, page.rect.width)       # raster px per unit
    clip = pymupdf.Rect(box[0] / k, box[1] / k, box[2] / k, box[3] / k) & page.rect
    if clip.is_empty:
        clip = page.rect
    s = scale * k
    pm = page.get_pixmap(matrix=pymupdf.Matrix(s, s), clip=clip, alpha=True)
    im = Image.frombytes("RGBA", (pm.width, pm.height), pm.samples)
    if backing is not None:
        im = Image.alpha_composite(
            Image.new("RGBA", im.size, tuple(int(v) for v in backing) + (255,)),
            im).convert("RGB")
    return im


def render_svg(svg_text, width_px):
    """Rasterise an SVG (pymupdf) to an RGBA image `width_px` wide."""
    import pymupdf
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "d.svg"
        p.write_text(svg_text, encoding="utf-8")
        doc = pymupdf.open(str(p))
        page = doc[0]
        sc = width_px / max(1.0, page.rect.width)
        pm = page.get_pixmap(matrix=pymupdf.Matrix(sc, sc), alpha=True)
        im = Image.frombytes("RGBA", (pm.width, pm.height), pm.samples)
        doc.close()
    return im


def check_against_scan(svg_text, crop_rgba, min_iou=0.45, max_colour=110.0):
    """Does the drawing look like the scan? Returns (ok, iou, colour_dist).
    iou: overlap of the drawn silhouette with the scan's, both padded a
    little so a different font or a hair-thin edge is forgiven;
    colour_dist: mean RGB distance where both are opaque, measured on
    BLURRED pictures so a word set a few pixels off in a different font
    is forgiven while a red decal drawn blue is not. (A colour-share
    histogram test was tried and dropped: a small decal's anti-aliased
    edge pixels made it reject drawings with 90%+ overlap.)"""
    from PIL import ImageFilter
    W, H = crop_rgba.size
    try:
        drawn = render_svg(svg_text, W)
    except Exception:
        return False, 0.0, 999.0
    if drawn.size != (W, H):
        drawn = drawn.resize((W, H), Image.LANCZOS)
    crop = crop_rgba.convert("RGBA")
    a = np.asarray(crop)
    d = np.asarray(drawn)
    pad = max(1, int(round(0.02 * max(W, H))))
    k = 2 * pad + 1

    def mask(arr):
        m = Image.fromarray(((arr[..., 3] > 128) * 255).astype(np.uint8))
        return np.asarray(m.filter(ImageFilter.MaxFilter(k))) > 0

    ms, md = mask(a), mask(d)
    inter = (ms & md).sum()
    union = (ms | md).sum()
    iou = float(inter) / float(union) if union else 0.0
    both = (a[..., 3] > 128) & (d[..., 3] > 128)
    if both.sum() == 0:
        return False, iou, 999.0
    blur = max(1.0, 0.02 * max(W, H))

    def on_white_blurred(im):
        rgb = Image.alpha_composite(
            Image.new("RGBA", im.size, (255, 255, 255, 255)), im).convert("RGB")
        return np.asarray(rgb.filter(ImageFilter.BoxBlur(blur))).astype(np.int32)

    ab, db = on_white_blurred(crop), on_white_blurred(drawn)
    diff = np.sqrt(((ab - db) ** 2).sum(2))
    colour = float(diff[both].mean())
    return (iou >= min_iou and colour <= max_colour), iou, colour
