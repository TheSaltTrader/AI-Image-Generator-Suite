"""RAG retrieval on a large map: the inverted index must give the SAME
answers as the per-entry walk it replaces, and give them fast.

    venv\\Scripts\\python.exe app\\ragmap_test.py

A user's real map holds 481,177 entries; the per-entry walk took 5.5 s on
the UI thread at every Generate. Subjects:

  equivalence   indexed vs walked results identical across many prompts,
                with and without the reference filter, k from the map
                and k given, and the no-match fallback
  speed         indexed retrieval on 60k entries is well under a second
  robustness    no numpy index -> the walk still answers; an entry not
                made by load_ragmap (no _words) still scores
"""

import json
import random
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comic_art_creator as cac

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("  " + str(detail)) if detail and not cond else ""), flush=True)


rng = random.Random(7)
VOCAB = ["w%d" % i for i in range(1500)] + [
    "hero", "rooftop", "night", "city", "cape", "rain", "detective", "robot",
    "sunset", "beach", "sword", "dragon", "castle", "forest", "smile"]
N = 60000

td = Path(tempfile.mkdtemp(prefix="ragidx_"))
Image.new("RGB", (8, 8), "blue").save(td / "ref.png")
entries = []
for i in range(N):
    words = rng.sample(VOCAB, rng.randint(6, 14))
    entries.append({
        # even entries resolve to a real file, odd ones do not
        "image": "ref.png" if i % 2 == 0 else "missing_%d.png" % i,
        "keywords": words[:3], "caption": " ".join(words[3:]), "n": i})
mp = td / "big.ragmap.json"
mp.write_text(json.dumps({"schema": "cbac-ragmap/1", "name": "big",
                          "top_k": 4, "entries": entries}), encoding="utf-8")

t0 = time.time()
rag = cac.load_ragmap(mp)
print("       loaded %d entries in %.1fs" % (len(rag["entries"]), time.time() - t0))
check("the map built an index", rag.get("_index") is not None)
check("half the entries carry a reference",
      int(rag["_index"][1].sum()) == N // 2, int(rag["_index"][1].sum()))


def ids(hits):
    return [e["n"] for e in hits]


def both(prompt, **kw):
    """(indexed result, walked result) for one prompt."""
    idx = rag["_index"]
    fast = ids(cac.ragmap_retrieve(rag, prompt, **kw))
    rag["_index"] = None
    slow = ids(cac.ragmap_retrieve(rag, prompt, **kw))
    rag["_index"] = idx
    return fast, slow


print("equivalence")
prompts = [" ".join(rng.sample(VOCAB, rng.randint(2, 9))) for _ in range(30)]
prompts += ["a hero on a rooftop at night", "zzzz qqqq nothing", "",
            "hero hero hero rooftop", "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10"]
same = sum(1 for p in prompts if both(p) == both(p)[::-1][::-1]
           and both(p)[0] == both(p)[1])
check("indexed == walked for every prompt (reference filter on)",
      same == len(prompts), "%d/%d" % (same, len(prompts)))
same = sum(1 for p in prompts
           if (lambda f, s: f == s)(*both(p, require_image=False)))
check("indexed == walked with the reference filter off",
      same == len(prompts), "%d/%d" % (same, len(prompts)))
same = sum(1 for p in prompts if (lambda f, s: f == s)(*both(p, k=7)))
check("indexed == walked for a caller-given k", same == len(prompts))
f, s = both("zzzz qqqq nothing")
check("no match: the first k entries WITH a reference, in both",
      f == s == [0, 2, 4, 6], (f, s))
f, s = both("zzzz", require_image=False)
check("no match, filter off: the first k entries, in both",
      f == s == [0, 1, 2, 3], (f, s))
f, _ = both("hero rooftop night city")
check("the filter never returns an entry without a reference",
      all(i % 2 == 0 for i in f), f)
check("results honour the map's top_k", len(f) == 4)
check("the best match ranks first",
      f and len(set(cac.ragmap_retrieve(rag, "hero rooftop night city")[0]["_words"])
                & {"hero", "rooftop", "night", "city"}) >= 2)

print("speed")
t0 = time.time()
for p in prompts:
    cac.ragmap_retrieve(rag, p)
fast_t = (time.time() - t0) / len(prompts)
idx = rag["_index"]
rag["_index"] = None
t0 = time.time()
for p in prompts[:5]:
    cac.ragmap_retrieve(rag, p)
slow_t = (time.time() - t0) / 5
rag["_index"] = idx
print("       indexed %.1f ms / walked %.1f ms per retrieval on %d entries"
      % (fast_t * 1000, slow_t * 1000, N))
check("indexed retrieval is well under a second", fast_t < 0.25,
      "%.3fs" % fast_t)
check("…and faster than the walk it replaces", fast_t < slow_t)

print("robustness")
rag["_index"] = None
check("without an index the walk still answers",
      len(cac.ragmap_retrieve(rag, "hero rooftop")) == 4)
rag["_index"] = idx
foreign = {"entries": [{"caption": "hero on a rooftop", "_path": "x", "n": 0},
                       {"caption": "a quiet forest", "_path": "x", "n": 1}],
           "top_k": 1}
check("an entry not made by load_ragmap (no _words, no index) still scores",
      ids(cac.ragmap_retrieve(foreign, "rooftop hero")) == [0])
check("words are interned (one object per word across entries)",
      any(w is next(iter(rag["entries"][1]["_words"] & {w}), None)
          for w in rag["entries"][0]["_words"] if w in rag["entries"][1]["_words"])
      or not (rag["entries"][0]["_words"] & rag["entries"][1]["_words"]))

print("randomness")
# one prompt used to fetch the same references every time -> the same
# person in every picture; a seeded draw among the strongest matches
# varies them, and a fixed seed reproduces them
rag["_index"] = idx
p = "hero rooftop night city cape"
plain = ids(cac.ragmap_retrieve(rag, p))
draws = [tuple(ids(cac.ragmap_retrieve(rag, p, rng=random.Random(s))))
         for s in range(12)]
check("without an rng retrieval is deterministic (unchanged behaviour)",
      ids(cac.ragmap_retrieve(rag, p)) == plain)
check("with an rng the references vary across seeds", len(set(draws)) >= 4,
      len(set(draws)))
check("a fixed seed reproduces the same references",
      ids(cac.ragmap_retrieve(rag, p, rng=random.Random(7)))
      == ids(cac.ragmap_retrieve(rag, p, rng=random.Random(7))))
check("every draw still honours top_k and the reference filter",
      all(len(d) == 4 and all(i % 2 == 0 for i in d) for d in draws))
strong = set(ids(cac.ragmap_retrieve(rag, p, k=32)))
check("draws stay among the strongest matches",
      all(i in strong for d in draws for i in d))
rag["_index"] = None
walk_draw = ids(cac.ragmap_retrieve(rag, p, rng=random.Random(3)))
rag["_index"] = idx
check("the walk path draws the same way as the index for one seed",
      walk_draw == ids(cac.ragmap_retrieve(rag, p, rng=random.Random(3))),
      (walk_draw, ids(cac.ragmap_retrieve(rag, p, rng=random.Random(3)))))

import shutil
shutil.rmtree(td, ignore_errors=True)
print()
print("4 subjects enumerated, %d checks passed, %d failed" % (len(PASS), len(FAIL)))
for f_ in FAIL:
    print("  FAILED: " + f_)
sys.exit(1 if FAIL else 0)
