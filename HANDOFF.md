# AI Image Generator Suite — Developer Handoff

A single-file **Tkinter desktop app** that drives a bundled **headless ComfyUI**
engine to generate images fully locally on an NVIDIA GPU. Ships as a portable
Windows app (PyInstaller one-file). This document is the engineer's onboarding +
build/release runbook. For end-user docs see `README.md`; for the full build
history and every gotcha see `KNOWLEDGE_BASE.md`.

- **Repo:** https://github.com/TheSaltTrader/AI-Image-Generator-Suite (PUBLIC)
- **Dev tree:** `C:\users\renoi\claudecode\Comic_book__art_creator`
- **User's live install:** `D:\Sofware\AI Image Generator Suite\` (note the
  misspelled "Sofware"). `app.log` / `engine.log` live in that folder root — read
  them to diagnose the user's errors. (Was `H:\Comic Book creator` before
  2026-09-11.)
- **Current version:** see `APP_VERSION` in `app/comic_art_creator.py` and
  `app/version_app.txt` (keep them in lock-step).

---

## 1. Architecture

```
Tkinter UI (app/comic_art_creator.py, ~10k lines, single file)
      │  REST + websocket on 127.0.0.1
      ▼
Headless ComfyUI engine (ComfyUI/, its own Python in python/ or venv/)
      │  models_manifest.json drives downloads
      ▼
models/  (checkpoints, loras, vae, ipadapter, clip_vision, text_encoders,
          style_models, upscale_models, embeddings, insightface)
```

- **Live engine port 8188**, dev engine conventionally **8189** (`ENGINE_PORT`).
- The app **spawns the engine itself** (`start_engine()`), writing
  `extra_model_paths.yaml` with absolute paths so the folder is portable.
- Generation runs on a worker thread (`Generator.run`) and posts results to a
  Tk queue consumed by `_handle_msg` / `_poll_queue` on the UI thread. **All Tk
  calls stay on the UI thread; heavy work (PIL, PDF, vtracer, engine I/O) runs
  off-thread and hands results back through the queue.**

### Key modules (`app/`)
| File | Role |
|---|---|
| `comic_art_creator.py` | The whole app: UI, generation, tabs, self-update glue, engine control. |
| `decals.py` | Decal restoration/vectorize pipeline (importable + CLI). PIL/numpy + optional pymupdf/vtracer. |
| `variations_db.py` | `VariationsDB`: SQLite + files store for "clones" (saved people). |
| `self_update.py` | GitHub-release self-updater (dynamic `APP_EXE`, dual-name zip, `REFRESH_FILES`). |
| `engine_files.py` | Engine/add-on file management + manifest audit. |
| `applog.py` | `app.log` logging + Tk exception hooks + `📋 Log` button. |
| `setup_installer.py` | Source for `Setup.exe` (first-run installer). |
| `version_app.txt` | PyInstaller version resource (keep synced with `APP_VERSION`). |
| `models_manifest.json` | Model download list (HF `resolve/main/<file>` or URL; dir; local). |
| `presets.json` | Art-style presets. |

---

## 2. Feature map (left tabs)

1. **Image generation** — prompt/negative, model picker (SDXL / Flux / SD3.5 /
   anime families via `model_family()`), LoRAs, RAG image-guidance (IP-Adapter /
   Flux Redux / SD3.5 InstantX), QUALITY (FreeU, Upscale 4×, Anatomy guard),
   Variations count (1–100), pinned GENERATE bar.
   - **FACE SWAP** (insightface/inswapper, the only method — Qwen/Kontext dropped):
     draws the base from the prompt, then swaps the chosen face on top.
   - **CLONING** (its own section, mutually exclusive with Face Swap): replays a
     saved person's exact recipe by default; optional "lock the exact face" adds
     the swap. Clones stored via `variations_db`.
2. **Animation** — SVD-style motion from an image.
3. **Borders** — ornate frame generation + center cut-out.
4. **Edit image** — Qwen Image Edit / Flux Kontext on a loaded image.
5. **Decals** — scan → print-ready (see §3).

Cross-cutting: Incognito toggle, resizable panels (ttk.PanedWindow), window/sash
persistence, batch ETA, "keep model in VRAM" (`--highvram`) pref, self-updater
(asks before download AND before restart).

---

## 3. Decals pipeline (`app/decals.py`)

