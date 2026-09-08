"""End-to-end: LoRA files and RAG maps on a LIVE engine, through the app's
own graph builder and retrieval code — the path a generation takes when a
LoRA is ticked and a RAG map is loaded.

    venv\\Scripts\\python.exe app\\rag_lora_e2e_test.py
    set CAC_ENGINE_PORT=8189   (to use an engine started on another port)

Subjects, enumerated so what is not covered is counted:

  style support   the engine registers the IP-Adapter node and sees its
                  models — the exact test the app's _style_support_ok makes
  map loading     a cbac-ragmap/1 map parses, its LoRA resolves to the
                  installed file, its images resolve to real files
  retrieval       a prompt retrieves top_k entries carrying image paths
  LoRA only       SDXL + a LoRA renders (LoraLoader in the chain)
  LoRA + RAG      the retrieved images are uploaded and guide the render
                  (LoraLoader -> IPAdapterUnifiedLoader -> IPAdapter), and
                  the result differs from the LoRA-only render at the same
                  seed — the references changed the picture
  LoRA + embeds   an embeddings-only map (.ipadpt) guides the same way
  combined        embeds and images chained off ONE loader (v1.20 rule)
  Flux + LoRA     a Flux LoRA renders on the Flux family
  family gate     RAG image guidance is SDXL-only (the UI's rule)

Every render is fetched back and decoded, not just reported by the engine.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import requests
from PIL import Image, ImageChops

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comic_art_creator as cac

PORT = int(os.environ.get("CAC_ENGINE_PORT") or cac.ENGINE_PORT)
cac.ENGINE_PORT = PORT
cac.ENGINE_URL = "http://127.0.0.1:%d" % PORT
URL = cac.ENGINE_URL
ROOT = Path(__file__).resolve().parent.parent

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("  " + str(detail)) if detail and not cond else ""), flush=True)


def render(params, label, timeout=600):
    """Queue a graph, wait, fetch the PNG back. Returns (Image, graph)."""
    g = cac.build_graph(params)
    r = requests.post(URL + "/prompt", json={"prompt": g,
                                             "client_id": "rag-lora-e2e"},
                      timeout=30)
    if not r.ok:
        raise RuntimeError("%s: engine refused the graph: %s"
                           % (label, r.text[:600]))
    pid = r.json()["prompt_id"]
    t0 = time.time()
    while True:
        time.sleep(2)
        h = requests.get(URL + "/history/" + pid, timeout=15).json()
        if pid in h:
            st = h[pid].get("status", {})
            if st.get("status_str") == "error":
                raise RuntimeError("%s: %s" % (label, json.dumps(
                    st.get("messages", []))[:800]))
            imgs = [i for o in h[pid].get("outputs", {}).values()
                    for i in o.get("images", [])]
            if imgs:
                break
        if time.time() - t0 > timeout:
            raise RuntimeError(label + ": timed out")
    i = imgs[0]
    v = requests.get(URL + "/view", params={"filename": i["filename"],
                                            "subfolder": i.get("subfolder", ""),
                                            "type": i.get("type", "output")},
                     timeout=60)
    v.raise_for_status()
    img = Image.open(io.BytesIO(v.content))
    img.load()
    print("       %s: %.1fs -> %s %s" % (label, time.time() - t0,
                                        i["filename"], img.size), flush=True)
    return img, g


def differs(a, b):
    """Mean absolute pixel difference, 0-255."""
    d = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    hist = d.convert("L").histogram()
    total = sum(hist)
    return sum(i * n for i, n in enumerate(hist)) / total if total else 0


def node_types(g):
    return [n["class_type"] for n in g.values()]


# ---------------------------------------------------------- style support
print("style support")
try:
    requests.get(URL + "/system_stats", timeout=5)
except requests.RequestException as e:
    print("engine is not running on port %d (%s)" % (PORT, e))
    sys.exit(2)
try:
    cac.api_get("/object_info/IPAdapterUnifiedLoader")
    node_ok = True
except Exception:
    node_ok = False
check("engine registers IPAdapterUnifiedLoader", node_ok)
check("ipadapter model is on disk", bool(cac.scan_models("ipadapter")))
check("clip_vision model is on disk", bool(cac.scan_models("clip_vision")))
loras_seen = cac._api_choices("LoraLoader", "lora_name")
SDXL_LORA = "SDXL_GraphicNovel.safetensors"
FLUX_LORA = "Flux_RetroComic_v2.safetensors"
check("engine sees the SDXL LoRA", SDXL_LORA in loras_seen, loras_seen)
check("engine sees the Flux LoRA", FLUX_LORA in loras_seen, loras_seen)
SDXL = "DreamShaperXL-Turbo-v2.1.safetensors"
FLUX = "flux1-dev-fp8.safetensors"

# ------------------------------------------------------------ map loading
print("map loading")
td = Path(tempfile.mkdtemp(prefix="ragmap_"))
imgdir = td / "images"
imgdir.mkdir()
srcs = sorted((ROOT / "output").glob("*.png"))[:4]
check("four example images available for the map", len(srcs) == 4, len(srcs))
entries = []
words = [["hero", "rooftop", "night", "city"], ["heroine", "crater", "rain"],
         ["detective", "streetlamp", "noir"], ["robot", "punch", "battle"]]
for i, s in enumerate(srcs):
    name = "%04d.png" % (i + 1)
    shutil.copyfile(s, imgdir / name)
    entries.append({"image": name, "keywords": words[i],
                    "caption": " ".join(words[i]) + " comic art"})
mp = td / "test.ragmap.json"
mp.write_text(json.dumps({
    "schema": "cbac-ragmap/1", "name": "E2E map", "lora": SDXL_LORA,
    "trigger": "graphic novel", "image_dir": "images", "weight": 0.8,
    "top_k": 2, "entries": entries}, indent=1), encoding="utf-8")
rag = cac.load_ragmap(mp)
check("map parses with its name", rag.get("name") == "E2E map")
check("map's LoRA resolves to the installed file",
      cac.ragmap_lora(rag) == SDXL_LORA, cac.ragmap_lora(rag))
check("every entry resolves to a real image file",
      all(Path(e["_path"]).is_file() for e in rag["entries"]))
check("an image map is not read as embeddings-only", not rag["_embeds_only"])

# --------------------------------------------------------------- retrieval
print("retrieval")
PROMPT = ("a caped hero on a city rooftop at night, bold inks, "
          "halftone dots")
hits = cac.ragmap_retrieve(rag, PROMPT)
check("retrieval returns top_k entries", len(hits) == 2, len(hits))
check("the best hit is the rooftop entry",
      bool(hits) and hits[0]["image"] == "0001.png", hits and hits[0]["image"])
check("every hit carries an image path", all(h.get("_path") for h in hits))

# --------------------------------------------------------------- renders
gen = cac.Generator.__new__(cac.Generator)
base = dict(prompt="graphic novel, " + PROMPT, negative="photo, blurry",
            width=832, height=1216, seed=4242, steps=None, cfg=None)
try:
    print("LoRA only")
    a, ga = render(dict(base, model=SDXL, loras=[(SDXL_LORA, 0.9)]),
                   "SDXL + LoRA")
    check("SDXL + LoRA renders at the requested size", a.size == (832, 1216))
    check("LoraLoader is in the chain", "LoraLoader" in node_types(ga))
    check("no IP-Adapter node without a map",
          "IPAdapterUnifiedLoader" not in node_types(ga))

    print("LoRA + RAG images")
    names = [gen._upload_ref(h["_path"]) for h in hits]
    check("retrieved images upload to the engine", len(names) == 2, names)
    b, gb = render(dict(base, model=SDXL, loras=[(SDXL_LORA, 0.9)],
                        style_ref_names=names, style_weight=0.8),
                   "SDXL + LoRA + RAG images")
    t = node_types(gb)
    check("LoRA + RAG renders", b.size == (832, 1216))
    check("the chain is LoraLoader -> UnifiedLoader -> IPAdapter",
          "LoraLoader" in t and "IPAdapterUnifiedLoader" in t
          and "IPAdapter" in t and t.count("LoadImage") == 2, t)
    check("the loader feeds off the LoRA'd model",
          gb["50"]["inputs"]["model"][0] == "20", gb["50"]["inputs"])
    d_ab = differs(a, b)
    check("the references changed the picture (same seed)", d_ab > 5,
          "mean diff %.1f" % d_ab)

    print("LoRA + embeddings-only map")
    embeds = sorted(p.name for p in (ROOT / "ComfyUI" / "input").glob("*.ipadpt"))
    check("precomputed .ipadpt embeds are available", len(embeds) >= 1, embeds)
    c, gc = render(dict(base, model=SDXL, loras=[(SDXL_LORA, 0.9)],
                        style_embed_names=embeds[:3], style_weight=0.8),
                   "SDXL + LoRA + embeds")
    t = node_types(gc)
    check("embeds chain: LoadEmbeds -> CombineEmbeds -> IPAdapterEmbeds",
          "IPAdapterLoadEmbeds" in t and "IPAdapterCombineEmbeds" in t
          and "IPAdapterEmbeds" in t, t)
    d_ac = differs(a, c)
    check("the embeds changed the picture (same seed)", d_ac > 5,
          "mean diff %.1f" % d_ac)

    print("combined: embeds + images off one loader")
    dimg, gd = render(dict(base, model=SDXL, loras=[(SDXL_LORA, 0.9)],
                           style_ref_names=names, style_embed_names=embeds[:2],
                           style_weight=0.8), "SDXL + LoRA + embeds + images")
    t = node_types(gd)
    check("one UnifiedLoader feeds both stages",
          t.count("IPAdapterUnifiedLoader") == 1 and "IPAdapterEmbeds" in t
          and "IPAdapter" in t, t)
    check("the image stage chains on the embeds stage",
          gd["51"]["inputs"]["model"][0] == "59", gd["51"]["inputs"])
    check("combined render decodes", dimg.size == (832, 1216))

    print("Flux + LoRA")
    e, ge = render(dict(base, model=FLUX, loras=[(FLUX_LORA, 0.8)], negative=""),
                   "Flux + LoRA")
    check("Flux + LoRA renders", e.size == (832, 1216))
    check("Flux chain carries the LoRA and FluxGuidance",
          "LoraLoader" in node_types(ge) and "FluxGuidance" in node_types(ge))
except Exception as ex:
    check("render step raised", False, repr(ex))

# ------------------------------------------------------------ family gate
print("family gate")
check("Flux is not an SDXL family (RAG images are skipped there)",
      cac.model_family(FLUX) in ("flux", "schnell"))
check("DreamShaper Turbo is an SDXL family", cac.model_family(SDXL) == "turbo")
check("Juggernaut is an SDXL family",
      cac.model_family("Juggernaut-XL-v9.safetensors") == "sdxl")

shutil.rmtree(td, ignore_errors=True)
SUBJECTS = ["style support", "map loading", "retrieval", "LoRA only",
            "LoRA + RAG images", "LoRA + embeddings-only map",
            "combined: embeds + images", "Flux + LoRA", "family gate"]
print()
print("%d subjects enumerated, %d checks passed, %d failed"
      % (len(SUBJECTS), len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: " + f)
sys.exit(1 if FAIL else 0)
