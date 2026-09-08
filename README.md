# AI Image Generator Suite

**v2.0.0** — a fully local, unrestricted AI image studio for your GPU.
Nothing leaves your machine — no accounts, no filters, no cloud.
Compatible with 64-bit **Windows 10 and 11** — **no Python, git, or any
other software required**; everything the app needs lives in its own
folder.

## Quick start

Launch **`ComicArtCreator.exe`** (right next to `Setup.exe`).

The app starts the engine (headless ComfyUI) automatically — the first
launch takes a minute or two while a model loads into VRAM. When the status
line says **"Engine ready."** you're good to go.

1. Pick an **Art Style Preset** (e.g. *Noir / Sin City*).
2. Click **Try example** for a ready-made prompt, or write your own —
   your prompt describes the *scene*, the preset supplies the *style*.
3. Hit **⚡ GENERATE**.

Your prompt, negative prompt, style and all settings are **remembered** —
across generations and app restarts — until you change them or hit
**Clear**. Every image is auto-saved to `output\` with its full recipe
(prompt, model, seed, LoRAs) embedded in the PNG metadata, so any result
can be reproduced. **Save As…** copies the current image anywhere.

On start the app also **checks for newer releases of the installed models
(HuggingFace) and of the ComfyUI engine (GitHub)** and asks (yes/no popup)
before updating anything.

## Border maker

The **Border maker** (very bottom of the left panel) generates themed
4:3/16:9 frames with a transparent screen hole. It mimics the main
controls — model, LoRAs, Variations, Steps, Seed and Editor selections
all come from above; the section itself only sets the border prompt,
aspect, thickness and references, and has its own progress bar. Masked
generation keeps the center empty; prompts are text-free by design. Load
**reference images** and they go through the Image editor instead: the
reference is redrawn as the frame, carrying over its style, characters
and composition.

## The models

Sizes are shown next to each model in the dropdown — **your video card RAM
must be above that number** (keep ~2 GB headroom; the app shows your
detected VRAM under the model box).

| Model | Size | Best for | Speed on a 5090 |
|---|---|---|---|
| **flux1-dev-fp8** | 16.1 GB | Best overall quality + prompt understanding; painted styles, covers | ~16 s |
| **Flux1-Schnell-fp8** | 16.1 GB | Near-dev quality in 4 steps; **Apache 2.0 — outputs fully unrestricted incl. commercial** | ~4 s |
| **Juggernaut-XL-v9** | 6.6 GB | Versatile, great with LoRAs, semi-real styles | ~5 s |
| **DreamShaperXL-Turbo-v2.1** | 6.5 GB | Fast iteration — 8 steps; stylized art | ~2 s |
| **Animagine-XL-4.0** | 6.5 GB | Manga / anime lineage | ~5 s |

Steps, CFG and sampler are chosen automatically per model family — leave
**Steps** on `auto` unless you want to override.

## Style presets (21)

From Golden Age to Moebius to Marvel House Style to Photorealistic. The
preset's style text is shown in an editable box — tweak it freely; it's
appended to your prompt. Each preset auto-picks a suitable model (you can
change it). A negative prompt you typed yourself is never overwritten by
preset switching. Add your own presets in `app\presets.json`.

## LoRAs — starter pack included

LoRAs are small style add-ons that push a base model hard toward a specific
look. Seven come pre-installed (`models\loras`) — the name prefix tells you
which model family they need:

| LoRA | Pair with | Look |
|---|---|---|
| Flux_RetroComic_v2 | Flux | vintage comic print |
| Flux_ArtStyle_XLabs | Flux | painterly art style |
| Flux_Realism_XLabs | Flux | photoreal boost |
| SDXL_GraphicNovel | Juggernaut / DreamShaper | graphic-novel ink & color |
| SDXL_BW_Manga | any SDXL | bold B&W manga |
| SDXL_LineArt_Manga | any SDXL | clean line art |
| SDXL_StyleEnhancer_Anime | Animagine | anime detail boost |

Get more with the **⬇ Get LoRAs (CivitAI)** button — paste a CivitAI model
page URL (free API key from civitai.com → Account Settings → API Keys;
stored encrypted). Or drop `.safetensors` files into `models\loras` and
hit **↻**.

**Trigger words are handled for you**: when you tick a LoRA, its
activation keyword(s) are read from the LoRA's own metadata (or the
sidecar file the CivitAI downloader saves) and added to the prompt
automatically — no need to remember or type them, and nothing is doubled
if you already wrote them yourself.

Two **status badges** top-right (next to the VRAM meter) show at a glance
whether your ticked LoRAs and loaded RAG map will actually affect the next
generation: green = will apply, red = won't (hover them for the reason).

## Image editor — Gemini-style instruction editing

Load one or more images and **your prompt becomes an edit instruction**:
"remove the title text", "make it night", "put her on a beach", "give
him more detailed armor", "combine these two characters into one scene".
The editor changes what you asked and keeps the rest of the image intact.
While images are loaded, the model/preset/LoRA selections are ignored —
the editor engines are separate models:

- **Flux Kontext** — best overall edit quality and instruction following
  (non-commercial license; needs ~16 GB VRAM)
- **Qwen Image Edit** — the best at removing/altering text in images;
  Apache 2.0, outputs unrestricted (needs ~24 GB VRAM)

Editor models download on first use (~12 GB / ~28 GB) after a prompt.
Edited results land in the gallery like everything else, so you can
chain edits: edit → select result → send it back in → edit again.

## RAG maps — your own style, retrieved

Load a **RAG map** (🧭) — a folder of example images paired with a trained
LoRA, as produced by a compatible trainer (see `RAGMAP.md`). At generation
the examples closest to your prompt guide the image via IP-Adapter, and
the paired LoRA + trigger word are applied automatically. Embeddings-only
("private") maps work too — the guidance comes along without the source
images ever leaving the trainer. SDXL models only.

## Reference database & face swap

Point 📇 **Reference DB** at a people database (built with a companion
tool) and pick a 👤 **Person**. In plain generation their photo
face-guides the image (SDXL); when editing, the photo joins the editor as
context.

**🔀 Use RAG & LoRA for image swap** — a checkbox in the image-editor
section — turns Generate into a two-step run: your prompt + preset +
LoRAs + RAG map draw the styled picture first, exactly as usual, and
then the face from your loaded image (🖼 Load…, or the chosen 👤 Person
if nothing is loaded) is applied to the person in it — via **Qwen Image
Edit**, which is part of the base install (needs ~24 GB VRAM to run;
without it Flux Kontext is used). Both pictures land in the gallery:
the styled base and the face-swapped one. While the box is ticked, a loaded image is the *face*, not an edit
target — untick it to go back to normal instruction editing.
**Variations** applies: N variations = N base+swap pairs. The ℹ button
explains exactly what applies in each mode.

## Quality of life

- **✨ Enhance** rewrites your prompt into a richer one using a local
  Ollama model, if you happen to run Ollama — optional, never installed,
  nothing sent online. **↩** undoes it.
- **Upscale 4x** runs finished images through RealESRGAN for print-size
  output.
- **＋Q buttons** queue generations, edits, borders and animations to run
  one after another; the app works through the queue while you keep
  browsing results.
- The app **updates itself**: on start it checks GitHub for a newer
  release, downloads and installs it while you keep working, and asks
  only before restarting (Restart now, or Later — the new version then
  starts next time). A box in the update window turns the automatic
  part off if you would rather be asked first.
- **Hover any control** for a tooltip explaining what it does.

## Animator — sprites & GIFs

Animate any character image (ideally a transparent PNG): describe the
action ("walk cycle", "sword slash", "idle breathing, cape swaying"),
pick duration, frames-to-keep and a loop mode, and generate. You get a
folder of **transparent PNG frames**, a **looping GIF**, and optionally a
**zip** of the whole animation — ready for old-school sprite work.
Loop modes: **Ping-pong** plays forward-then-backward for a mathematically
perfect loop; **Crossfade** blends the ends for organic cycles. One
generation per action builds a full sprite set (idle / walk / attack…).
Besides frames and GIF you can export a **sprite sheet** (PNG + JSON
atlas) and an **MP4/WebM video**. A preset dropdown offers 25 ready-made
actions. Powered by Wan 2.2 (Apache 2.0); ~18 GB, self-installs on first
use.

## Transparency

Tick **Transparent BG** for a clean transparent PNG of the subject —
perfect for compositing panels, stickers, or layering in an editor. The
first use downloads a small background-removal model (~180 MB).

## Fresh install (new machine / from GitHub)

The models and engine are too big for a Git repo, so a release ships the
app plus **`Setup.exe`** — a graphical installer that makes the folder
fully self-contained: it downloads a private Python runtime (into
`python\`, nothing installed system-wide), the engine, and the full
model pack — base models, LoRAs, the Kontext and Qwen editors, and the
animator (~130 GB), all with a progress log. Requirements on the new
machine: **just 64-bit Windows 10/11, an NVIDIA GPU with a current
driver, and ~170 GB free disk** — no Python, no git, no anything else.
Tick *"Engine only"* in Setup to skip the model downloads.

Existing installs stay current automatically: the app carries its model
list inside the exe, so after any app update the startup check offers
whatever models the new version needs.
(`Setup.exe --cli --skip-models` works for scripted installs.)

Windows SmartScreen will warn about unsigned exes on a machine that
downloaded them: **More info → Run anyway** (or right-click → Properties →
Unblock).

## Security

Reviewed against the OWASP Top 10 — see `SECURITY.md`. Highlights: engine
is loopback-only, CivitAI key is DPAPI-encrypted, safetensors-only policy
(no pickle model formats), sanitized download paths, HTTPS-only.

## Troubleshooting

- **Engine did not come up** → read `engine.log`; the last lines say why.
- **Model missing from dropdown** → wrong folder or still downloading;
  checkpoints go in `models\checkpoints`, then hit **↻**.
- **Out of VRAM** → model size + canvas must fit your card; close other
  GPU-hungry apps.
- The engine stays running after you close the app (faster next launch).
  To stop it: Task Manager → end the `python.exe` using the most GPU
  memory.

## Layout

```
Comic_book__art_creator\
├── ComicArtCreator.exe                   ← run this
├── Setup.exe                             first-time install / model updates
├── app\comic_art_creator.py              the studio app (source)
├── app\setup_installer.py                Setup.exe (source)
├── app\presets.json                      art style presets (editable!)
├── app\models_manifest.json              update-checker model list
├── ComfyUI\                              engine (headless, auto-started)
├── venv\                                 python environment
├── models\checkpoints|loras\             your model library
├── output\                               finished art (auto-saved)
├── releases\vX.Y.Z\                      versioned releases for GitHub
└── SECURITY.md                           OWASP security review
```

## Documentation

| File | What's in it |
|---|---|
| `KNOWLEDGE_BASE.md` | How the app is put together, the engine's lifecycle, build and release steps, the gotchas behind each subsystem, and the dead ends not worth retrying. Read this first if you're changing the code. |
| `RAGMAP.md` | The `.ragmap.json` contract — what a trainer should emit so its maps load here. |
| `TRAINING.md` | Building a captioned dataset from your own generations. |
| `SECURITY.md` | OWASP-mapped security review. |
| `CHANGELOG.md` | What changed in each version. |
