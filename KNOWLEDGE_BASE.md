# Comic Book Art Creator — knowledge base

Everything learned building this app, written down so it does not have to
be rediscovered. `<project>` below means the folder holding
`ComicArtCreator.exe`.

Companion documents: `RAGMAP.md` (the RAG-map contract), `SECURITY.md`
(threat model), `TRAINING.md` (building a dataset), `CHANGELOG.md` (what
changed when).

---

## 1. Architecture

A Tkinter desktop app drives a **headless ComfyUI** engine over
REST + websocket on `127.0.0.1:8188`. The app spawns the engine itself;
users never see it.

```
<project>/
  ComicArtCreator.exe      the app (PyInstaller onefile)
  Setup.exe                first-run installer (runtime + models)
  app/                     source, presets.json, models_manifest.json, settings.json
  ComfyUI/                 the engine (+ custom_nodes/)
  python/ or venv/         engine runtime — python/ (embedded) wins if present
  models/                  checkpoints, loras, vae, ipadapter, clip_vision,
                           diffusion_models, text_encoders, upscale_models, rembg
  output/                  finished art; output/_raw is engine scratch
  extra_model_paths.yaml   rewritten at every engine start (gitignored)
```

Key invariants:

- **Frozen mode**: `PROJECT` = the exe's own folder; source mode: the
  parent of `app/`. Everything else is derived from `PROJECT`, so the
  folder is portable — move or unzip it anywhere.
- `extra_model_paths.yaml` is **rewritten on every engine start** with
  this machine's absolute paths. Never rely on a checked-in copy.
- `_contained_env()` keeps every download and cache inside the project
  (`U2NET_HOME`, `HF_HOME`, `PIP_NO_CACHE_DIR`). Nothing lands in the
  user profile.
- Zero prerequisites by design: Setup bootstraps an embedded Python. Any
  feature that would need git or a system Python does not belong in the
  app (an in-app LoRA trainer was built and then removed for exactly
  this reason).

---

## 2. Engine lifecycle — the most expensive lessons

**The engine outlives the app unless you kill it.** It keeps whatever
model it last loaded resident: ~7 GB after an SDXL job, up to ~17 GB
after a Flux one. `_on_close` must stop it (v1.13.1). Before that fix an
engine was found still running two days after its session ended.

**Matching the engine process.** It is launched with `cwd=ComfyUI` and a
bare `main.py` argument, so **its command line does not contain the word
"ComfyUI"**. A filter like `*ComfyUI*main.py*` matches nothing — that bug
sat unnoticed for months and silently broke every restart path. Match on
the project path instead, and use `.Contains()` rather than `-like` so a
bracket in the path cannot act as a wildcard:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -and
                 $_.CommandLine.Contains('<project>') -and
                 $_.CommandLine.Contains('main.py') }
```

**ComfyUI runs as a parent + child python pair**; the *child* holds the
socket. A path-based kill gets both. Killing only the parent PID leaks
the child, which keeps the port and the VRAM.

**Ownership** (`engine_owner.json`, written by `_mark_engine_owned`):

- `engine_is_ours()` — true if the owner pid is us **or still alive**.
  Right for *trusting* an engine, wrong for *killing* one.
- `engine_ours_to_stop()` — true only if we started it, or the session
  that did has died (an orphan worth cleaning). This is what shutdown
  uses, so a second open window never loses its engine.

**Port 8188 squatting** is the classic failure. Symptoms: the node or
model you just installed is missing from `/object_info`, `engine.log`
shows `Port 8188 is already in use` and `Could not acquire lock on
database comfyui.db`, and the app appears to work but answers come from
a stale engine. Diagnose with `Get-NetTCPConnection -LocalPort 8188` and
read the owning process's `CommandLine` — never trust the API alone.

**VRAM readings**: trust `nvidia-smi`, not ComfyUI's `/system_stats`.
They disagree wildly (30.2 GB "free" while nvidia-smi showed 20.5/32.6 GB
used at 98% utilisation).

**Never run engine tests while another GPU job is running.** On Windows
an NVIDIA card does not OOM when it runs out — the driver silently spills
to system RAM and everything gets 10-20x slower while looking healthy.
Aim to stay under ~85% VRAM.

**Boot heal**: if the engine cannot see checkpoints that exist on disk,
it was started with the wrong paths — kill and restart it once
(`_engine_heal_tried` guards against a loop). In a healthy install the
disk and engine lists match exactly and this never fires.

**THE NESTING BUG (v1.34.0 — a user's install had `custom_nodes` 17
levels deep).** `update_engine` preserved `user/input/custom_nodes` by
`shutil.move(keep/sub, ENGINE_DIR/sub)` — but the fresh ComfyUI zip
ships its own `custom_nodes/` and `input/`, and **moving a directory
onto an existing directory puts the source INSIDE it**. So every engine
update buried the previous folder one level deeper until the engine saw
none of the user's nodes; the only symptom was a mid-generation `Node
'IPAdapterUnifiedLoader' not found`. Fixes: `restore_preserved()` merges
item-by-item (the fresh engine's own copy wins on a name clash), and
`flatten_nested_dir(parent, name, dedupe)` repairs existing installs at
every boot (`_repair_engine_dirs` before the engine starts) — shallowest
level first because that is the most recently preserved copy, `dedupe`
only for custom_nodes (a same-named nested dir there is an older copy of
the same package; for input/user a name clash may be different data, so
leftovers are kept). **Every filesystem probe must go through `_long()`
(`\\?\` prefix): a plain `Path.is_dir()` silently returns False past
MAX_PATH, which made the first version of the walk stop before the
buried levels — and building the test fixture hit the same wall.**
`_autoheal_addons()` runs after `engine_ready` and self-installs the
IP-Adapter node if it is still missing (with a re-check after 3s, since
nodes register during startup).

**Custom nodes need an engine restart, and the manifest cannot install
them.** `models_manifest.json` delivers *files* only; anything requiring
a node in `custom_nodes/` needs an app-side installer (see
`_install_style_support` for the pattern: download zip → extract →
move → download models → `kill_engine()` → reboot engine).

---

**THE IN-PLACE DELETE (v1.36.0 — a real install lost its engine AND its
custom nodes).** `update_engine` moved `user/input/custom_nodes` into
`_upd_tmp/keep`, ran `shutil.rmtree(ENGINE_DIR)`, then moved the fresh
tree in. Python 3.12's rmtree walks `os.walk(topdown=False)` and the
first error stops it, so on 2026-08-21 the user's engine was left as
**empty package folders behind an intact main.py** (`.ci` … `comfy_extras`
emptied, `middleware` onward untouched, every top-level file still
there — that pattern is the fingerprint). The function's `finally:
rmtree(_upd_tmp)` then deleted the preserved folders. Five `.pyc` files
that would not delete stayed in `_upd_tmp\keep\…\__pycache__`, and from
then on EVERY user of that shared folder — the engine updater and
`_install_style_support` alike — died on `tmp.mkdir()` with `WinError
183 Cannot create a file when that file already exists`, which is what
the user finally reported ("IP-Adapter install failed") three weeks later.
Meanwhile engine.log said `ModuleNotFoundError: No module named
'comfy.options'` and the app only offered "see engine.log".

All engine file management now lives in **`app\engine_files.py`** (imports
nothing from the monolith; `engine_files.configure(...)` once at import,
like `self_update`). Its rules, each of which the census in
`app\engine_files_test.py` enforces:

- **Never delete the live engine in place.** Download → unpack →
  integrity-check the staged tree → `pip -r` its requirements, ALL in a
  temp folder; only then `os.rename(ComfyUI, ComfyUI_old_<stamp>)` (a
  rename happens whole or not at all — a locked folder means nothing
  changed, and the error says so), rename the staged tree in, COPY the
  user's folders across (`copy_preserved`, fresh engine's own copy wins
  on a name clash). A failure after the rename rolls the old tree back.
  The old tree is removed last; leftovers are reported and swept next
  launch (`sweep_leftovers` also clears the legacy `_upd_tmp` names).
- **User data never lives under a temp folder** that a `finally` cleans.
- **Temp folders are unique per run** (`make_tmp(tag)` →
  `_tmp_<tag>_<pid>_<n>`, `exist_ok`, bumps the name past a stuck
  leftover). `remove_tree()` deletes what it can, retries read-only
  files, and RETURNS what would not go instead of raising or hiding.
- **`check_integrity()`** (ten sentinel files that have existed in ComfyUI
  for years) runs before every engine start (`_repair_engine_files`) and
  `log_shows_damage()` re-checks from engine.log after a failed start;
  either triggers `repair_engine()` = the same swap from the RECORDED sha
  (`engine_version.json`, so the installed packages match; pip
  non-strict). Once per session.
- `install_node_zip()` is the add-on installer; the App holds
  `_addon_lock` so `_autoheal_addons` and the RAG-map/Reference-DB offer
  cannot run `_install_style_support` twice at once, and the module's own
  lock makes the final move safe even if they did.
- `_download_updates` now restarts the engine whether or not the update
  succeeded — it used to stay stopped until the next launch.

Diagnosing a broken install: `Get-ChildItem ComfyUI -Recurse -File |
Group-Object DirectoryName` — empty package folders + intact top-level
files = the in-place delete; `comfy\options.py` missing is the quickest
tell. NTFS directory mtimes date the event (they update on entry
add/remove).

---

**Manifest staleness (v1.25.0 — why installs missed Qwen)**: the app
self-updater swaps ONLY the exe; `app\models_manifest.json` on disk
stayed at whatever version was first unzipped, so `check_model_updates`
(which reads that file) never saw models added later — a user's install
never offered Qwen although the manifest in the repo had carried it for
weeks. Fix: the exe bundles the manifest it was built with
(`--add-data "app\models_manifest.json;."`) and `sync_bundled_manifest()`
writes it over a differing disk copy at startup (frozen only; validates
JSON before replacing; the disk copy remains the single source Setup.exe
reads). Rule: any data file the app READS at runtime and the release
ships must either be embedded or explicitly refreshed by the updater —
exe-only self-updates silently fork it. (`presets.json` is deliberately
NOT synced — users edit it.)

---

**The self-updater lives in `app\self_update.py` (v1.35.0).** It owns the
whole path: check → window → download → verify → swap → refresh → relaunch.
It imports nothing from `comic_art_creator`, so there is no cycle; the app
calls `self_update.configure(PROJECT, APP_DIR)` once at import and hands it
callbacks. Two entry points: the startup background check
(`_check_updates_bg`) and the **Check for updates** button
(`_check_updates_now`, which passes `include_skipped=True` so a skipped
release stays reachable). The window is `UpdateWindow`: release notes,
size, a progress bar, and — in manual mode — Update now / Skip this
version / Continue, nothing downloading until the user presses Update now.

**Automatic mode (v1.36.0, the default).** `auto_update()` lives in
`update_state.json` (absent = on). The startup path calls
`self_update.startup_check()` (ignores a remembered skip when automatic —
automatic means the latest) and opens `UpdateWindow(..., auto=True)`,
which calls `_start()` itself, takes NO grab (the main window stays
usable while it downloads), hides Skip, and on success does NOT relaunch:
the primary button becomes **Restart now** (`_restart` → `on_relaunch`),
the other is **Later** (close; the swapped exe is already on disk, so the
new version starts next launch — `_old_<pid>.exe` is swept then). A
failure turns the primary into **Retry**. The window's checkbox
(`set_auto_update`) switches modes; the Check for updates button always
opens the manual window. The user asked for exactly this split:
"autoupdate when the software first loads … restarts on user approval".
`_make_tmp()` gives the download a unique `_app_upd_tmp_<pid>_<n>` folder
(exist_ok) — the shared-folder mkdir failure described under the engine
section applied here too.

**v1.47.0 — stage, then ask.** User rule: "always give an option to skip
the upgrade for this time". `apply_update` is now `install_staged(
stage_update(...))`: `stage_update` downloads + verifies into a `Staged`
(tmp, new_app, new_setup; `.discard()`), touching nothing else;
`install_staged` swaps + refreshes + removes the tmp. The automatic window
stages on open and then shows **Install and restart / Skip this version /
Not now** (`_ready`); Install runs `install_staged` off the UI thread
(`_restart` → `_installed` → `on_relaunch`); Not now (`_continue`)
discards the staged download and says "asked again next time"; the
window's X is Not now. The manual window's third button is Not now too.
Tests stub `su.stage_update` / `su.install_staged` (not `apply_update`).

**v1.51.0 — ask BEFORE downloading.** User rule ("small bug: wait for
confirmation before downloading"): the automatic window no longer calls
`_start()` on open. It opens with the notes and **Download and install /
Skip this version / Not now**; `_start` stages, `_ready` installs at once
(the yes covered it), `_installed` then offers **Restart now / Later**
(`_relaunch` → `on_relaunch`; Later = the swapped exe starts next launch).
So "automatic" now means only that the window appears by itself. Two
questions per update (download, restart), never a byte without the first.

Things it does that are easy to get wrong, and why:

- **`version_tuple()` pads to exactly 4 parts.** A git tag reads as three
  numbers (`1.34.0`) but an exe's version resource always yields four
  (`1.34.0.0`), and in Python a shorter tuple sorts BELOW a longer one
  sharing its prefix — so `(1,34,0,0) > (1,34,0)` and an exe of exactly
  the running version sailed straight through the "is it actually newer"
  gate. The test caught this; reading the code did not.
- **Nothing is swapped until the download is verified.** `exe_version()`
  reads the version resource through `version.dll` via ctypes (no
  pywin32 — it is not in the frozen bundle) and the swap is refused
  unless the file is a real PE reporting a NEWER version. That catches a
  truncated download and a mis-tagged release before the user is left
  with an app that will not start.
- **The swap rolls back.** A running exe cannot be overwritten on
  Windows, so it is renamed `*_old_<pid>.exe` and the new one copied in;
  if a later copy fails, everything moved is put back. Those aside copies
  cannot be deleted while that session runs, so `sweep_old_exes()` clears
  them at the NEXT launch rather than letting them pile up forever.
- **`_refresh_data_files()` applies the staleness rule above** — the docs
  and `app\models_manifest.json` are copied out of the same release zip
  (JSON is parsed before it can replace a working file). `presets.json`
  and `settings.json` are never touched.
- Zip entries are path-checked before extraction, and the download loop
  honours a cancel event so Cancel actually stops it.

Tests, all of which must pass before a release:
`app\self_update_test.py` (111 checks — versions, the resource read, skip
memory, the sweep, the zip guard, the verify gate, roll-back, data
refresh, every `check()` path, notes rendering, the window built for
real on a withdrawn root, and automatic mode end to end with
`apply_update` stubbed), `app\update_ui_test.py` (48 checks — the flow as
wired into the real App window, engine stubbed out, automatic and manual
startup paths, the RAG map parsed off the UI thread, the relaunch and
close hand-overs), `app\startup_test.py` (12 checks — the relaunched
copy waits for the copy that started it; a real child process holds the
real mutex), `app\applog_test.py` (log lines, tracebacks, thread and Tk
hooks, message boxes, rotation), `app\ragmap_test.py` (15 checks — the retrieval index gives
the walk's exact answers on a 60k-entry map, fast), `app\engine_files_test.py` (65 checks, 11 enumerated
subjects — the engine swap against throw-away folders and a local HTTP
server), and `app\rag_lora_e2e_test.py` (32 checks on a LIVE engine —
LoRA / RAG / embeds / combined / Flux renders fetched back and compared;
`set CAC_ENGINE_PORT=8189` to aim it at an engine on another port so the
user's app on 8188 is never touched). The release zip layout the first
two assume is the real one: exes and docs flat at the root, plus
`app/models_manifest.json` and `app/presets.json`. **The worker thread's
`after()` calls need a running `mainloop()`** — the tests `pump()` the real
loop until a condition holds; an `update()` polling loop makes every
cross-thread `after()` raise "main thread is not in main loop" (§7 said
so already; v1.36's first draft found out again).

## 3. Building and releasing

No spec file is checked in. These commands are the source of truth; both
paths **must be absolute** because `--version-file` and `--icon` resolve
relative to `--specpath` (getting this wrong fails at the EXE step, after
several minutes of work):

```powershell
# app -> 58 MB
venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed `
  --name ComicArtCreator --icon "<abs>\app\icon.ico" `
  --version-file "<abs>\app\version_app.txt" --collect-all av `
  --add-data "<abs>\app\models_manifest.json;." `
  --distpath dist_app --workpath build_app --specpath build_app `
  "<abs>\app\comic_art_creator.py"

# setup -> 15 MB
venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed `
  --name Setup --icon "<abs>\app\icon.ico" `
  --version-file "<abs>\app\version_setup.txt" `
  --distpath dist_setup --workpath build_setup --specpath build_setup `
  "<abs>\app\setup_installer.py"
```