Turns an imperfect scan (PDF or image) into print-ready art. **Runs in-process**;
`pymupdf` (PDF read + SVG render) and `vtracer` (trace) are **bundled into the
exe** via the spec (`collect_all`). Verify in a frozen build with
`AIImageGeneratorSuite.exe --selftest-decals` → expect
`{'pymupdf': True, 'vtracer': True, 'cleanup': True}`.

- **Input:** `iter_source_images()` — PDF (largest embedded image per page) +
  PNG/JPG/WEBP/BMP/TIFF.
- **Cleanup (faithful, default):** `exact=True` leaves art RGB **byte-identical**;
  only alpha + streak columns change. Steps: `detect_carrier` → `_carrier_alpha`
  (keys the film by its TINT so faint white ink is kept) → `destripe` (surgical
  vertical-line removal, thin runs only) → `clean_matte` (drop halo + speckle,
  alpha-only) → optional `solidify_black` (patchy grey→#000) / `smooth_flats`
  (edge-preserving de-mottle).
- **Vectorize (trace):** `vectorize()` — median+quantize → vtracer → strip
  magenta/halo paths → pymupdf alpha render → transparent SVG+PNG. Good for a
  single clean logo; whole dense sheets drift in colour (it's a trace, not a
  redraw).
- **Generate → SVG:** `_generate_decal()` sets `_decal_gen`, reuses `_generate`
  (main-tab model/LoRA/RAG) on the Decals prompt box, `_finish_image` vectorizes
  the result. **Generates ORIGINAL art from a text prompt** (no image reference).
- **Scale conversion:** `SCALE_PRESETS` / `scale_factor` (3.75"/1/18 ↔
  Classified/1/12 = 1.5×). **AI upscale:** `_decal_ai_upscale` via engine
  RealESRGAN (colour-preserving).
- Output → gallery + `output/decals/` (transparent PNG, + SVG for vector).

**Honest limits:** white ink ≈ the film colour can't be separated (keep bg, print
on white paper); home printers can't print white; true colour-exact *vector*
needs a manual redraw.

---

## 4. Dev setup

```
# from the dev tree
venv\Scripts\python.exe   # the app/build venv (has numpy, PIL, requests,
                          # pymupdf, vtracer, pyinstaller, websocket-client …)
```
The **engine** has its own Python (`python/` embedded, else `venv/`); engine-only
deps (insightface for face swap, diffusers for SD3.5) install into THAT on first
use, not the app venv. `setup.ps1` provisions a fresh machine (clones ComfyUI,
installs torch cu128 + requirements, downloads the model pack).

### Run the app in dev
Launch the built exe, or run the module with the venv python. **Never launch-test
while the user's app runs** — the single-instance mutex (`Global\…singleton`) is
global across installs, and both share the one engine/GPU. `tasklist` for
`AIImageGeneratorSuite.exe` and check port 8188 first.

### Run tests (use the **venv python** — system python lacks pymupdf/vtracer)
```
venv\Scripts\python.exe app\update_ui_test.py     # MAIN gate — builds the real
                                                  # App (engine/net stubbed), 292 checks
venv\Scripts\python.exe app\swap_test.py          # face-swap two-step (22)
venv\Scripts\python.exe app\variations_test.py    # clones store (18)
# others: self_update_test, ragmap_test, engine_files_test, startup_test, applog_test, …
```
`update_ui_test.py` is the build gate: it constructs the real `App` on a withdrawn
root with the engine, Ollama probe, VRAM poll and settings stubbed, and asserts
the whole UI + pipeline wiring. Add a check here for every new UI feature.

---

## 5. Build

PyInstaller one-file `--windowed`, via `build_app/ComicArtCreator.spec`:
```
venv\Scripts\python.exe -m PyInstaller build_app\ComicArtCreator.spec ^
    --distpath dist_app --workpath build_app --noconfirm
```
- Output: `dist_app\AIImageGeneratorSuite.exe` (~80 MB).
- The spec bundles `models_manifest.json`, `icon.ico`, and `collect_all('av')`,
  `collect_all('pymupdf')`, `collect_all('vtracer')`.
- `version=app/version_app.txt`, `icon=app/icon.ico`, name `AIImageGeneratorSuite`.
- **Verify the build:** `dist_app\AIImageGeneratorSuite.exe --selftest-decals`
  (prints the dep status and exits 0). Confirm the exe version with
  `self_update.exe_version(...)`.

---

## 6. Self-update (important for release compatibility)

- `self_update.APP_EXE` is **dynamic** = the running exe's name; releases ship
  **both** `AIImageGeneratorSuite.exe` and a byte-identical `ComicArtCreator.exe`
  so installs from before the v2.4 rename still find their exe.
