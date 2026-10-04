"""Tests for api_cache: a repeated paid request is answered from disk for $0."""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_cache
import vector_redraw as vr

PASS = FAIL = 0


def check(name, ok, info=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ok  ", name)
    else:
        FAIL += 1
        print("  FAIL", name, info)


class _Inner:
    def __init__(self, stop="end_turn"):
        self.calls = 0
        self.stop = stop
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ANSWER %d" % self.calls)],
            stop_reason=self.stop, model=kw.get("model"),
            usage=SimpleNamespace(input_tokens=1000, output_tokens=100,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0))


tmp = tempfile.mkdtemp()
print("anthropic answers")
api_cache.set_dir(None)
check("no cache folder: the client is returned as it is",
      api_cache.wrap(_Inner()).__class__ is _Inner)
api_cache.set_dir(tmp)
inner = _Inner()
c = api_cache.wrap(inner)
kw = dict(model="claude-opus-5-5", max_tokens=50,
          messages=[{"role": "user", "content": "read this"}])
r1 = c.messages.create(**kw)
r2 = c.messages.create(**dict(kw, timeout=99))      # timeout does not matter
check("the same request twice reaches the service once", inner.calls == 1, inner.calls)
check("…and the second answer is the same text", r2.content[0].text == r1.content[0].text)
check("…and costs $0", vr.estimate_cost("claude-opus-5-5", r2.usage) == 0.0
      and vr.estimate_cost("claude-opus-5-5", r1.usage) > 0)
c.messages.create(**dict(kw, messages=[{"role": "user", "content": "other"}]))
check("a different request is paid for", inner.calls == 2, inner.calls)
inner2 = _Inner(stop="refusal")
c2 = api_cache.wrap(inner2)
kw2 = dict(kw, model="claude-sonnet-5-5")
c2.messages.create(**kw2)
c2.messages.create(**kw2)
check("a refusal is never remembered", inner2.calls == 2, inner2.calls)

print("recraft answers")
import recraft_vectorize as rv
from PIL import Image, ImageDraw
crop = Image.new("RGBA", (120, 80), (0, 0, 0, 0))
ImageDraw.Draw(crop).rectangle((10, 10, 110, 70), fill=(200, 30, 40, 255))
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 213">'
       '<path d="M27 27 L293 27 L293 186 L27 186 Z" fill="#c81e28"/></svg>')


class _Sess:
    def __init__(self):
        self.posts = 0

    def post(self, *a, **k):
        self.posts += 1
        import base64
        return SimpleNamespace(status_code=200, text="", json=lambda: {
            "image": {"url": "data:image/svg+xml;base64,"
                      + base64.b64encode(SVG.encode()).decode()}})


sess = _Sess()
st = {}
fn = rv.make_vector_fn("fal-test-key-not-real", 300, stats=st, session=sess)
a = fn(crop, None, 0.4, 0.27)
b = fn(crop, None, 0.4, 0.27)
check("the same decal twice is sent to Recraft once", sess.posts == 1, sess.posts)
check("…and charged once", abs(st.get("cost", 0) - rv.PRICE_PER_IMAGE) < 1e-9, st)
check("…and both drawings are the same",
      a is not None and b is not None and a[0] == b[0])
api_cache.set_dir(None)
sess2 = _Sess()
fn2 = rv.make_vector_fn("fal-test-key-not-real", 300, stats={}, session=sess2)
fn2(crop, None, 0.4, 0.27); fn2(crop, None, 0.4, 0.27)
check("with the cache off every call is made", sess2.posts == 2, sess2.posts)
check("only hashes and answers are stored (no key in the cache folder)",
      not any("fal-test-key" in p.read_text(errors="ignore")
              for p in Path(tmp).rglob("*.json")))


print("batch mode")
api_cache.set_dir(tempfile.mkdtemp())


class _Batches:
    def __init__(self):
        self.created = []
        self.cancelled = False

    def create(self, requests):
        self.created.append(list(requests))
        return SimpleNamespace(id="b1")

    def retrieve(self, bid):
        return SimpleNamespace(processing_status="ended", request_counts=None)

    def cancel(self, bid):
        self.cancelled = True

    def results(self, bid):
        for r in self.created[-1]:
            yield SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(
                type="succeeded", message=SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="BATCHED " + r["custom_id"])],
                    stop_reason="end_turn", model="claude-opus-5-5",
                    usage=SimpleNamespace(input_tokens=1000, output_tokens=100,
                                          cache_read_input_tokens=0,
                                          cache_creation_input_tokens=0))))


class _BInner(_Inner):
    def __init__(self):
        super().__init__()
        self.batches = _Batches()
        self.messages = self


binner = _BInner()
bclient = api_cache.wrap(binner)
reqs = [dict(model="claude-opus-5-5", max_tokens=50,
             messages=[{"role": "user", "content": "word %d" % i}]) for i in range(3)]
reqs.append(dict(reqs[0]))                         # a duplicate
sent, stored, cost = vr.batch_prefetch(bclient, reqs)
check("a batch sends each different request once and stores every answer",
      sent == 3 and stored == 3, (sent, stored))
check("…at half price", abs(cost - 0.5 * 3 * vr.estimate_cost(
    "claude-opus-5-5", SimpleNamespace(input_tokens=1000, output_tokens=100))) < 1e-9, cost)
r = bclient.messages.create(**reqs[1])
check("the redraw then finds the batched answer in the cache, free",
      binner.calls == 0 and r.content[0].text == "BATCHED r1"
      and vr.estimate_cost("claude-opus-5-5", r.usage) == 0.0, (binner.calls, r.content[0].text))
sent2, _s2, _c2 = vr.batch_prefetch(bclient, reqs)
check("a second batch of the same requests sends nothing", sent2 == 0, sent2)


class _SlowBatches(_Batches):
    def retrieve(self, bid):
        return SimpleNamespace(processing_status="in_progress", request_counts=None)


binner2 = _BInner()
binner2.batches = _SlowBatches()
try:
    vr.batch_prefetch(api_cache.wrap(binner2),
                      [dict(model="m", max_tokens=5, messages=[{"role": "user", "content": "x"}])],
                      cancelled=lambda: True, poll_s=0)
    ok = False
except vr.Cancelled:
    ok = True
check("Cancel stops the wait and cancels the batch", ok and binner2.batches.cancelled)

rec = vr.RecordingClient()
import decals as _d
from PIL import Image as _I, ImageDraw as _D
sheet = _I.new("RGBA", (500, 160), (0, 0, 0, 0))
_D.Draw(sheet).text((20, 40), "DANGER", fill=(255, 255, 255, 255))
big = sheet.resize((1500, 480))
dry = vr.make_text_fn(rec, "claude-opus-5-5", 300, stats={})
out = _d.redraw_sheet(big, None, native_dpi=300, target_dpi=300, text_fn=dry,
                      dry_text=True, reuse_copies=False)
check("the dry pass collects lettering requests without drawing anything",
      out is not None and all(it["source"] in ("dry", "text") for it in out["items"]),
      [it["source"] for it in (out or {}).get("items", [])])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
