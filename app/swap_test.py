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

print()
print("3 subjects enumerated, %d checks passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