- The updater downloads the release **zip asset**, finds `APP_EXE` + `Setup.exe`
  by `rglob`, swaps them in (renames the running one aside), and refreshes
  `REFRESH_FILES` (CHANGELOG, KNOWLEDGE_BASE, RAGMAP, README, SECURITY, TRAINING,
  LICENSE, `app/models_manifest.json`) in place.
- It **asks before downloading AND before restarting** (standing rule); always
  offers "Not now".
- The zip's internal layout == the `release/` folder contents (12 top-level
  entries: both exes, Setup.exe, docs, `app/models_manifest.json`,
  `app/presets.json`).

---

## 7. Release checklist (every change ships a release)

1. Bump **`APP_VERSION`** in `app/comic_art_creator.py` AND the `filevers`/
   `prodvers`/`FileVersion`/`ProductVersion` in `app/version_app.txt` (and
   `VERSION.txt`).
2. Add a **CHANGELOG.md** entry (top) and a **KNOWLEDGE_BASE.md** note.
3. Run the test suites (venv python) — `update_ui_test.py` must be green.
4. Build (§5) and run `--selftest-decals`; confirm the exe version.
5. Assemble `releases/vX.Y.Z/release/` (copy the previous release/ then overwrite
   both exe names from `dist_app`, the refreshed docs, and `app/*.json`); copy
   sources to `releases/vX.Y.Z/source/`.
6. Zip the `release/` contents → `AIImageGeneratorSuite_vX.Y.Z.zip`.
7. `git add` the **source** files (CHANGELOG, KNOWLEDGE_BASE, app/*.py,
   version_app.txt) — **not** `build_app/` (gitignored) and not the exes/zip
   (gitignored). Commit, `git tag vX.Y.Z`, push `main` + the tag.
   - Gotcha: `git add` of a gitignored path **exits non-zero** and aborts an
     `&&` chain — add only tracked paths, then commit.
8. `gh release create vX.Y.Z <zip> --title … --notes-file …` (use `--notes-file`
   on PowerShell 5.1, never inline `--notes`).
9. Update the memory files (`comic-book-art-creator.md` + `MEMORY.md` index).

`releases/`, `dist_app/`, `build_app/`, `*.exe`, `*.zip`, `models/`, `output/`,
`variations/`, `__pycache__/` are **gitignored** — the GitHub **release asset** is
the distribution, git holds source only. Old local `releases/vX/` copies are
reconstructable from GitHub; prune them if the disk fills (don't delete what isn't
on GitHub).

---

## 8. Dependencies

- **App venv / bundled in exe:** numpy, Pillow, requests, websocket-client,
  av, pymupdf, vtracer, pyinstaller.
- **Engine venv (installed on first use, not in the exe):** torch cu128 +
  ComfyUI requirements; `insightface`+`onnxruntime`+inswapper/buffalo_l for face
  swap; `diffusers` for SD3.5 InstantX. Face-swap readiness = `faceswap_ready()`
  (checks inswapper + buffalo_l + a `.ready` marker).
- cairosvg / svglib were dead ends on Windows (need native cairo) — pymupdf
  renders SVG fine.

---

## 9. Gotchas / standing rules

- **Edit source with the Write/Edit tools**, not bash heredocs (they eat
  backslashes; a failed assert mid-script discards prior edits).
- **Never blind-kill the user's processes**; identify by PID/command line, not
  window title. Kill dev engines (8189) after testing to free the shared 5090.
- The **5090 often holds ~24 GB of other AI work** — check `nvidia-smi` before
  blaming the app for a VRAM/slowness issue.
- Models on an HDD load slowly (minutes); recommend SSD. First engine start on an
  HDD can look like "engine stopped while starting" — it recovers;
  `_boot_engine` auto-retries.
- **Content/IP policy:** the app generates the user's own content (incl. adult) —
  don't judge/refuse that. BUT do **not** build or run features that reproduce
  copyrighted/trademarked artwork (e.g. AI-redrawing scanned branded decals).
  Restoring the user's own scan, a general raster→vector trace, and generating
  **original** art from a prompt are all fine; "AI re-create this copyrighted
  image" is not.

---

## 10. State at handoff

- All test suites green (update_ui 292). Frozen build self-test passes.
- Latest release published to GitHub; dev tree clean (only gitignored build
  artifacts untracked). `Stickers/_final/` holds the user's cleaned sample
  output; `Stickers/` holds their source PDFs (user data, not committed).
- Full chronological history + every fix rationale is in `KNOWLEDGE_BASE.md`.