- `--collect-all av` is required for video export (PyAV's avcodec/avformat
  DLLs must land in the frozen bundle).
- rembg is deliberately **not** bundled — it runs via the engine runtime
  as a subprocess, which keeps the exe small.

Release procedure:

1. Bump `APP_VERSION` in `app/comic_art_creator.py` **and** the two
   `app/version_*.txt` resources.
2. Add a `CHANGELOG.md` entry written for users, not for developers.
3. Build both exes, copy them to `<project>`, launch-test the exe (it
   must still be alive after ~10 s, and `VersionInfo.FileVersion` must
   read the new number).
4. Assemble `releases/vX.Y.Z/release/` = both exes + the docs +
   `app/{presets,models_manifest}.json`; `source/` = `app/*.py` + icon;
   zip the release folder (~72 MB).
5. `git commit -F <file>` — **never** `-m` with embedded quotes; Windows
   PowerShell 5.1 splits the argument and mangles the message.
6. `git push origin main` + push the tag, then
   `gh release create vX.Y.Z <zip> --title "..." --notes-file <file>`
   (again, `--notes-file`, not inline quotes).

Editing gotcha: **never** edit `.py` or version files with PowerShell
`-replace`/`Set-Content` — it mojibakes em-dashes and eats quotes. Use a
real editor or a Python script file.

---

## 4. Model families and graphs

`FAMILY_DEFAULTS` picks sampler settings from the checkpoint name:

| family | detection | settings |
|---|---|---|
| flux | "flux" in name | euler/simple, cfg 1, `FluxGuidance` 3.5, `EmptySD3LatentImage` |
| schnell | "schnell" | as flux, 4 steps (Apache-licensed, unrestricted output) |
| turbo | "turbo"/"lightning" | 8 steps, cfg 2 — ignores negatives |
| anime | animagine/illustrious/noob/pony | euler_ancestral |
| sdxl | everything else | standard |

`build_graph(p)` node numbering, in build order: `1` checkpoint → `20+`
LoRA chain → `2`/`3` text encode → `4` FluxGuidance → `31+/41+/50/51`
IP-Adapter → `5` latent (or `10`-`14` for img2img / masked border) → `6`
KSampler → `7` VAEDecode → `40/41` optional upscale → `8` SaveImage.
Editing prompts (`edit_image_names`) take a completely separate path
(`build_kontext_graph` / `build_qwen_edit_graph`).

Editors:

- **Flux Kontext** (`flux1-dev-kontext_fp8`, 11 GB) uses the ordinary
  `flux1-dev-fp8` checkpoint as a **CLIP and VAE donor**, which saves
  downloading T5 and the autoencoder separately. `ReferenceLatent` +
  `FluxGuidance` 2.5, cfg 1. Multi-image two ways (`build_kontext_graph`):
  the default **stitches** the images side-by-side into one context image
  (right for "combine these" edits), while `ref_mode="chain"` gives each
  image its own `FluxKontextImageScale` → `VAEEncode` → `ReferenceLatent`
  chained on the conditioning, so the model sees N distinct context
  images. **Lesson (v1.22.0, found by a live user run)**: cross-image
  instructions like "replace the person in image 1 with the person from
  image 2" DO NOT work on a stitched reference — the model redraws the
  stitched canvas (or ignores the instruction) instead of transferring;
  the chained mode transfers correctly (live-validated A/B at the same
  seed). Stitch = compose, chain = refer.
- **Qwen Image Edit** needs `ImageScaleToTotalPixels` with
  `resolution_steps: 1` on ComfyUI 0.30+, and `CLIPLoader type
  qwen_image`.
- "Output at canvas size" swaps the reference latent for an
  `EmptySD3LatentImage` at the requested dimensions — this is what makes
  restaging into a different aspect ratio work.

