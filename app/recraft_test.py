"""Recraft vectorize (fal.ai) client — fake HTTP, no key, no cost."""
import base64
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from PIL import Image, ImageDraw

import recraft_vectorize as rv

PASS = FAIL = 0


def check(name, ok, info=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ok  ", name)
    else:
        FAIL += 1
        print("  FAIL", name, f" ({info})" if info else "")


# a decal: red square with a white dot, on transparent
crop = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
d = ImageDraw.Draw(crop)
d.rectangle([20, 10, 100, 70], fill=(200, 20, 30, 255))
d.ellipse([50, 30, 70, 50], fill=(255, 255, 255, 255))

print("prepare")
png, scale = rv._prepare(crop)
sent = Image.open(__import__("io").BytesIO(png))
check("the decal is enlarged inside fal's limits, on magenta",
      min(sent.size) > 256 and max(sent.size) < 4096 and abs(scale - sent.width / 120.0) < 1e-6
      and sent.getpixel((2, 2)) == (255, 0, 255), (sent.size, scale))


class _Resp:
    def __init__(self, code, js=None, text=""):
        self.status_code, self._js, self.text = code, js, text

    def json(self):
        return self._js


class _Session:
    def __init__(self, code=200, svg=None, delay=0.0):
        self.code, self.svg, self.delay, self.posts = code, svg, delay, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json, headers))
        if self.delay:
            time.sleep(self.delay)
        if self.code != 200:
            return _Resp(self.code, None, "insufficient balance" if self.code == 402 else "boom")
        W, H = Image.open(__import__("io").BytesIO(
            base64.b64decode(json["image_url"].split(",", 1)[1]))).size
        sx, sy = W / 120.0, H / 80.0
        svg = self.svg or (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">'
            f'<rect x="0" y="0" width="{W}" height="{H}" fill="#FF00FF"/>'
            f'<path d="M{20*sx} {10*sy} L{100*sx} {10*sy} L{100*sx} {70*sy} L{20*sx} {70*sy} Z" fill="rgb(200,20,30)"/>'
            f'<circle cx="{60*sx}" cy="{40*sy}" r="{10*sx}" fill="#ffffff"/></svg>')
        uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
        return _Resp(200, {"image": {"url": uri, "content_type": "image/svg+xml"}})


print("the call")
ses = _Session()
svg = rv.vectorize_decal(crop, "fal-key-not-real", session=ses)
url, body, hdr = ses.posts[-1]
check("it posts the decal as a data URI with the key header",
      url == rv.ENDPOINT and body["image_url"].startswith("data:image/png;base64,")
      and hdr["Authorization"] == "Key fal-key-not-real")
check("the magenta backing is removed, the drawing mapped back to crop pixels",
      "FF00FF" not in svg.upper() and 'viewBox="0 0 120.000 80.000"' in svg, svg[:200])

print("vector_fn")
st = {}
fn = rv.make_vector_fn("k", 300, stats=st, session=_Session())
got = fn(crop, None, 0.4, 0.2667)
ras = np.asarray(got[1]) if got else None
check("a good result is checked, sized in inches and rendered transparent",
      got is not None and 'width="0.4000in"' in got[0] and st["ok"] == 1
      and abs(st["cost"] - 0.01) < 1e-9 and ras[1, 1, 3] == 0 and ras[ras.shape[0] // 2, ras.shape[1] // 2, 3] == 255,
      (st, ras[1, 1] if ras is not None else None))
st2 = {}
bad = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300"><rect x="0" y="0" width="40" height="30" fill="#00ff00"/></svg>'
check("a drawing that misses the decal falls back to the trace",
      rv.make_vector_fn("k", 300, stats=st2, session=_Session(svg=bad))(crop, None, 0.4, 0.27) is None
      and st2["fallback"] == 1)
try:
    rv.make_vector_fn("k", 300, stats={}, session=_Session(code=401))(crop, None, 0.4, 0.27)
    stop = ""
except RuntimeError as e:
    stop = str(e)
check("a rejected key stops the run with a clear message", "key was rejected" in stop, stop)
try:
    rv.make_vector_fn("k", 300, stats={}, session=_Session(code=402))(crop, None, 0.4, 0.27)
    stop2 = ""
except RuntimeError as e:
    stop2 = str(e)
check("used-up credits stop the run and say where to top up", "top up" in stop2, stop2)
st3 = {}
check("a server error falls back to the trace",
      rv.make_vector_fn("k", 300, stats=st3, session=_Session(code=500))(crop, None, 0.4, 0.27) is None
      and st3["fallback"] == 1)
flag = threading.Event()
threading.Timer(0.3, flag.set).start()
t0 = time.time()
r = rv.make_vector_fn("k", 300, stats={}, cancelled=flag.is_set,
                      session=_Session(delay=5))(crop, None, 0.4, 0.27)
check("Cancel abandons a call in flight at once", r is None and time.time() - t0 < 1.0)
check("no key is kept in this module's source",
      "sk-" not in Path(rv.__file__).read_text(encoding="utf-8").replace("sk-ant", ""))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
