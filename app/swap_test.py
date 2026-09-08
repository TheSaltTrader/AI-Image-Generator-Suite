"""The gen-then-swap batch and the loading feedback, against a fake engine.

    venv\\Scripts\\python.exe app\\swap_test.py

Subjects:
  ordering    a batch draws EVERY base first, then swaps EVERY base — the
              28 GB swap model loads once per batch, not once per picture
  cancel      cancelling during the swap phase keeps the bases and says
              how many were swapped
  feedback    while a model loads the app is told to sweep the bar and, at
              intervals, how long it has been; the real bar returns with
              the first step
"""

import json
import queue
import sys
import time
import types
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comic_art_creator as cac

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("  " + str(detail)) if detail and not cond else ""), flush=True)


# ---- a fake engine ---------------------------------------------------------
fake_ws_mod = types.ModuleType("websocket")


class _WS:
    def connect(self, *a, **k):
        pass

    def settimeout(self, t):
        pass

    def close(self):
        pass


fake_ws_mod.WebSocket = _WS
sys.modules["websocket"] = fake_ws_mod


class _Resp:
    status_code = 200

    def json(self):
        return {"prompt_id": "pid"}

    def raise_for_status(self):
        pass


cac.requests.post = lambda *a, **k: _Resp()
cac.build_graph = lambda p: {}
real_await = cac.Generator._await_images
cac.Generator._await_images = lambda self, ws, pid, timeout=600: [{"filename": "x.png"}]
cac.Generator._fetch_image = lambda self, meta: Image.new("RGBA", (8, 8), "red")
order = []


def fake_swap(self, ws, base_img, face_paths, seed=0, out_size=None, editor="kontext"):
    order.append(seed)
    return Image.new("RGBA", (8, 8), "blue")


cac.Generator._swap_face_pass = fake_swap


def run(batch, cancel_after_swaps=None):
    order.clear()
    cac.CANCEL.clear()
    q = queue.Queue()
    params = dict(prompt="p", batch=batch, seed=100, random_seed=False,
                  swap_face=["face.png"], swap_editor="qwen", width=8,
                  height=8, model="m", loras=[], negative="", steps=None,
                  cfg=None)
    if cancel_after_swaps is not None:
        def swap_then_cancel(self, ws, base_img, face_paths, seed=0,
                             out_size=None, editor="kontext"):
            order.append(seed)
            if len(order) >= cancel_after_swaps:
                cac.CANCEL.set()
            return Image.new("RGBA", (8, 8), "blue")
        cac.Generator._swap_face_pass = swap_then_cancel
    else:
        cac.Generator._swap_face_pass = fake_swap
    cac.Generator(q).run(params)
    msgs = []
    while not q.empty():
        msgs.append(q.get())
    return msgs


print("ordering")
msgs = run(3)
images = [(m[2].get("user_prompt", "base"), m[2]["seed"])
          for m in msgs if m[0] == "image"]
check("three bases come first, then three swaps",
      [u for u, _ in images] == ["base"] * 3 + ["face-swapped"] * 3, images)
check("each swap keeps its base's seed, in order",
      [s for u, s in images if u == "face-swapped"] == [100, 101, 102])
check("the swap ran once per base, after all bases", order == [100, 101, 102],
      order)
check("the swap phase says which picture it is on",
      any("Swapping the face into picture 2/3" in str(m[1])
          for m in msgs if m[0] == "status"))
check("the run ends with done", msgs[-1][0] == "done")
check("a single picture still swaps", len([1 for m in run(1) if m[0] == "image"]) == 2)

print("cancel")
msgs = run(3, cancel_after_swaps=2)
images = [(m[2].get("user_prompt", "base"), m[2]["seed"])
          for m in msgs if m[0] == "image"]
check("cancelling in the swap phase keeps every base",
      [u for u, _ in images].count("base") == 3)
check("…and the swaps done so far", [u for u, _ in images].count("face-swapped") == 2)
check("…and says how many were swapped",
      any("2 of 3 swapped" in str(m[1]) for m in msgs if m[0] == "status"),
      [m[1] for m in msgs if m[0] == "status"][-2:])
cac.CANCEL.clear()

print("feedback")


class _WS2:
    """A websocket that plays a script: model loading, then steps."""

    def __init__(self, script):
        self.script = list(script)

    def settimeout(self, t):
        pass

    def recv(self):
        if self.script:
            item = self.script.pop(0)
            if item == "tick":
                raise TimeoutError("timed out")
            return json.dumps(item)
        raise TimeoutError("timed out")