**IP-Adapter is SDXL-only** and needs exact filenames
(`ip-adapter-plus_sdxl_vit-h.safetensors` +
`CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors`) for the
`IPAdapterUnifiedLoader` preset "PLUS (high strength)". Flux Redux is
gated and not used.

**Two ways references reach IP-Adapter (v1.16.0):**

- *Images* (default RAG map): retrieved image files are uploaded to the
  engine's `input/` via `/upload/image`, then `LoadImage` → `ImageBatch`
  → `IPAdapter` (node `51`), which runs clip_vision internally.
- *Precomputed embeds* (embeddings-only map): the map ships no viewable
  pictures — each entry is a `.ipadpt` file (a `torch.save`d tensor = the
  CLIP vision **penultimate hidden states**, `[1,257,1280]` fp16, the
  exact "PLUS" image embed IP-Adapter conditions on). The app copies the
  retrieved `.ipadpt` files into the engine's `input/` (a plain file copy
  — the main process has no torch to build them; they arrive ready-made
  from Laura-Trainer's builder, which does) and the graph wires the STOCK
  nodes `IPAdapterUnifiedLoader` (PLUS) → one `IPAdapterLoadEmbeds`
  (`torch.load`) per file → `IPAdapterCombineEmbeds` (`concat`, ≤5) →
  `IPAdapterEmbeds` (nodes `50`/`52…`/`58`/`59`). Same PLUS adapter as the
  image path, so the guidance is equivalent — the source pictures simply
  never existed as files here. `IPAdapterEmbeds` needs either a
  `clip_vision` or a `neg_embed`; the UnifiedLoader bundles clip_vision, so
  passing only `pos_embed` is valid. The map itself is one layout or the
  other, selected by `ragmap["_embeds_only"]`.

**Chaining both sources (v1.19.0):** images and embeds can now steer the same
generation — used when an embeddings-only ("private") RAG map and a Reference
DB person are both active. A **single** `IPAdapterUnifiedLoader` (node `50`)
feeds both stages: `IPAdapterEmbeds` (`59`) applies `model[50,0]`→`model`, then
the basic `IPAdapter` (`51`) takes that `model[59,0]`, reusing the same adapter
output `[50,1]`. One loader + one clip_vision drive both, so there is no
node-`50` collision (two loaders would cross-reference and cycle). `build_graph`
runs the embeds block first, then the image block, off whichever `model_ref` the
prior block produced; either source alone builds the exact graph it did before.
Validated on a live engine (real SDXL render, embeds + person). SDXL only.
  **GOTCHA (v1.16.1, caught only by a live engine run):** `IPAdapterEmbeds`
  is an ADVANCED node — its `weight_type` list is `WEIGHT_TYPES` (`linear`,
  `ease in/out`, `style transfer`, …) and does NOT contain `"standard"`, which
  is only valid on the basic `IPAdapter` node the image path uses. Passing
  `"standard"` makes the engine reject the whole graph with HTTP 400
  `value_not_in_list` and no picture is produced — use `"linear"` (the
  advanced-node equivalent of standard uniform weighting). Static/offline
  checks pass this; only a real POST to a running engine catches the enum
  mismatch, so validate any new node-input value against a live engine, not
  just the node's Python source.

---

## 5. Subsystems

**Borders** took five iterations; the final recipe is: trained LoRA
(`SDXL_BorderFrames_v1`, trigger `cbacframe`, on Juggernaut-XL) + full
frame generation (no latent noise mask — masking locks the model into a
flat edge band) + a **content-aware centre cut** that flood-fills from
the centre using the frame's own colour, giving an organic inner
silhouette + a **floating margin** (the frame is downscaled onto a
transparent canvas, ~6%) + an automatic **second Kontext pass** that
empties the centre when the model drew a character there. Reference
bezels measured 100% transparent outer margin and 0.52-0.77 "rectness",
which is what those two post-processing steps reproduce.

**Animator** — Wan 2.2 ti2v 5B (24 fps native, 49 frames in ~30 s on a
5090). Findings that cost real time:

- Black backgrounds and hard cutouts freeze or mush *any* video model.
  Always pre-composite onto a neutral grey (200,200,205) before
  animating; the app does this automatically.
- First-last-frame conditioning (Wan 2.1 FLF) cannot do locomotion — a
  prepped walk scored 0.95 motion versus 22 for in-place actions. It is
  excellent for capes, hair, idle loops.
- Seamless loops come from generating freely and then cutting the best
  cycle: pairwise frame diffs on 64px greyscale, requiring the segment's
  internal motion to be ≥60% of the clip average, otherwise the "best
  loop" is just the quietest stretch of a dead clip.
- Measure motion on **cut** frames; raw frames include background
  shimmer.
- GIF transparency needs palette index 255 plus disposal 2.

**Transparency (cutout)** — rembg `isnet-general-use`. Defringing must
measure the background colour from the **raw** frame corners *before*
cutting; sampling a cut frame reads rembg's zeroed-black transparent
pixels and makes the outline worse. Erode 1px, feather 0.5, alpha floor
30.

**RAG maps** — see `RAGMAP.md`. Retrieval is literal word overlap
against keywords + caption, so keywords must use the words a person
would actually type. Optional CLIP embeddings (`embeddings.safetensors`,
pooled + L2-normalised) are used only to drop near-duplicate references
(cosine > 0.97) — NOT for conditioning. Every field is optional except
`entries`, and each missing piece costs only its own feature.

`load_ragmap` reads two conditioning layouts, distinguished by
`_embeds_only`:

- *with-images*: entries carry `image`; `_path` resolves to a real file
  (a bare folder must NOT count — check `is_file()`, or retrieval tries to
  feed a directory).
- *embeddings-only* (`mode: "embeddings-only"`, or embeds present and no
  images): entries carry `embed` (a `.ipadpt` under `embeds_dir`), resolved
  to `_ipadpt`; `_path` stays empty. `ragmap_retrieve` treats an entry as a
  usable reference when it has EITHER `_path` OR `_ipadpt`, so retrieval,
  dedup, LoRA auto-apply and trigger injection are identical for both
  layouts. Generation then branches on `_embeds_only` (see §4). The dedup
  `embeddings.safetensors` still ships in embeddings-only maps, so
  near-duplicate skipping keeps working.

This is the privacy path: it lets a map guide generation from real
training references without ever shipping an openable copy of those
images. It pairs with Laura-Trainer's "Private references" build option.

**Prompt enhancer** — optional local Ollama. It must degrade to nothing
when Ollama is absent: `ollama_models()` returns `[]` on any failure and
the button explains itself once. Clean the reply: strip `<think>` blocks
(reasoning models), "Sure, here's…" lead-ins, quotes and bullets, and
reject anything shorter than half the original as a non-answer.

**Reference database** (v1.18.0, lives in the Image editor section) — a
portable SQLite database of people the **user builds** with the separate
*Actor DB Builder* tool (nothing ships with the app); schema
`actor(imdb_id, first_name, last_name, birth_date, death_date, sex,
headshot BLOB, …)` + `meta(format='cbac-actordb-1')`. The app opens it
**read-only** (`file:…?mode=ro`), never holds a connection (open → query
→ close per call), and validates the `meta.format` prefix. The picker
dialog is sortable on every column and searchable. The chosen person's
photo BLOB is written to a temp JPEG at generation time and routed
**context-aware**:

**How many photos each path sends (v1.33.0)**: `➡ To editor` loads ALL
of the person's photos (`_actor_ref_paths_all()`) up to the editor cap —
4 Kontext / 3 Qwen — skipping duplicates and reporting what was left
out; the 🔀 swap takes `SWAP_MAX_FACES` (2, bounded by Qwen's 3-image
encoder with the base occupying one slot); plain-generation IP-Adapter
guidance and the auto-appended person during an edit still use the
single primary photo (`_actor_ref_path()`). **The browser's ◀ ▶ arrows
choose that photo** (v1.33.0): `choose()` commits with `photo_i=pv["i"]`,
`_set_actor(row, photo_i)` stores `actor_photo_i` (persisted as ui-key
`actor_photo`), and `_actor_ref_paths_all()` ROTATES the blob list so the
chosen picture is index 0 — every single-photo caller therefore picks it
up for free, and multi-photo callers keep the rest in wrap-around order.
Temp filenames keep the ORIGINAL index (`…_<i>.jpg`) so rotation never
clobbers a cached file. `flip()` also calls `_set_actor_photo()` live
when the flipped person is the committed one, `select_iid()` reopens on
`actor_photo_i`, and `_refresh_actor_view()` repaints the 48px thumb +
the "· photo n/m" suffix.

- *editing* (`editing == bool(ref_paths)`): appended to `ref_images`
  exactly like a hand-loaded reference, cap-aware (Kontext stitches ≤4,
  Qwen `image1-3` ≤3 — over the cap the photo is left out with a status
  note). `➡ To editor` pushes the temp JPEG into `self.ref_paths`
  directly for person-only edits.
- *plain generation*: appended to `rag_ref_paths`, riding the LoadImage →
  basic IPAdapter path (§4); SDXL-only like every image ref. If an
  embeddings-only RAG map is active too, both now guide the run — the
  person's photo and the map's embeds **chain on one IP-Adapter** (§4,
  v1.19.0) instead of the person replacing the map.

**Image swap — the 🔀 "Use RAG & LoRA for image swap" checkbox**
(v1.24.0; evolved from the v1.19–v1.23 "Swap into selected" button, which
is REMOVED along with its direct-swap path and `_ask_swap_source` modal).
Swapping is now a *mode on plain generation*, not a separate action: with
the box ticked, `_generate` resolves the face via `_swap_face_source()`
(the loaded editor image wins, else the chosen Reference DB person;
neither → a status note and the run proceeds as a normal generation) and
sets `swap_face=<path>`. The run then has two steps: (1) the styled base
generates exactly as usual — presets, LoRAs, RAG map, trigger injection
all active (`editing` forced off; the approximate IP-Adapter face guide
is skipped — identity comes from the crisp Kontext pass); (2)
`Generator._swap_face_pass` applies the face with one **chained** Kontext
pass per variation (§4 lesson: a stitched pair does NOT swap) at
`SWAP_GUIDANCE` 3.0, inheriting each variation's seed, with `out_size` =
the CANVAS dims (a 4x-upscaled base is LANCZOS-downsized first so the
swap lands at canvas size). Both pictures are kept per variation
(Variations = N base+swap pairs); any swap failure is swallowed with a
status note so the base is never lost. While ticked, a loaded image is
the FACE, not an edit target — `_refresh_mode_badges` treats swap-mode
as non-editing so the LoRA/RAG badges stay green; untick to return to
classic instruction editing. The checkbox persists as ui-state key
`swap_rag`.

