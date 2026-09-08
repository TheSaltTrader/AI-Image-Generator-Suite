# RAG map format (`.ragmap.json`)

A **RAG map** pairs a trained LoRA with a set of example images. When you
load one in the AI Image Generator Suite's first section, the app retrieves
the example images most relevant to your prompt and feeds them as
**visual guidance (IP-Adapter)** while it generates — so the output stays
faithful to the LoRA's subject/style. It also auto-applies the paired
LoRA and its trigger word.

A trainer should emit one of these next to every LoRA it produces.
[Laura-Trainer](../Laura-Trainer) writes one automatically when "Also
build a retrieval map beside the LoRA" is ticked.

## Schema — `cbac-ragmap/1`

```json
{
  "schema": "cbac-ragmap/1",
  "name": "My Character",
  "lora": "SDXL_MyCharacter_v1.safetensors",
  "trigger": "mychar",
  "image_dir": "images",
  "weight": 0.8,
  "top_k": 4,
  "entries": [
    {
      "image": "0001.png",
      "keywords": ["red cape", "flying", "hero", "sky"],
      "caption": "the hero flying through the sky in a red cape"
    },
    {
      "image": "0002.png",
      "keywords": ["portrait", "smiling", "close up"],
      "caption": "a close-up portrait of the hero smiling"
    }
  ]
}
```

### Fields

| Field | Required | Meaning |
|---|---|---|
| `schema` | recommended | `"cbac-ragmap/1"`. |
| `name` | optional | Display name. Defaults to the map's folder or filename, with a trailing `-rag` stripped. |
| `lora` | optional | The paired LoRA. Auto-applied when the map is used. A bare name works — `.safetensors` is appended if missing. |
| `trigger` | optional | Trigger word; prepended to the prompt if not already present. |
| `image_dir` | optional | Folder holding the images, **relative to the map file**. Default: the map's own folder. |
| `weight` | optional | IP-Adapter strength 0.0-1.5 (default 0.8). |
| `top_k` | optional | How many images to retrieve per generation (default 4). |
| `entries` | **required** | The example images. |

### Entry fields

- `image` — filename (relative to `image_dir`) or an absolute path.
- `keywords` — short tags used to match the user's prompt. Optional: if
  absent, they're derived from the caption.
- `caption` — a full description. Used both for matching and, when
  IP-Adapter isn't installed, appended to the prompt as text guidance.

### Optional: CLIP embeddings

A map may ship an embedding per entry. The app uses them to keep the
retrieved set varied — an image that is a near-duplicate (cosine > 0.97)
of one already picked is passed over in favour of the next-best
different one, so `top_k` references don't come back as the same picture
four times.

```json
"embeddings": {
  "file": "embeddings.safetensors",
  "key": "image_embeddings",
  "clip_model": "laion/CLIP-ViT-H-14-laion2B-s32B-b79K",
  "dim": 1024,
  "normalized": true
}
```

The file must be **safetensors** (never a pickle) holding one float32
`[n, dim]` matrix whose row `i` belongs to `entries[i]`. Vectors need not
arrive normalised — the app normalises on load. A missing, mismatched or
corrupt embeddings file is ignored; the map still works on words alone.

## How retrieval works

The app scores each entry by how many of its `keywords`/`caption` words
appear in your prompt, takes the best `top_k` (thinning near-duplicates
when embeddings are present), and feeds those images to IP-Adapter. If
nothing matches, it uses the first `top_k` entries so the LoRA still gets
representative guidance.

## Packaging

Ship the map and its images together, e.g.:

```
MyCharacter/
  MyCharacter.ragmap.json
  images/
    0001.png
    0002.png
  embeddings.safetensors      (optional)
  MyCharacter.safetensors     (the LoRA — may also sit one folder up)
```

Point the app's **🧭 RAG map…** button at the `.ragmap.json` — or at the
folder holding it, or at an `index.json`.

## Also accepted: a trainer's `index.json`

Retrieval maps written in the earlier `lora-retrieval-map` layout load
unchanged; the app normalises them. In that layout `images_dir` replaces
`image_dir`, the embeddings are named by flat `embeddings_file` /
`embeddings_key` keys, entries carry `{index, image, caption}` with no
keywords, and `lora` is a bare name with no extension.

## Everything is optional but the entries

Each missing piece costs its own feature and nothing else:

| Missing | What happens |
|---|---|
| Images didn't travel with the map | Captions are appended to the prompt as text instead. |
| `lora` not installed here | If the file sits next to the map, the app offers to copy it into `models\loras`; otherwise it generates without it, using the images alone. |
| `keywords` | Derived from the caption (stop-words dropped). |
| `embeddings` | Retrieval falls back to word overlap only. |
| `weight` / `top_k` / `name` | Defaults: 0.8, 4, and the folder/file name. |

## Notes

- Image guidance uses **IP-Adapter**, which is **SDXL-only** — use an SDXL
  model (Juggernaut-XL, DreamShaper) as the base. With a Flux model the
  app skips image guidance.
- If IP-Adapter isn't installed yet, loading a map offers a one-time
  ~1 GB download; without it the map still helps by adding its captions
  to your prompt as text.