cac.api_get = lambda path: {"pid": {"outputs": {"9": {"images": [{"filename": "o.png"}]}}}}
q = queue.Queue()
gen = cac.Generator(q)
ws = _WS2([{"type": "executing", "data": {"prompt_id": "pid", "node": "1"}},
           "tick", "tick",
           {"type": "progress", "data": {"value": 1, "max": 4}},
           {"type": "progress", "data": {"value": 4, "max": 4}},
           {"type": "executing", "data": {"prompt_id": "pid", "node": None}}])
real_time = cac.time.time
clock = [1000.0]
cac.time.time = lambda: clock.__setitem__(0, clock[0] + 6) or clock[0]
try:
    imgs = real_await(gen, ws, "pid", timeout=600)
finally:
    cac.time.time = real_time
msgs = []
while not q.empty():
    msgs.append(q.get())
kinds = [(m[0], m[1] if m[0] in ("progress_mode",) else None) for m in msgs]
check("the images come back", imgs == [{"filename": "o.png"}], imgs)
check("a model load tells the bar to sweep",
      ("progress_mode", "loading") in kinds, kinds)
check("…and the first step brings the real bar back, before the step lands",
      kinds.index(("progress_mode", "steps")) < [i for i, m in enumerate(msgs)
                                                  if m[0] == "progress"][0])
check("elapsed time is reported while loading",
      any("elapsed" in str(m[1]) for m in msgs if m[0] == "status"),
      [m[1] for m in msgs if m[0] == "status"])

print("composite")
import random as _rnd
from PIL import ImageDraw as _ID
W, H = 512, 768


def photo(seed, head=None, noise=0):
    """A 'photoreal' base: soft gradient + grain; optional head blob colour."""
    rng = _rnd.Random(seed)
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        for x in range(W):
            v = 90 + (x * 60) // W + (y * 40) // H
            px[x, y] = (v + rng.randint(-noise, noise), v + rng.randint(-noise, noise),
                        v + rng.randint(-noise, noise))
    if head:
        _ID.Draw(img).ellipse([176, 120, 336, 300], fill=head)
    return img


# (a) a stylised base with a strongly changed head: composite, base kept
base = photo(1, head=(200, 170, 150))
swapped = photo(1, head=(40, 60, 200))                 # very different head
stats = {}
out = cac.swap_composite(base, swapped, stats=stats)
check("a clearly changed head is composited, not discarded",
      stats.get("used") == "composite" and stats["mask"] > 0.02, stats)
bl, ol = base.convert("L").load(), out.convert("L").load()
check("…keeping the base's exact pixels outside the head",
      all(abs(bl[x, y] - ol[x, y]) < 2 for x, y in ((20, 20), (490, 740), (256, 700))))
check("…and taking the swap's pixels inside it", abs(ol[256, 210] - swapped.convert("L").load()[256, 210]) < 3)

# (b) the reported case: a photoreal base the swap model re-rendered all
# over (global grain), with a subtle face change — the old rule's bar
# (2.4x mean) rose above the face; the raw swap must be used, not the base
base2 = photo(2, head=(200, 170, 150))
subtle = photo(3, head=(196, 160, 150), noise=18)      # global noise, slight face
stats = {}
out2 = cac.swap_composite(base2, subtle, stats=stats)
check("a subtle swap on a re-rendered base is not thrown away",
      stats.get("used") == "raw", stats)
check("…so the result is the swap, not the base",
      abs(out2.convert("L").load()[256, 210] - subtle.convert("L").load()[256, 210]) < 3
      and abs(out2.convert("L").load()[256, 210] - base2.convert("L").load()[256, 210]) > 0)

# (c) the cap: a noisy re-render with a REAL new head still keeps the head
noisy = photo(4, head=(40, 60, 200), noise=30)
stats = {}
out3 = cac.swap_composite(base2, noisy, stats=stats)
check("the threshold is capped so a noisy re-render cannot hide a real head",
      stats.get("thr") <= cac.SWAP_THR_CAP and stats.get("used") == "composite"
      and stats["mask"] > 0.02, stats)
check("stats carry the numbers for the log",
      all(k in stats for k in ("mean", "thr", "mask", "used")))

print()
print("4 subjects enumerated, %d checks passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