**Swap engine choice (v1.24.0, decided by live A/B)** — Kontext-dev is a
single-image editor at heart: with two chained references it anchors on
whichever comes FIRST. Measured on two base+face pairs at fixed seed:
base-first preserved the scene but transferred the face 0/2; face-first
transferred 1/2 but collapsed to a portrait redraw (scene lost) on the
other. **Qwen Image Edit is natively multi-image (`image1`/`image2` via
`TextEncodeQwenImageEditPlus`) and went 2/2** — identity landed AND the
scene/pose/style stayed intact, including a cross-gender swap onto a
small distant figure. So `_generate` picks `swap_editor="qwen"` whenever
the Qwen files are installed and `_editor_tier("qwen") != "block"`
(~24 GB VRAM), falling back to the Kontext chain (base-first — keeps the
scene; identity may not always land) otherwise; `_ensure_editor_ready`
runs on whichever was picked. `Generator._swap_face_pass(editor=…)`
builds `build_qwen_edit_graph` with `QWEN_SWAP_PROMPT` ("image 1 / image
2" wording) or the Kontext chain as before. Same out_size/seed plumbing
in both.

**Grid table (v1.29.0)** — ttk's Treeview cannot draw cell gridlines on
Tk 8.6, so the browser's table is a read-only `Text` widget rendering a
monospace box-drawing grid (white lines + white letters on black, bold
header line, `selrow` tag for the highlight). The v1.28 `spec` list
(id/heading/px-width/anchor/getter/numeric) still drives everything —
px widths become character widths (`px // 8`, Consolas 10). Line math:
line 1 top border, 2 header (click → column from char offset), 3 double
rule, data rows at `4 + 2*i` with a rule between each. **v1.30.0 — the
table is fully virtualized**: only the visible window renders
(`rows_per_view()` from `winfo_height` / font linespace; each data row
is two text lines) while the detached scrollbar spans the whole filtered
list (`command=on_scroll` handling moveto/units/pages; `sb.set` fed from
offset/len after every render; wheel bound with "break" so the Text
never scrolls internally; `<Configure>` re-renders; click math adds
`view["off"]`). 47k rows scroll end-to-end with no cap — a render is
only ever ~40 rows, so it is instant. Selection/sort/choose/scroll/
offset are exposed on `self._dbview_api` so tests never have to spelunk
widgets. The old `DB.Treeview` styles remain defined but unused.
**v1.31.0**: a `#` row-number column (absolute position in the filtered+
sorted list; `view["nw"]` width feeds the rule builders and the header
click-x math). Whole-rows-only fit: `rows_per_view` has no partial `+1`,
and because font metrics lie under DPI scaling, render() schedules an
after_idle check of `table.yview()[1]` — if the block overflows, whole
rows are dropped (`view["adj"]`) and it redraws; `<Configure>` resets
adj. NB `yview()` reads STALE geometry when called inside render — the
after_idle deferral is load-bearing. The visible block always ends with
the closed `└┴┘` border: a dangling `├┼┤` continuation rule optically
reads as a cut-off row (cost a debugging round — the "clipped" row in a
screenshot was the rule glyph's descending strokes; diagnose with
`dlineinfo`/`yview`, not eyeballs).
**v1.32.0 — elastic columns**: each column keeps a `ratio` (share of the
table); `fit_widths()` runs at the top of every `render()` and sizes the
columns to exactly fill `winfo_width() // char_px` (line length =
`nw + 4 + sum(w) + 3*len(cols)`), so the grid follows window resizes
and maximise. Dragging a header border calls `resize_col()`, which sets
that width, re-derives every ratio, and refits — the neighbours give way
and the grid stays full-width. Press/motion/release are split so a
plain click still sorts (decided on release); `near_border()` uses
`col_borders()` (`[0, nw+3, …+w+3]`) with ±1 char tolerance, and
`<Motion>` swaps the cursor to `sb_h_double_arrow` over a border.
**Fixed photo box**: the preview label lives inside a 200×240 frame with
`pack_propagate(False)` — without it the label resizes to each photo and
everything below it (arrows, details) jumps around. Verified by
measuring the ▶ button's absolute position across a tall, a wide and a
square photo (identical).

**In-panel Reference DB browser (v1.27.0)** — `_pick_actor` no longer
builds a Toplevel: it grids `self.dbview` into the SAME cell as the
preview canvas (`canvas.grid_remove()` hides, `grid()` restores — grid
options survive removal) and `_close_db_browser()` destroys the frame
and calls `_show_current()`. Style `DB.Treeview` = green (#33ff66) on
black, Consolas, with a Heading variant. The photo pane has ◀ n/m ▶
arrows fed by `_actor_photo_blobs(imdb_id)` (photo sources below).
`_actor_ref_paths_all()` writes every blob to a temp file so multi-photo
people feed the swap with several pictures automatically. Open guards:
`_stop_gif()` first (a playing GIF would keep drawing to the hidden
canvas), and `_close_db_browser(restore=False)` before building so two
browsers can never stack.

**Every DB-builder format reads (v1.28.0)** — `meta(format)` decides the
kind in `_load_actordb`: `cbac-actordb-*` → person (`actor` table),
`cbac-chardb-*` → character (`character` table, `comicvine_id` PK).
Character rows are NORMALISED into the person-dict shape in
`_actordb_rows` (`imdb_id`=str(comicvine_id), `first_name`=name,
extra keys real_name/character_type/first_year/appearances/publisher/
deck ride along) so selection, ui-state restore (`actor_imdb`) and the
swap feed are format-blind. The browser builds its columns from a
per-kind spec (person: first/last/age/sex/born/died; character:
name/real/type/year/apps/pub, default sort = appearances DESC), and the
detail pane surfaces singer-build music columns (genres, voice_type,
years_active, description — detected via `PRAGMA table_info`, so any
subset works). Photos: `_actor_photo_blobs` returns the PRIMARY
headshot/portrait first, then `actor_photo(imdb_id, seq, photo)` /
`character_photo(comicvine_id, seq, photo)` in seq order — the shapes
the enrichment tools actually write — with the older
`photo(imdb_id, image)` shape as a fallback (v1.27 supported only that
one, which no real producer emits). All reference-DB connects carry
`timeout=10`: enrichment writers (journal mode delete) briefly lock the
file at commit, and Windows can transiently refuse the OPEN itself —
the timeout rides out the former; the latter surfaces an error dialog
and a re-open works.

**Detail-preserving + multi-face swap (v1.26.0)** — the swap runs at
FULL denoise (identity needs the freedom: partial denoise was measured
and REJECTED — 0.85 preserved barely more than a full re-render yet
already failed to transfer the face on the stylized test base) and the
result is merged back onto the base client-side by `swap_composite()`:
per-pixel |Δ| → blur → threshold (2.4× mean, floor 16) → **wide
morphological opening** (`MinFilter(k)`/`MaxFilter(k+4)`, k ≈
short-side/32 — kills the thin ribbons caused by slight silhouette
drift, which otherwise ghost as halos) → feathered blend. Only the
genuinely changed region (the head) comes from the swap; everything
else is the base's exact pixels — measured on the photoreal test scene:
base deviation 2.2 vs 19.6 for the raw swap, identity intact. Degenerate
inputs degrade gracefully (uniform change → threshold keeps only the
strongest region; zero change → base returns). The graphs RETAIN a
`swap_denoise` capability (base-latent sampling at partial denoise) for
future use — and the CRITICAL GUARD stands: the graphs never read the
params' ordinary `denoise` (the editor Change-amount slider rides in
every editing params dict and would silently cripple normal edits;
regression-asserted). The swap prompts' ending is style-aware ("if image
1 is photographic, keep the face photographic; if stylized art, that
style") instead of the blanket "not as a photograph" that fought
realistic bases. Faces are a LIST end to end: `_swap_face_source()`
returns all loaded editor images (capped `SWAP_MAX_FACES` 2 — Qwen's
encoder takes 3 images total) else `_actor_ref_paths()` (today one
headshot; a multi-photo Actor DB changes only that function);
`swap_face_prompt(editor, n)` pluralizes the instruction ("images 2 and
3 are photos of the same person — combine them").

**Prompt-token leakage (v1.24.1 lesson — cost a user a bearded man on a
woman's face)**: NEVER name concrete traits in a swap prompt. "Copy
their … beard, glasses …" reads as an example list to a human, but with
weak image grounding the WORDS instantiate — an unconditional list made
Qwen hallucinate glasses, and even a conditional one ("only if the
person actually has them") produced a bearded man from a female
reference. Both swap prompts are now trait-neutral ("the same face and
the same hair, matching every visible feature … adding nothing that
person does not have"), and the regression suite asserts no leakable
nouns ever return. **Swap robustness plumbing (v1.24.1)**: the swap's
`_await_images` timeout is 1800s (a first-time 19–28 GB model load off a
hard disk plus the render can far exceed the 600s default — users saw
"the engine stopped responding"), and `_swap_face_pass` POSTs `/free
{"unload_models": true}` once per run before the first swap so the base
checkpoint isn't squatting VRAM while the swap model loads. If the swap
box is ticked and Qwen is absent but fits, `_generate` makes a ONE-TIME
install offer (declining sets settings key `qwen_swap_declined` and
quietly uses Kontext thereafter).

**LoRA trigger auto-injection** (v1.21.0; `lora_trigger`,
`_safetensors_metadata`) — each ticked LoRA's activation keyword(s) are
appended to the hidden full prompt at generation (never to the user's typed
text), looked up in order from: a `<name>.civitai.info` / `<name>.json`
sidecar next to the file (`trainedWords` / `activation text` / `trigger`,
capped at 4 words), the safetensors header's `__metadata__`
(`modelspec.trigger_phrase`, `ss_trigger_words`), then `ss_output_name` as a
last resort (alphabetic, ≤40 chars, no leading underscore). Results are
cached per filename in `_LORA_TRIGGER_CACHE`; the CivitAI downloader writes
a `trainedWords` sidecar beside every LoRA it fetches and ➕ Add LoRA file…
copies an existing sidecar along — both pop the cache entry so the next use
re-reads. A case-insensitive already-present check stops doubling (the RAG
map prepends its own trigger before injection runs). Skipped entirely while
editing (editors take no LoRAs). The safetensors reader parses only the
8-byte length + JSON header — no torch, bounded at 20 MB.

**Mode badges + tooltips** (v1.20.0) — two `ttk.Label` badges next to the
VRAM meter (`lora_badge`/`rag_badge`, styles `BadgeOn.TLabel` green /
`BadgeOff.TLabel` red) show whether each will actually apply next run.
`_refresh_mode_badges`: LoRA green = ≥1 ticked AND not editing; RAG green =
a map loaded AND not editing AND SDXL family (red on Flux). Repainted by
`_refresh_editor_state` (buttons + badges) from every input change —
ref load/clear, Use-selected, To-editor, person set/clear, model pick, map
pick/clear, LoRA `<<ListboxSelect>>`, and `_refresh_models` (ghost pruning).
Hover help is the module-level `Tooltip` (a borderless `Toplevel` on
`<Enter>` after a delay, one visible at a time; frozen-exe-safe, no deps);
`App._tip(widget, text)` attaches and retains them. The verbose IMAGE EDITOR
header was trimmed to "(optional)" with the detail moved into its tooltip.
The three primary Generate buttons share `Go.TButton` so they're one size.
**v1.21.0** extended tooltip coverage from the editor to the entire left
panel (prompt, negative, model, presets, LoRA controls, RAG map, canvas,
steps/variations/seed, animator) plus the gallery and save/output buttons —
every interactive control now explains itself on hover.

This is **not RAG** — no retrieval; one explicitly chosen picture goes
straight through. Ages are computed at display time, never stored. The ℹ
button (`_refdb_info`) carries the user-facing what-uses-what matrix:
editing bypasses presets/LoRAs/RAG maps, plain generation keeps them and
can combine LoRAs + RAG + a person in one pass.

---

**Theme + size (v1.55.0).** Colours are two palettes in `THEMES`
(dark/light); `apply_theme(name)` rebinds the module globals (BG, BG2,
BG3, FG, FG_DIM, ACCENT, ACCENT2, RULE, plus BTN_ACTIVE/TIP_BG/BADGE_FG/
GO_ACTIVE which `_style` now reads instead of hardcoded hexes). `__init__`
loads `settings["prefs"]` and calls `apply_theme` + sets `tk scaling`
(`_base_scaling` × ui_scale) BEFORE `_style()`/`_build_ui`. The ⚙ Settings
dialog (`_open_settings`) saves prefs; `_apply_prefs_live` rebinds the
palette, re-runs `_style`, walks the tree recolouring Text/Listbox/Canvas
(plain tk widgets don't follow ttk styles), and resets scaling — a
Restart (`_restart_app`, releases the mutex + Popen sys.executable) makes
it exact. TEST GOTCHA: the dev settings.json accumulates a ragmap_path so
the app auto-loads a map at startup and the "bar hidden at rest" checks
trip — clear ui.ragmap_path/face_paths/actordb_path (and prefs) for a
clean run; `check()` now `str()`s its detail so a list detail can't crash
the run.

## 6. Tkinter and threading rules

- **`exportselection=False` on every Combobox, Listbox, Entry and
  Spinbox.** The default ties the displayed value to the X/primary
  selection, so clicking another widget blanks the first one. This was
  reported repeatedly as "my selection disappears".
- Style `TCombobox` for **readonly, focus and disabled** states, not just
  the default one — otherwise the selected value renders in a colour
  that vanishes against a dark field and the box looks empty until
  clicked.
- **Never read Tk variables from a worker thread.** It raises "main
  thread is not in main loop", the thread dies silently and the UI is
  left frozen in its previous state. Snapshot values on the main thread
  and pass plain data in; `root.after(...)` back is fine.
- `_poll_queue` wraps each message in its own try/except and reschedules
  in a `finally`. One exception in a handler used to kill the UI loop.
- **Sweep speed + cancel (v1.52.0).** `_sweep(bar, on, interval=45)` — the sweep interval was 12–15 ms (frantic, read as "racing"); 45 ms is calm. `_await_images` now posts `("progress_mode","steps")` on a CANCEL return, so a sweep started for a model load stops at once rather than only at the batch's `done`. The full RAG+LoRA base → Qwen swap pipeline was validated live on 8189: base-with vs base-without RAG mean-diff 70, swap head-mask 13% at the capped threshold, the face landing on the same body/pose/style; the swap took 136 s on an SSD (the user's models are on an H: HDD — slower). The swap WORKS; the user was cancelling during the multi-minute load.
- **Never call `Progressbar.start()` twice (v1.49.0).** ttk's `start`
  schedules a NEW `ttk::progressbar::Autoincrement` timer chain on every
  call and remembers only the newest id; `stop` cancels only that one.
  Each chain keeps stepping the bar in ANY mode. After a batch of four
  model loads the Generate bar raced for ever ("extremely fast"). Every
  sweep goes through `_sweep(bar, on, interval)`: a `self._sweeping`
  registry guards the start, and the stop also scans `after info` for
  Autoincrement scripts naming the bar and cancels them. `update_ui_test`
  asserts no stray Autoincrement timers after repeated loads on all three
  bars (Generate, RAG map, readiness strip).
- **Never parse a RAG map on the UI thread (v1.37.0).** A user's map is
  925 MB of JSON + a 1.97 GB embeddings file on an HDD; `load_ragmap` on
  the UI thread showed the app as "Not responding" for the whole parse —
  at startup (the restore), again straight after (`_refresh_models` →
  `_validate_ragmap` re-parsed the whole file), and again on every model
  refresh. `_load_ragmap_async(path, on_done)` parses in a thread and
  posts `("ragmap_loaded", gen, path, rag, err, on_done)`; the handler
  runs `on_done` only for the NEWEST generation, so a later pick
  supersedes an earlier parse. `_validate_ragmap` compares the file's
  `(mtime, size)` (`rag["_sig"]`) and only re-parses (async) when the
  file changed. `load_ragmap` precomputes `e["_words"]` (interned) and
  **`_build_rag_index`** (v1.38.0: word → `np.int32` entry indices +
  a has-reference mask); `ragmap_retrieve` scores only the entries
  sharing a word with the prompt and ranks with a stable `argsort`, so
  ties keep original order and excluded entries sink — the per-entry
  walk it replaces took **5.5 s per Generate on the UI thread** on the
  user's 481,177-entry map (0.9 ms vs 48.7 ms on 60k in
  `ragmap_test.py`, answers identical across dozens of prompts). The
  walk stays as the no-numpy fallback. Rule: anything that scales with a
  user's data goes through `ui_queue`, never inline in a handler — and
  never assume a map has four entries.
- **Shutting the engine down is seconds of PowerShell** (`kill_engine`
  enumerates every process). `_on_close` withdraws the window first and
  does it in a thread, then posts `("quit", None)` (the handler destroys
  via `after(0)` so the poll's own reschedule is the last Tk call);
  `_relaunch_after_update` does the same. Both arm a 20 s
  `_force_quit` so a stuck shutdown can never leave a hidden window
  running.
- **NEVER spawn a frozen exe with an inherited environment (v1.41.0).**
  PyInstaller 6.21's onefile bootloader passes `_PYI_APPLICATION_HOME_DIR`,
  `_PYI_ARCHIVE_FILE`, `_PYI_PARENT_PROCESS_LEVEL`, `_PYI_SPLASH_IPC` to
  its child, and the Python child keeps them in `os.environ`. A frozen exe
  started from the app (`_relaunch_after_update`, Setup.exe) inherits them
  and RUNS FROM OUR `_MEIxxxx` FOLDER instead of extracting its own; when
  we exit, our bootloader deletes that folder as far as the other copy's
  open DLLs allow (the "Warning" window on the old pid is that failed
  cleanup), and every LATER import in the new copy fails: `cannot import
  name …`, `[WinError 3] … _MEI416282\numpy\_core` (the RAG index),
  `certifi\cacert.pem` (the update check). Diagnosed from app.log the first
  time it existed. Fix: `_clean_child_env()` (drops `_PYI_*`/`_MEIPASS*`)
  on every Popen of an exe (relaunch, Setup.exe, and the engine/helper env
  for hygiene), plus a guard at the top of the module: a frozen copy whose
  `sys._MEIPASS` folder is >60 s older than the process (`_foreign_
  extraction`) re-execs itself with a clean env (`CBAC_REEXEC=1` stops a
  loop) and `os._exit(0)`s — that is what rescues the copy a pre-v1.41
  relaunch started. `%TEMP%\_MEI*` folders whose `numpy\_core` is missing
  are the fingerprint. **v1.42.0: the age net was not enough** — a v1.41
  auto-update completed 17 s after the old copy started, and the new copy
  died the same way the day it was meant to fix. The certain signal:
  `--after-update` WITHOUT the `--clean` marker a v1.42+ relaunch adds =
  started by an older copy = inherited folder → `_needs_clean_restart()`
  re-execs at once (args + `--clean`). Age stays as a second net. The
  bootloader's "Failed to remove temporary directory" box on the OLD pid is
  that copy failing to delete the folder the new copy still uses.
- **`app.log` (v1.39.0) — `app\applog.py`.** A `--windowed` exe has no
  console, so until v1.39 an exception in a worker thread or a Tk
  callback vanished and the user reported "it said cannot import name"
  from memory, unreproducible. `applog.configure(PROJECT/"app.log")` at
  import, `install_hooks()` (sys + threading excepthooks),
  `tk_report(root)` (report_callback_exception), `wrap_messagebox(
  tkinter.messagebox)` (logs every box as it opens, on the MODULE so every
  import style is covered), a `status_var` write-trace (`_log_status`,
  immediate repeats deduped), `applog.error("shown: …")` in the "error"
  queue handler, and `applog.exception(...)` in every except that shows a
  message (Generator.run, RAG parse, add-on install, engine update/repair).
  Capped at 3 MB / newest 1 MB kept. The 📋 Log button opens it. Rule for
  new code: an except that turns an exception into a message must also
  `applog.exception()` it — the message loses the traceback.
- **The relaunched copy must WAIT for the copy that started it.** Right
  after every update the new exe took the single-instance mutex while
  the old one was still closing and asked "already running — open
  another window?". `previous_instance_pids()` reads the pid out of the
  `*_old_<pid>.exe` the swap leaves (and `--after-update <pid>`, which a
  v1.37+ relaunch passes); `wait_for_previous_instance()` polls tasklist
  until those pids are gone, releases OUR mutex handle (while we hold
  one the name never disappears) and probes again. A genuinely separate
  copy still gets the question. `startup_test.py` covers it with a real
  child process holding the real mutex. **v1.40.0: the wait is
  unconditional** — hiding the window first in `_on_close` (v1.37) made
  "close, then reopen within a few seconds" hit the question too, with no
  `_old_` file to name the closing pid. `main()` now shows a small
  "waiting for the previous copy to finish closing…" window and polls the
  mutex (`wait_for_previous_instance(pids=(), timeout=12, tick=
  root.update)`); with named pids it stops early once they are gone and
  the mutex is still held (a real second copy). The mutex is GLOBAL
  across installs: a dev copy launch-tested on C: blocks the user's H:
  copy — never launch-test while their app runs.
- Settings persist on a 700 ms debounce (`_schedule_persist`) with a
  baseline save at startup, so a force-kill still keeps recent edits.
- **The wheel over the left panel scrolls the panel, full stop (v1.47.0).**
  Bindtags run instance → class → toplevel → all, so a `bind_all` router
  runs AFTER a Combobox has changed its value or a Text has scrolled
  itself, and "break" from "all" cannot undo that. `_arm_panel_wheel()`
  (after `_build_ui`) puts a `PanelWheel` tag FIRST on every widget of the
  three pages; `root.bind_class("PanelWheel", "<MouseWheel>", …)` scrolls
  the page canvas under the pointer (`winfo_containing`) and returns
  "break". Widgets created on a page LATER need the tag too. The old
  `bind_all` router stays for the pointer-over-page, focus-elsewhere case.
- **The readiness strip (v1.47.0).** `self._pending` {key: text} painted
  by `_set_pending(key, text|None)` into `ready_frame` (row 0 of
  `left_wrap`, above the notebook; `grid_remove`d when empty). Worker
  threads post `("pending", key, text)`; keys today: engine (boot →
  engine_ready / failures), ragmap (`_rag_progress` with the percentage →
  `_rag_progress_done`), updates (`_check_updates_bg` wrapper), addons
  (`_install_style_support`). Anything else that makes the user wait
  should post its key too.
- **Dialogs are placed, never left to Windows (v1.44.0).** A `Toplevel`
  with no geometry lands at the screen's top-left; a `messagebox` lands
  wherever. `_place_near(dlg, anchor)` puts a dialog just above an anchor
  widget (below it when there is no room), clamped to the root window;
  `_confirm_near(anchor, title, text, ok_label)` is the Delete/Cancel box
  built on it (Enter/Escape bound, modal via `wait_window`). Build the
  dialog withdrawn, place, then deiconify — otherwise it flashes at the
  corner first. Tests drive a modal box by polling for it with
  `root.after` and `invoke()`-ing its button — `event_generate("<Return>")`
  on an unmapped toplevel hung the suite; keep a watchdog `after` that
  destroys the box.
- **"The selected image is never used" with RAG on = the composite threw
  the swap away (v1.50.0).** `swap_composite` masks where |base−swap|
  ≥ `max(16, 2.4×mean)` after a blur and a wide opening. A photoreal,
  RAG-guided base gets re-rendered all over by Qwen (mean high → bar
  high) while the face change is subtle → empty mask → the BASE came back;
  a stylised base's new face differs strongly → kept. Now `thr =
  min(max(16, 2.4×mean), SWAP_THR_CAP=40)`, and a mask under
  `SWAP_MIN_MASK=1.5%` returns the RAW swap ("used raw"); `stats` (mean,
  thr, mask, used) go to app.log per swap — read them before guessing.
  Also `/free` now passes `free_memory: true` before a swap. `swap_test`
  covers a strong head (composite), a subtle swap on a noisy re-render
  (raw), and a noisy re-render with a real head (cap keeps it). The user
  had deleted every output, so the pairs could not be compared — hence
  the numbers in the log from now on.
- **The swap "hang" was a 163-second Qwen load (v1.48.0).** app.log +
  the engine history proved the swap SUCCEEDED; the user saw a bar parked
  at 100% and read it as hung. Qwen (19 GB fp8 + 8.7 GB text encoder)
  cannot stay resident next to SDXL + IP-Adapter vision + upscaler on the
  32 GB card, so every base→swap alternation evicted it and re-read it
  from the H: HDD (~3 min). Fixes: `_await_images` posts
  `("progress_mode", "loading")` on the first `executing` and an
  elapsed-time status every 10 s until the first `progress`
  (`("progress_mode", "steps")`); the handler sweeps the bar
  (indeterminate) and restores it. `Generator._run` collects
  `pending_swaps` and swaps AFTER the whole batch (one model switch per
  batch); Cancel in the swap phase keeps the bases. Under the swap
  checkbox: `swap_use_rag_var`, `swap_use_lora_var` (the base without the
  map / LoRAs; badges follow), `swap_fast_var` (Kontext, 11 GB, fits
  alongside — no reload; likeness weaker). Separate RAG/LoRA checkmarks
  do NOT remove the reload; only the ordering and the fast option do.
  `swap_test.py` drives `Generator._run` and `_await_images` against a
  fake websocket/requests with a scripted clock.
- **Cycle/drop a person's photos (v1.58.0).** `self._actor_excluded`
  {imdb_id -> set(indices)} restricts which of a multi-photo person's
  pictures are sent — never touches the DB. `_actor_step_photo(delta)`
  cycles `actor_photo_i` across ALL photos (so an excluded one can be
  re-included); `_actor_toggle_photo` adds/removes the shown index
  (refuses to drop the last remaining one); `_actor_ref_paths_all` skips
  excluded indices (falls back to all if somehow all excluded). The
  ◀ ▶ + Drop nav (`photo_nav`) shows only when >1 photo. Persisted as
  `actor_excluded` (sets -> sorted lists), restored BEFORE `_set_actor`
  so the view reflects it. `_refresh_actor_view` shows "photo i/N
  (dropped) · K sent"; the Using list lists the kept photos.
- **Engine (VRAM) not freed on close (v2.3.2).** User: "is the app not clearing
  vram when it closes?" Correct. `_on_close` only killed the engine
  `if engine_ours_to_stop()` — which is False when the owner pid no longer
  matches (after an update RELAUNCHED the app, ownership drifted) or the owner
  file is missing → the ComfyUI engine (last model resident, up to ~17 GB)
  survived every close. FIX: `_on_close` now calls `kill_engine()`
  UNCONDITIONALLY — it's already scoped to this install's own PROJECT path
  (`CommandLine.Contains(PROJECT)`), so it never touches a different install or
  a foreign ComfyUI; the ownership gate was the leak. Since kill_engine kills
  ALL matching python+main.py+PROJECT procs, one launch+close of the fixed app
  also cleans accumulated orphans. update_ui_test asserts kill_engine fires on
  close even when `engine_ours_to_stop` is stubbed False (162). DIAGNOSIS NOTE:
  the ~11 GB the user saw was MOSTLY my leftover DEV engines on the shared 5090
  (dev testing left ComfyUI on 8188/8189 with models loaded); killing the C:
  dev engines dropped used VRAM 12 GB → 2.3 GB. So on this shared GPU, always
  sweep dev engines after testing — they masquerade as the user's app leaking.
- **IP-Adapter validation rejection + install-at-update (v2.3.1).** User: base
  generation failed with `Engine rejected the request: Prompt outputs failed
  validation (nodes: IPAdapterLoadEmbeds …)` — for EVERY Clone method (each
  draws a RAG base first), on an embeds-only map. Root cause is the engine's
  IPAdapter_plus node version drifting from what the map's `.ipadpt` embeds
  expect (the app copies them to `ENGINE_DIR/input` and `IPAdapterLoadEmbeds`
  reads by name; `_style_support_ok()` sees the node present so autoheal
  skipped it). FIX (engine-independent, unblocks regardless of cause): in the
  Generator's 400 handler, if `node_errors` mention IPAdapter AND the params
  carry `style_embed_names`/`style_ref_names`, retry the POST ONCE with those
  stripped (`build_graph(p_fb)`) — the RAG LoRA + trigger in the prompt still
  steer it — and emit `("addon_repair","ipadapter")`. The App handles that by
  reinstalling the pinned IP-Adapter node via `_install_style_support` (once
  per session, `_ipa_repair_started`). INSTALL-AT-UPDATE (user: "should
  install at the update, not when a user first runs it"): `_autoheal_addons`
  now also runs `_install_face_swap()` at boot when `not faceswap_ready()`
  (sequentially after the IP-Adapter install so they don't fight the
  `_addon_lock`), so the face-swap engine downloads once after an update
  instead of on the first swap. swap_test grew a 400-then-200 fake asserting
  the retry-without-IP-Adapter path (22 checks).
- **Quality suite (v2.3.0).** A new QUALITY UI section on the gen tab
  (`hires_var`/`hires_scale_var`/`freeu_var`; the 4x `upscale_var` moved in).
  build_graph additions: **hi-res fix** = `LatentUpscaleBy` (bislerp,
  scale 1.5/2) then a SECOND `KSampler` at denoise ~0.45 and 0.6× steps, its
  output feeding VAEDecode via a `sampler_out` var (skipped when
  `ref_image_name` or `border_assets` — img2img/mask jobs); **FreeU_V2** on
  `model_ref` before the sampler (SDXL family only, not flux/schnell);
  **CLIPSetLastLayer(-2)** for the anime family (v2.2.1). `builtin_enhance`
  now wraps the subject's first clause in `(head:1.15)` for non-Flux families
  (Flux ignores attention weights). Params `hires`/`hires_scale`/`freeu`
  thread through the params dict, persistence and the autosave var list. All
  off by default. Tests: build_graph asserts the nodes appear only in the
  right cases (SDXL vs Flux vs img2img); update_ui 161.
- **Face-swap install loop — a method on the wrong class (v2.2.1).** app.log:
  `Face-swap setup failed: 'App' object has no attribute '_download_to'`,
  repeating every Generate. `_download_to` was defined on the **Generator**
  class (next to `_face_swap_local`/`_swap_face_pass`, which ARE Generator
  methods) but `_install_face_swap` is an **App** method — so the App call
  `self._download_to(...)` raised, the install never wrote `.ready`,
  `faceswap_ready()` stayed False, and the offer re-fired on every Generate.
  The clone engine was uninstallable in the shipped v2.1/v2.2. FIX: made it a
  module function `download_stream(url, dest, progress=None)` both classes can
  call; `_install_face_swap` passes a `ui_queue` progress lambda. Plus a
  `self._faceswap_installing` flag so `_generate` shows "still setting up"
  instead of re-asking mid-install. update_ui_test now stubs subprocess/
  download/_sha256 and asserts `_install_face_swap` reaches `.ready` with no
  error queued. LESSON: when adding a helper, put it on the class that CALLS
  it (or make it module-level) — the recurring "written for one shape" defect.
  Also v2.2.1: anime family gets `CLIPSetLastLayer(-2)` (clip skip 2) in
  build_graph (SDXL/Flux unaffected).
- **Validation + hardening + new icon (v2.2.0).** LODESTONE: the portable kit
  (`kit/lodestone.py` + `templates/`, copied from the CDG Spec Forge kit — the
  LATEST kit is under CDG, not Community Uploader) with a project `kit/
  lodestone.toml` declaring the app's sweeps. Run: `venv/Scripts/python.exe
  kit/lodestone.py`. GOTCHA: the runner does `subprocess.run(cmd, cwd=root)`;
  a FORWARD-SLASH relative exe path (`venv/Scripts/python.exe`) fails
  `WinError 2` on Windows even with cwd set — use a BACKSLASH literal
  (`'venv\Scripts\python.exe'`, TOML single-quoted) or an absolute path.
  Result: 7 PASS (414 checks), the live-engine/GPU/journey/security/fidelity
  sweeps NAMED as debt (never averaged). OWASP re-review (the v1.0.0
  SECURITY.md was stale — self-update/downloads/installs now exist): only A08
  regressed; fixed — `INSWAPPER_SHA256` pin verified before use, `insightface
  ==2.0` pinned, `_safe_extractall` zip-slip guard added to setup_installer's
  3 extract sites (the runtime extractors already had it), and
  `download_model_update` refuses non-HTTPS URLs. ICON: `app/icon.ico`
  regenerated (PIL, 7 sizes 16-256; `save(..., format='ICO', sizes=[...])` on
  a 256 base — `append_images` is IGNORED for ICO, that's why only 16px
  embedded the first time). All three Windows icon mechanisms wired
  (`windows-app-icons` memory): exe icon via the spec, `icon.ico` added to
  spec `datas` + `root.iconbitmap(default=...)` for the window/taskbar, and
  `SetCurrentProcessExplicitAppUserModelID` for grouping/pinning.
- **Clone Tool: a REAL face swap, not an editor re-render (v2.1.0).** THE
  fix for "the clone doesn't resemble the person at all." app.log proved the
  cause: every Qwen/Kontext swap ended `Cancelled — 1 of 4 swapped` within
  seconds (the 28 GB editor loads for minutes off the H: HDD; the user
  cancels every time), and NO completed swap ever logged its
  `face swap (...): mean change…` line — so the user only ever saw the
  faceless base. Fix: a true identity swap via **insightface / inswapper**
  (`inswapper_128.onnx` ~554 MB + the `buffalo_l` detector), run in the
  engine venv like rembg (`run_face_swap`, `_FACESWAP_CODE`). Validated live:
  swapped-face cosine similarity to the SOURCE = 0.88–0.91 (same-person),
  vs the original face ~0.05. GOTCHAS: (1) **insightface 2.0 is a pure-python
  wheel** — no compiler needed, so the in-app `pip install insightface onnx`
  into the engine venv is clean. (2) **`Face.normed_embedding` is a read-only
  property in 2.0** — to average identity over several photos, set
  `srcf.embedding = mean(embeddings)` and let normed_embedding derive; setting
  normed_embedding raises `AttributeError: property has no setter`. (3) request
  `["CUDAExecutionProvider","CPUExecutionProvider"]` but wrap in try/except —
  onnxruntime only WARNS on a missing CUDA provider (falls back to CPU), and
  CPU is fast enough for the 128 model. (4) read images with
  `cv2.imdecode(np.fromfile(p), ...)` for Windows/unicode paths. UI: the
  section is renamed **CLONE TOOL**, with a **Method** dropdown
  (`clone_method_var` / `CLONE_METHODS`, `_clone_method()` → faceswap|qwen|
  kontext, default faceswap). Each method self-installs on first use
  (`_install_face_swap` mirrors `_install_editor`; a `.ready` marker gates
  `faceswap_ready()`). BEHAVIOUR: clone mode now emits ONE finished image —
  the base is held in `pending_swaps`, never shown faceless; the gallery gets
  the cloned result (or the base, with a status, if the swap fails/cancels).
  When SDXL + IP-Adapter are available the base is also IP-guided by the
  chosen face ("with the person in mind") before the exact swap. Tests:
  update_ui 155, swap 20 (rewritten: red base vs blue clone, single-image),
  self_update 113, ragmap 24, engine_files 65.
- **Rename to AI Image Generator Suite (v2.0.0).** The product name changed
  everywhere the USER sees it — window titles (`comic_art_creator.py`),
  `version_app.txt`/`version_setup.txt` (ProductName/FileDescription/
  CompanyName), the updater/setup window labels, README/CHANGELOG/KB
  titles — and the GitHub repo was renamed to `AI-Image-Generator-Suite`.
  DELIBERATELY LEFT UNCHANGED for auto-update safety: the exe filename
  `ComicArtCreator.exe` (self_update `APP_EXE`), the single-instance mutex
  `Global\\ComicBookArtCreator_singleton`, and `InternalName`/
  `OriginalFilename`. WHY: `install_staged` on already-deployed v1.x builds
  looks for `ComicArtCreator.exe` inside the release zip and writes it to
  `_PROJECT/ComicArtCreator.exe`; a renamed exe in the v2.0 zip would make
  every existing user's update fail ("release zip has no ComicArtCreator.exe")
  and could let two differently-named copies run at once. GitHub 301-redirects
  the old repo's `releases/latest` API to the new name, so v1.x installers
  (which still hold the old `RELEASES_API`) reach v2.0; v2.0's constants point
  at the new repo directly. LESSON: rebrand the identity, not the update
  contract — the filename an installed updater already looks for is frozen.
- **Built-in offline prompt enhancer (v2.0.0).** `✨ Enhance` was Ollama-only,
  so with no Ollama the model dropdown was empty and the button dead-ended in
  a "get Ollama" popup. `BUILTIN_ENHANCER = "Built-in (offline)"` is now always
  the first dropdown value and the default; `builtin_enhance(text, style,
  family)` (a pure function, tested) keeps the user's words, folds in the
  style, and appends composition + `_ENHANCE_QUALITY[family]` phrasing,
  skipping anything already present. `_run_enhance` branches built-in vs
  `ollama_enhance`; `_enhance_prompt` never bails for lack of Ollama and passes
  `model_family(self._model_raw())`. Ollama models still appear after the
  built-in and give a smarter rewrite when present.
- **"Using:" is a thumbnail strip, not a Listbox (v2.0.0).** `self.face_using`
  (a `ttk.Frame`) replaced the text `face_list` Listbox. `_refresh_face_list`
  destroys and rebuilds small (56px) thumbnail cells via `_face_thumb_image`
  (opens a path OR raw DB bytes with PIL; refs kept in `self._face_thumb_imgs`
  or Tk garbage-collects them out of the labels). GOTCHA: widgets built after
  startup miss the panel's mouse-wheel tag — `_arm_panel_wheel` only tags the
  tree once at build time. Factored out `_arm_wheel(subtree)` and call it at
  the end of `_refresh_face_list`, or the "every widget carries PanelWheel
  first" rule breaks over the new thumbnails.
- **Engine start-up: detect a crash, don't just wait it out (v2.0.0).**
  `_boot_engine`'s wait was a silent `for _ in range(180): if engine_alive()`
  — six minutes of nothing, whether the engine was slow or already dead. A
  fresh IP-Adapter install + `kill_engine`/restart that fails to come up
  looked identical to a slow first load. `start_engine` now stashes the
  `Popen` in `_ENGINE_PROC` and returns it; the wait checks `proc.poll()` and
  breaks immediately on a crash, posts an elapsed-seconds status every ~10s,
  and both the timeout and crash paths surface `engine_log_tail()` (the last
  engine.log lines) so the reason is visible instead of "see engine.log."
- **A loaded Edit image must not put the whole app in edit mode
  (v1.60.0).** THE ROOT CAUSE behind "LoRA and RAG still not enabling."
  Once editing moved to its own tab (v1.56), `_generate` and
  `_refresh_mode_badges` still decided edit-vs-generate from
  `editing = bool(self.ref_paths)` — a global. So an image left sitting
  on the Edit tab silently forced *every* GENERATE into an edit (no
  LoRA, no RAG, both badges red) even though the user was on the
  Image-generation tab with a LoRA ticked and a RAG map ready. The
  app.log tell was `"Editing with Flux Kontext — preset and LoRAs are
  ignored"` firing on a plain Generate. Fix: `_generate(…, edit=False)`
  takes an explicit flag — only the Edit tab's **Apply edit** button
  passes `edit=True`. Inside `_generate` a local `ref_paths =
  self.ref_paths if editing0 else []` shadows the attribute for the whole
  method (swap gate, style-override, editor path, `edit_refs`), so
  GENERATE ignores the loaded image entirely. `_refresh_mode_badges` now
  hard-codes `editing = False` (GENERATE never edits) and drops the
  `and not bool(self.ref_paths)` clause from `swap_mode`. LESSON (the
  recurring one across these apps): a boolean derived from shared state
  is a landmine once the UI splits that state across two independent
  places — pass intent explicitly instead of re-deriving it.
- **Edit on its own tab + clone toggle (v1.56.0).** A 4th notebook tab
  "Edit image" (`_page_edit`) holds the loaded-image editor with its OWN
  instruction box (`edit_prompt_box`) — `_generate` uses it when
  `ref_paths` is set (v1.60: AND the edit flag), the main prompt otherwise. A "Common edits" Menu
  (`_build_edit_menu`/`_popup_edit_menu`/`EDIT_ACTIONS`, right-click too)
  fills it. The face-swap checkbox (`swap_cb`) is at the TOP of the Clone
  section; `_on_clone_toggle`/`_apply_clone_enabled` grey `clone_body`
  when off. ROW-ORDER GOTCHA: after building the Edit tab with its own
  `r=0`, restore `left=self._page_gen` and `r=self._gen_row` where
  `_gen_row` was captured AFTER the clone section (not before) — else the
  Generate button grids over the clone rows. `_scroll_page` records each
  tab page in `self._page_tabs[str(inner)]`; select a tab with
  `left_tabs.select(self._page_tabs[str(inner)])` (the inner scroll frame
  is NOT a notebook tab — selecting it raises "not managed by notebook").
  `_refresh_mode_badges` now logs each badge's colour + reason to app.log.
- **Minimal face controls (v1.54.0).** Two independent checkboxes 'LoRA'/'RAG' (swap_use_lora_var/swap_use_rag_var) replaced the single combined guide checkbox; the Best/Fast radio is gone (swap_fast_var stays, defaults False=Qwen, no widget). A 'Using:' Listbox (`_refresh_face_list`, wired into `_refresh_editor_state` and `_on_face_source`) lists the face file(s) or the person. 'Output at Canvas size' removed (editor_canvas_var forced True on restore). Fixed a stray `self.change_var = DoubleVar()` after the block that had orphaned the Change-amount slider's variable.
- **Face source is separate from the edit target (v1.53.0).** The "IMAGE EDITOR" block was rebuilt into FACE / CHARACTER + EDIT A LOADED IMAGE. The face is now `self.face_paths` (file source) or `actor_sel` (db source), chosen by `self.face_source_var` (file/db); `_on_face_source` grid_remove()s the unused rows. The swap runs only when NOT editing (`... and not self.ref_paths`), so a face-file and an edit image no longer share `ref_paths`. `_swap_face_source` reads the source; `_pick_face`/`_clear_face`/`_set_face_label` manage the file; one guide checkbox (`_on_swap_guide`) drives both `swap_use_rag_var` and `swap_use_lora_var`; Best/Fast is a radio on `swap_fast_var`. Persisted: face_source, face_paths. `_forget_deleted_refs` drops deleted face files too. The old `_actor_to_editor`/`_refdb_info` buttons are gone (methods remain, unused).
- **Deleting a file must let go of every reference to it (v1.46.0).**
  The editor (`ref_paths`), the border maker (`border_ref_paths`) and the
  animator (`anim_image_path`) hold PATHS into `output\`; after the user
  deleted a gallery image that was the swap's face, every gen-then-swap
  ran the base and logged "Face swap skipped ([Errno 2] …); kept the base
  image" — reported as "the selected image is never used". Found in
  app.log in one grep. `_forget_deleted_refs(goneset)` is called by
  `_delete_paths` and by Delete art files; `_generate`'s swap branch stops
  with "no longer exists … Nothing was generated" when the face file is
  gone; `_swap_face_pass` filters missing faces and logs failures with
  their traceback. Rule: any feature that remembers a path to a generated
  file must be on that list.
- **Gallery tags follow the image PATH** (`self.tagged` is a set of
  paths, `_toggle_tag(idx)` repaints one thumbnail via `_thumb_image(img,
  tagged)` with a red frame), so a rebuild after a delete never shifts a
  tag onto the wrong picture; `_rebuild_gallery` intersects the tags with
  the live session. `_delete_paths(paths)` is the one deletion routine
  (disk + session + tags + reselect); `_delete_current` routes to
  `_delete_tagged` when anything is tagged.
- **RAG map loading reports phases (v1.45.0).** `load_ragmap(path,
  progress=None)` calls `progress(phase, done, total)`: "reading" (one
  `json.loads`, no finer grain possible), "resolving" every 5000 entries
  with a count (the per-entry `is_file()` walk is the other big cost),
  "indexing", then "embeddings" (that is the code's order — the test
  asserts it). `_load_ragmap_async` posts `("ragmap_progress",
  gen, phase, done, total)`; `_rag_progress` paints `self.rag_prog`
  (determinate for resolving, indeterminate otherwise) and ignores a
  superseded gen; `_rag_progress_done` on `ragmap_loaded`. The bar and
  its label are `grid_remove()`d at rest — test with `grid_info()`, not
  `winfo_ismapped()` (always 0 on a withdrawn root). The RAG controls are
  their own section (heading "RAG MAP …") under the LoRAs.
- **Variations share one draw of references — by design.** Retrieval
  runs once per Generate click (in `_generate`), so a batch is one person
  in several poses and the next click is a new person. The user asked to
  keep it that way (2026-09-08); per-image retrieval would need the draw
  moved into the Generator loop with per-image uploads.
- **RAG retrieval is a seeded draw, not a fixed top-k (v1.44.0).** User
  report: "one prompt gives the same person in different poses". The
  index/walk both rank, then `_rag_shuffle(cands, k, rng)` reorders the
  best `max(8k, 32)` with rank-weighted sampling before `_rag_diverse`;
  `ragmap_retrieve(..., rng=None)` stays deterministic without an rng (the
  equivalence tests rely on that), and the app passes `_rag_rng()` — a
  fresh `random.Random()` when the seed is random, `Random(seed)` when
  fixed — so results are reproducible exactly when the picture is.
- **The left panel is a `ttk.Notebook` of three scrollable pages
  (v1.43.0)** — Image generation / Animation / Borders — each built by
  `_scroll_page(notebook, title)` (Canvas + scrollbar + padded inner
  frame, registered in `self._scroll_canvases`); the batch queue and the
  version row live in `self._page_bottom` under the notebook. The
  section-building code was NOT moved: `_build_ui` still builds top to
  bottom into a local `left` with a row counter `r`, and the tab split is
  three lines at the section boundaries (`left = self._page_anim; r = 0`
  …). A global wheel router walks `winfo_containing` up to whichever page
  canvas holds the pointer. The chosen tab persists as `ui.tab`
  (`<<NotebookTabChanged>>` → `_schedule_persist`). Page headings need
  `wraplength=400` — the pages are 432 px wide and a long heading clips.
  `update_ui_test` asserts which page each key widget sits on.

---

## 7. Testing

- **Graph tests need no engine**: call `build_graph` and assert on node
  wiring. Fast, and they catch the majority of regressions.
- **Live validation** drives the real `Generator` against the engine from
  a headless script. Cover every path: plain generation, LoRA chain,
  upscale, RAG guidance, each model family, border, editor.
- **UI tests** must drive a real `mainloop()` with `after()`-scheduled
  steps. An `update()` polling loop makes cross-thread `after()` fail and
  produces false failures.
- **A UI test that exercises persistence overwrites the user's saved
  state.** Back up `app/settings.json` before, restore after. Learned the
  hard way.
- **Leak tests**: clear leftovers *first* (a previous run's app can still
  be alive and the single-instance mutex will silently make your new
  launch exit); aim `CloseMainWindow()` at the process whose
  `MainWindowHandle != 0`, because a PyInstaller onefile app is a
  windowless bootloader parent plus the real child; compare `nvidia-smi`
  against a baseline taken with nothing running.
- `models_audit_test.py` asserts every model constant referenced in code
  exists in the manifest and still resolves upstream. Run it whenever a
  model is added.
- `engine_files_test.py` is the census for the engine swap / repair /
  add-on installer (no network, no engine — a local HTTP server and a
  `.cmd` standing in for pip). Every subject is named before it is
  tested, LODESTONE-style, so a missing subject is counted.
- `rag_lora_e2e_test.py` is the LoRA + RAG end-to-end on a live engine:
  it builds its own `cbac-ragmap/1` map from `output\*.png`, uploads the
  retrieved references, renders LoRA-only / +images / +embeds /
  combined / Flux+LoRA, fetches every PNG back and asserts the guided
  renders differ from the unguided one at the same seed. Start a second
  engine on 8189 for it (`main.py --port 8189 …`) so the user's app on
  8188 is never touched.

---

## 8. Dead ends — do not retry without new upstream

- **LayerDiffuse native transparency** (attempted v1.13.0, reverted).
  The `ComfyUI-layerdiffuse` node is stale against current ComfyUI: its
  `LayeredDiffusionDecodeRGBA` calls `JoinImageWithAlpha
  .join_image_with_alpha()`, a method that has since been renamed. That
  part is routable around with core nodes (`LayeredDiffusionDecode` →
  `InvertMask` → `JoinImageWithAlpha`; note ComfyUI's join **inverts the
  mask internally**, so the InvertMask is required, not a double
  negation). The injection does apply — the same seed differs across
  73.5% of pixels — but the transparent VAE decoder returns garbage: a
  uniform mask under `diffusers` 0.39, and a faint ghost rather than a
  silhouette under 0.31. Either polarity yields a fully opaque or almost
  fully erased image. Untried: Conv Injection (3.6 GB), which shares the
  same failing decoder.
- **In-app LoRA training** — worked, removed on purpose: it was the only
  feature needing git and a system Python. Build datasets in-app
  (`⭐ Add to training set`) and train externally.
- **Bezel composer** — superseded by the editor plus border references.
- **Masked (`SetLatentNoiseMask`) border generation** — produces a flat
  edge band with no inward complexity. Generate full-frame and cut.

---

## 9. Security posture

Summarised from `SECURITY.md`: safetensors only (`.ckpt`/`.pt` are
pickles and can execute code on load, so they are never offered or
accepted), HTTPS-only downloads, the CivitAI API key encrypted at rest
with DPAPI, downloaded filenames sanitised to a basename, and the engine
bound to loopback. The exe is unsigned, so SmartScreen shows
"More info → Run anyway" on other machines. Open items: SHA-256 pinning
for downloads, and code signing (needs a purchased certificate).

---

## 10. Open ideas

Native transparency by some other route than LayerDiffuse; a builder for
`.ragmap.json` from an existing captioned dataset; SHA-256 pinning;
code signing.
