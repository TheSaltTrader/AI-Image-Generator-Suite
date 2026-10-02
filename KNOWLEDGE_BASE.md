# Comic Book Art Creator — knowledge base

Everything learned building this app, written down so it does not have to
be rediscovered. `<project>` below means the folder holding
`ComicArtCreator.exe`.

Companion documents: `HANDOFF.md` (engineer onboarding + the build/release
runbook), `RAGMAP.md` (the RAG-map contract), `SECURITY.md` (threat model),
`TRAINING.md` (building a dataset), `CHANGELOG.md` (what changed when).

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
- **Startup "application has been destroyed" crash box (v2.5.0).** app.log:
  `ERROR unhandled ... main ... tkinter destroy ... _tkinter.TclError: can't
  invoke "destroy" command: application has been destroyed`, and the frozen app
  showed PyInstaller's "failed to execute script comic_art_creator" dialog. It
  was the SECOND-copy / update-relaunch path in `main()`: `wait_for_previous_
  instance(tick=root.update)` pumps events, the little "Waiting…" window can be
  closed mid-wait (default WM_DELETE destroys root, no _on_close set yet), then
  `_wait.destroy()` / `root.destroy()` raise on the dead root -> unhandled ->
  scary box. The MAIN copy was unaffected (log continued to "Starting local
  engine"). FIX: wrap the wait+destroy and the askyesno+destroy in try/except
  and `return` quietly. NB the internal entry script stays `comic_art_creator.py`
  (only the EXE was renamed at v2.4), so error text naming it is expected.
- **Flux image-guided RAG via Redux (v2.5.0).** Flux couldn't use a RAG map's
  IMAGES (IP-Adapter is SDXL-only) — it fell back to captions. Now Flux uses
  **Redux**, which is NATIVE in ComfyUI (no custom node): `StyleModelLoader`
  (`flux1-redux-dev.safetensors`, ~129 MB) + `CLIPVisionLoader`
  (`sigclip_vision_patch14_384.safetensors`, ~445 MB) + per-ref
  `LoadImage`->`CLIPVisionEncode`(crop=center)->`StyleModelApply`(conditioning,
  style_model, clip_vision_output, strength, strength_type="multiply"), chaining
  StyleModelApply over pos_ref. VALIDATED live on 8189: plain-vs-Redux mean
  pixel diff 88.7 at strength 1.0 (it basically CLONES the ref and overrides the
  prompt), so the app CAPS strength at `min(style_weight, 0.6)` so the prompt
  leads. GOTCHAS: (1) the SDXL IP-Adapter block in build_graph was NOT
  family-guarded — added `and fam not in ("flux","schnell")` or it fires the
  IPAdapterUnifiedLoader on Flux and 400s. (2) `start_engine`'s
  extra_model_paths.yaml was MISSING `style_models: style_models` — Redux model
  invisible without it. (3) Redux needs viewable IMAGES; an embeds-only map
  (.ipadpt are IP-Adapter/SDXL) has none, so Flux falls back to captions there.
  Models added to models_manifest.json (repo Comfy-Org/Flux1-Redux-dev &
  Comfy-Org/sigclip_vision_384) so they're on the update list + one-time
  download. Downloads (non-gated): huggingface.co/Comfy-Org/Flux1-Redux-dev &
  /sigclip_vision_384. update_ui asserts Flux->StyleModelApply/no-IPAdapter and
  SDXL->IPAdapter/no-Redux (165). SD3.5 (the other half of the user's ask)
  shipped in v2.6.0 — see the next bullet.
- **Recraft cleaned up + repeated decals (v2.24.0).** User, after the
  first real Recraft run: "lines are not straight for the GI JOE banner on
  all 4, small errors on the orca symbol and some of the dangers have
  imperfections", "the grey area in the whale was supposed to be
  transparent", and "if a decal is repeated multiple times, take the best
  version of it and replace it over the ones that might be imperfect".
  Findings from the per-decal SVGs: Recraft traced the RAW scan (wobbly
  keyed edges; one DANGER! = 1,149 shapes + 184 gradients following the
  halftone) and paints in LAYERS — the orca's clear ellipse was a magenta
  (or invented grey (166,170,172)) shape on top of a black ellipse, so
  dropping the backing exposed black; MuPDF ignores SVG <mask>, so a mask
  is no fix. Now: `decals.flatten_decal` (palette snap + edge band from
  solid neighbours + 3x3 majority, split out of the trace) prepares what
  is sent; `clean_svg` maps every fill (gradients → mean stop colour) to
  the decal palette ∪ magenta and drops magenta; `finalize` renders the
  answer with and without the backing and compares with the SENT
  picture: if removing the backing exposes paint, or the answer paints
  where the sent picture was backing, it re-traces Recraft's render with
  alpha = scan outline ∧ not backing (purple blends r−g>50 ∧ b−g>50 count
  as backing) ∧ Recraft painted, flattened with a band = the enlargement
  and orphan slivers dropped, traced with vtracer hierarchical="cutout"
  (stacked layers expose ink under clear areas the same way — also
  switched for redraw_sheet's own trace). Recraft's palette: merge 60,
  min_share 2%. Live after: banner 21 shapes, straight; whale overlap
  0.99, colour 12, clear ring; remaining: thin brown/white contour shapes
  Recraft itself draws along outlines (genuinely close to real inks —
  left). COPIES: `find_copies` — same size ±6%, tight crop on mid grey,
  48 px, blur 1.2, normalised correlation over ±1 px shifts, straight /
  turn / mirror; star groups (no chaining). Measured: DANGER! copies
  0.855-0.97 (exact-mask IoU only 0.60 through halftone, 32 px thumbnails
  0.14-0.36 — both useless), mirrored orcas 0.97, different decals < 0.49
  on p2 but different words of one size up to 0.93 on p1 → redraw_sheet
  draws up to 3 candidates, scores each against its own scan (iou −
  colour/400), and places the best on a copy ONLY if it fits that copy's
  scan (iou ≥ 0.75, colour ≤ 90); else the copy is drawn itself. p1: 23
  groups, 49 copies reused, worst placed overlap 0.82, no word swapped.
  Tests: vector_redraw 60, recraft 12, update_ui 444.
- **fal.ai account states (v2.23.2).** Live, with the user's real key
  (shape `fal_sk_<32hex>:<32hex>` — NOT the uuid:hex shape assumed in
  v2.23.0; the secrets gate now greps both): a tiny request returned 403
  {"detail":"User is locked. Reason: ADMIN."} = the key is VALID but the
  account is locked by fal.ai (new account, no credits/verification yet);
  the whale-sized upload got `SSLEOFError` because fal closes the
  connection on a refused account while the body is still uploading.
  `vectorize_decal`: on a connection error it re-asks with a 300 px
  probe to learn the real status; 401/403 messages carry fal's `detail`;
  "lock" → "the account is locked … add credits or finish the account
  check". The key was saved to the Credential Manager for the user. Live
  Recraft QUALITY still unmeasured until fal unlocks the account.
- **Text sweep: banners and condensed words (v2.23.1).** User compared a
  run (it was the VISION method — settings decal_method "vision", every
  decal_NN.svg a Claude/text-sweep drawing; Recraft had not run): "danger
  is completely cut off, the whale design takes liberties and the banner
  is not the same". Causes: `text_geometry`'s "word" rule (w ≥ 2.5h)
  accepted the SOLID white banner bar as a word → the GI JOE logo+banner
  became Arial "G.I.JOE"; fix: a word piece must fill < 0.75 of its box
  (letters leave gaps; a bar fills ~1.0). `typeset_lines` clamped the
  width to ≥ 0.7× Arial's natural width; the original DANGER! is
  condensed, so the word overflowed the crop and was clipped both ends
  (svg translate -77); fix: textLength = the row's width within 0.4-1.6×.
  `make_text_fn` check min_iou 0.4 → 0.65. Live: one DANGER! read and set
  complete, $0.004. On KW p2 only the 9 DANGER! decals now count as text.
  The whale "liberties" are the vision model drawing from description —
  for faithful logos use the clean trace or Recraft. Tests vector 56.
- **Recraft vectorize via fal.ai (v2.23.0).** User asked for a better
  vector API with its own key, pay-as-you-go only. Vectorizer.AI is
  subscription-only (monthly plans from $9.99, credits roll over 5x);
  fal.ai is prepaid credits, no subscription, Recraft vectorize $0.01 per
  image. `app/recraft_vectorize.py`: POST https://fal.run/fal-ai/recraft/
  vectorize, header `Authorization: Key <FAL_KEY>`, body {"image_url":
  data URI} (fal accepts base64 data URIs for file inputs), answer
  image.url → the SVG (fetched, or decoded when it is a data URI). Limits
  PNG/JPG/WEBP < 5 MB, < 16 MP, 256 < side < 4096 → each decal is sent
  alone, scaled so its short side ≥ 320 and long side ≤ 2048 (≤ 4x), on
  pure magenta; `clean_svg` drops shapes filled near magenta (hex,
  3-hex, rgb(), style fill) and rewraps the drawing into crop-pixel
  viewBox. `make_vector_fn` = same contract as the vision one; 401/403
  → "key rejected", 402 or balance/credit text → "top up" (run stops);
  other errors → trace fallback; `call_cancellable`; cost 0.01/call;
  `check_against_scan` gate. Key: Credential Manager
  'AIImageGeneratorSuite/fal', FAL_KEY env wins; the release secrets gate
  now also greps the fal key shape (uuid:32hex). Live quality NOT yet
  measured (no key at release time). Tests: recraft_test 10, update_ui
  444.
- **The freezes, proven; Vectorize rebuilt; no auto-Compare (v2.22.7).**
  stall.log (the v2.22.4 recorder) caught five freezes: 5-6.5 s with the
  worker in `vectorize` line 818 (vtracer on a WHOLE page, Process in
  Vectorize mode) and 178.3 s in `check_against_scan` → `mask` →
  Pillow `MaxFilter(k)` with k = 2·(2% of the crop)+1 ≈ 103 on a
  2546 px decal. Pillow's rank filters are O(k²)/pixel and, like vtracer
  (PyO3) and pymupdf, HOLD THE GIL for the whole call, so the Tk thread
  cannot run. Fix the CLASS: `decals.dilate_mask` / `erode_mask`
  (running sums via `_box_mean`, equal to Pillow's Max/MinFilter —
  tested), used in check_against_scan, redraw_sheet's band, the
  face-swap composite; `trace_sheet` now traces DECAL BY DECAL
  (`segment_decals` gap 6) so each vtracer call is short; `_majority`
  replaces ModeFilter. Measured with a 10 ms ticker: Vectorize p2 0.67 s
  longest starvation (was the whole call), the scan check 0.01 s (was
  minutes). Vectorize quality: the old path quantised the WHOLE page to
  16 MEDIANCUT colours (film-dominated → red became maroon), dropped
  light bluish fills as "halo" (= white ink on blue film) and lost thin
  letters to filter_speckle at 1x; the base layer came out #730073 on
  big canvases and was not recognised as background (full-page purple
  sheet = the "blank page") — `vectorize` now drops the FIRST path when
  it spans the padded canvas, by geometry. Per decal: palette_of (merge
  40, min_share 0.004, erode 1), enlarge 2-3x (≤ 3600 px), snap, edge
  band (1 scan px + soft alpha) takes the solid neighbour's index, 3x3
  majority, trace with drop_halo=False. TRIED AND REVERTED: a 2-px band
  and merge 70 — rings broke into dashes, letters got white holes.
  Auto-Compare off for every job (user: "Do not pop up the screen
  without the user pressing the button"); `_auto_compare` flag default
  False. Tests 441/54/20 (+ swap 22).
- **Pages to redraw (v2.22.6).** User: "When rewriting to vector, allow
  to select a specific page and not do all the pages from the pdf."
  `decals.parse_pages` ('all'/'' → None, '2', '1,3', '2-4', mixes;
  ValueError on 0, words, reversed ranges); `decal_pages_var` read on
  the UI thread in `_redraw_decals`; pass 1 enumerates
  `iter_sources` from 1 and skips unpicked pages BEFORE any work; no
  matching page → err "none of the pages asked for (…) is in the
  file(s)". Process decals is unchanged (all pages). Tests 437/54/20.
- **Cancel clears the Decals figures (v2.22.5).** User: "Cancel on top
  appears to stop the model, but does not clear the progress bar and
  number of decals or the cost." `_cancel_decals` resets the bar and both
  badges and sets `_decal_cancelled`, so late `decal_progress` messages
  from the stopping worker are ignored; `decal_done` with "cancelled"
  resets again; a new run (`_decal_progress_reset(new_run=True)`) clears
  the flag. `_cancel_generation` (the main ✕) now also calls
  `_cancel_decals` — it returned early for Decals jobs (self.busy is only
  set by image jobs). Tests 431/54/20.
- **Freeze recorder + decal upscale graph (v2.22.4).** User: "API model
  do not seems to work and then app crashes" / "shows currently as not
  responding" / "App returned, sees it was just stalled, can we verify
  what caused this?". app.log: every Process-with-AI-upscale since the
  graph was written was rejected — node 13 SaveImage had no
  filename_prefix (fixed); the vision model answered UNSURE (honest
  fallback to the trace, not a failure); the log stops at 22:19:43 with
  the job unfinished and NO Windows crash/hang event. The cause of the
  freeze could not be proven from the log. Measured with a ticker thread
  (how long a native call starves other threads): process_image 0.03 s,
  vtracer per decal 0.19 s, vtracer whole page 0.54 s, pymupdf render of
  a 20 MB SVG 1.08 s — none reaches the ~5 s Windows needs for "Not
  responding" on these sheets. Instrument: `StallWatch` =
  faulthandler.dump_traceback_later re-armed by a 1 s Tk heartbeat; the
  C watchdog thread needs no GIL, so a native call holding the lock is
  caught too; dumps go to stall.log beside app.log, and the next beat
  logs "the window was frozen for X s". Test blocks the Tk thread 1.2 s
  with a 0.6 s timeout and finds "Timeout", "Thread 0x" and "answered
  again". Tests 428/54/20. NEXT TIME it freezes: read stall.log.
- **No auto-Compare after Redraw (v2.22.3).** User: "when the redraw
  completes do not auto compare the last decal, leave to the user to open
  the compare." `_auto_compare_after_job(n, err, what)` returns for
  what == "redraw"; Preview and Process keep it (the v2.21 request).
  Tests 425/54/20.
- **White ink on tinted film (v2.22.2).** User: "In the whale image the
  white color gets replaced by transparent, even when the pale blue the
  whale's white gets replaced as transparent". Measured on KW p1-p3 (film
  (222,253,255)): white ink scans as e.g. (225,244,238)-(239,255,254),
  distance 18-20 from the film (< tol 52), and the old `is_white` cut
  (|tint| sum < 24) lost the bluer white pixels. Projection onto the
  film's tint: film 1.0 (5th pct 0.84 per pixel), white ink 0.3-0.85.
  `_carrier_alpha` (tinted, |tintC| ≥ 12): white = light > gray−12 and
  ((3x3-mean proj < 0.7 and 3x3-mean cosine > 0.9) or 3x3-mean |tint| <
  0.25 of the film's), opened 3x3. The COSINE matters: film blended with
  a red edge also loses film tint but turns toward red — without it the
  DANGER letters got 2-4 px white blobs (sweep: proj<0.7 & cos>0.9 →
  rim 0.3%, REMOVAL ink kept 97%; proj<0.6 lost the whale belly).
  Enclosed areas are judged as REGIONS: `fill_enclosed_holes(carrier=)`
  on tinted film fills a hole only if its mean proj < 0.9 (clear windows
  read 1.00, white ink 0.58-0.89) — process_image now always runs it
  (photo/white paper without the tint test). Counter rule rewritten: a
  hole is a counter when compact (aspect ≤ 3), ≥ 25% of the host's SHORT
  side (vertical words!) and the host is letter-sized (short side ≤
  15 mm) — a big printed block's discs are windows. Re-checked: the
  photo's gauges (211 holes) still white, the DANGER counters clear.
  Tests 424/54/20.
- **Immediate cancel of model calls (v2.22.1).** User: "cancel the
  running job takes some time … when cancelling an llm redraw it is not
  immediate". The SDK / Ollama call blocks for the whole request and the
  flag was only checked between decals; then `redraw_sheet` traced the
  decal (an engine ESRGAN step) before looking again.
  `vector_redraw.call_cancellable(fn, cancelled)` runs the call on a
  daemon thread and joins in 0.1 s steps, raising `Cancelled` (the late
  answer is dropped); vector_fn / text_fn return None on it;
  `redraw_sheet` returns None right after text_fn / vector_fn when
  cancelled (no trace fallback); the orientation ask is wrapped too and a
  cancelled answer is not cached; pass 2 checks the flag per page. Test:
  a 5 s fake call is abandoned in < 1 s. Tests 422/54/20.
- **Print beside Save As (v2.22.0).** User: "Add a print button next to
  save as so it possible to print the svg in the highest quality straight
  to the user". The default PDF handler here is Edge (ProgId MSEdgePDF),
  which has no "print" shell verb, so `os.startfile(pdf, "print")` is not
  a route. `print_export.print_pages` renders each layout page at 600 dpi
  (SVG pieces through `render_svg`, so edges are vector-sharp at printer
  resolution), flattens on white, and runs `PRINT_PS1` (powershell -STA):
  System.Windows.Forms.PrintDialog (UseEXDialog, a TopMost 1-px owner so
  it is not behind the app) + System.Drawing.Printing.PrintDocument; the
  PrintPage handler draws in GraphicsUnit.Inch at the page's true size,
  shifted by −HardMarginX/Y (OriginAtMargins=false puts the origin at the
  printable corner); after OK the printer's PaperSize matching the layout
  is chosen and Landscape set for a wide page. `-CheckOnly` loads the
  pages and exits — the test runs the REAL script that way. App:
  `print_btn` → `_print_current` → `_print_piece_for` (decal = true size
  + its SVG; other = 300 dpi fitted to the page) → `print_piece` (layout
  with tiling) on a worker; `print_done` reports. Tests 422/51/20.
- **Compare opens by itself after a job (v2.21.0).** User: "preview one
  decal and all the other process under decal that modify the original
  image should display the original and the modified image so the user
  can compare". `decal_add` remembers the run's last entry with a `src`
  (`_last_run_entry`); `decal_done` without an error calls
  `_auto_compare_after_job`, which opens Compare on it with auto=True
  (the job's own "Done" status line is not overwritten) unless the open
  Compare window already shows that result (a Re-run just landed there).
  Generate → SVG has no original and is skipped. Tests 419/51/17.
- **Pull progress bar (v2.20.1).** User: "next to pull local model there
  should be progress bar to show how long its going to take". Ollama's
  /api/pull streams one status per LAYER (digest, total, completed);
  `ollama_pull(on_bytes=)` keeps per-digest totals and reports the sums
  (the bar may dip slightly when a new layer appears — the big weights
  layer comes first). `pull_eta_text(done, total, elapsed, start_done)`
  measures the rate from the first figure seen (a resumed pull starts
  part-way). UI: `decal_pull_bar` + `decal_local_lab` beside the button,
  `decal_pull_progress` queue message, throttled to ~4/s. Also: "if its a
  local model the api key next to it should be greyed out" —
  `_refresh_vision_key_lab` follows the model (trace on
  `decal_vision_model_var`). Tests 418/51/17.
- **Print export, multi-sheet tiling, transparent vision output, paper
  colour key, local vision models (v2.20.0).** User: "When using vision to
  recreate the decals, add the stickers on a transparent background … make
  sure the bigger decals can now be exported to multiple sheets … Add an
  export button to multiple formats ready for printing" / "add the best
  local models for decals in the list" / "select a sheet of paper to glue
  the stickers on and select the color paper as the transparent color" /
  "Option should be simple and next to the browse button".
  `app/print_export.py` (17 checks in print_export_test.py): `Piece`
  (label, w_in, h_in, svg text, png RGBA) with `crop()` by fractions —
  the SVG is cropped by viewBox so a tile stays vector;
  `pieces_from_entry` (redraw → its decal_NN.svg/png files; preview → one
  piece; processed sheet → `segment_decals` on the PNG, each piece's SVG a
  viewBox crop of the sheet SVG); `layout` (shelf packing, taller first,
  tiles anything over the printable area with a 0.2 in overlap); writers:
  PDF via pymupdf (`open("svg").convert_to_pdf()` + `show_pdf_page` keeps
  vector; PNG via `insert_image`), PNG pages (RGBA transparent, dpi tag),
  SVG pages (nested `<svg viewBox>` + embedded PNG). Sizes already include
  size_scale (every decal entry's size_in does). Vision transparency:
  `strip_background` drops rects covering ≥ 95% of the viewBox when the
  scan's corners are clear (`_corners_clear`). Paper colour: one button
  beside Add files (`decal_paper_swatch`, `_choose_decal_paper` → sample
  `_sample_decal_paper` → colorchooser on that colour; Cancel = auto);
  `prep["paper"]` reaches `process_image(carrier=)`, the film colour, and
  `assess_source(carrier=)`; `prepare_photo(normalize=False)` so the
  flattening does not turn the paper white. GOTCHA: a page whose border is
  coloured paper looks like a PHOTO to `looks_like_photo` and the sheet
  finder crops down to the stickers — when the border already is the
  picked paper, no crop; the sampler picks the candidate colour covering
  most of the page (a table only shows at the border). Local models:
  `MODELS` entries `ollama:<tag>`, `OllamaVision` mimics
  `client.messages.create` / `client.beta.messages.create` over
  `/api/chat` (system + images, think false, temperature 0); errors raise
  "Ollama: …" which `_account_stop` / the vector_fn turn into a run stop;
  cost 0; `ollama_pull` streams progress. GOTCHA fixed on the way: the
  orientation helper read the Tk dropdown from a worker thread ("main
  thread is not in main loop") — the model is now captured on the UI
  thread. Tests update_ui 414, vector_redraw 48, print_export 17.
- **Text sweep, enclosed holes, Cancel (v2.19.0).** User: "Can Jev AI be
  used to do a quick sweep with AI vision to analyse that decals use
  letters or numbers and have IA recreate those with the correct font
  rather than try to draw them, some stickers with letters end up
  mangled. Also, can it check when a sticker is a graphic and has a
  transparency inside it to use white instead of transparent, example of
  the circle with the startr" / "Edit image and decals does not have a
  cancel". Jev cannot see pixels (it is a text decision model) and the
  eye OCR cannot read 24 px labels, so the sweep is the vision model's
  reading + fontTools typesetting. `decals.text_geometry(crop)`: pieces
  via `_label_runs` (run-based union-find that also returns a label image
  and per-label box/area; diag=True for ink), letter-like = 6 px..70% of
  the crop high, aspect ≤ 6, each ≤ 20% of the ink; rows by vertical
  centre within 0.6× the median height, rows need ≥ 2 pieces; text when
  rows hold ≥ 85% of the ink. `vector_redraw.read_text_decal` asks for
  JSON lines {text, colour, weight, italic} + align at effort low
  (READ_SYSTEM: never guess → UNSURE); `typeset_lines` sets each line in
  its row: cap height = row height (÷1.22 with descenders), size = cap ×
  upem / sCapHeight, x from align, textLength = row width clamped to
  0.7–1.35× the face's natural width, colour snapped to the palette,
  `<text>` → outlines through `text_to_paths`; `_find_font` now knows
  regular / italic / bold-italic faces (arial.ttf, ariali, arialbi) —
  the old default (bold) is unchanged. `make_text_fn` = read → typeset →
  `check_against_scan(min_iou=0.4)` → render; stats text / text_fallback
  / unsure / calls / cost; account-level API errors raise (`_account_stop`,
  shared logic) so the run stops. `redraw_sheet(text_fn=)` tries it FIRST
  on text decals, then vector_fn, then the trace (`_place` closure shares
  the placement). The app builds text_fn for any method when the switch
  is on and a key exists. ENCLOSED HOLES: `fill_enclosed_holes(rgba,
  min_side, min_area)` labels the clear mask 4-connected (so a diagonal
  ink line still encloses), drops labels touching the border, finds each
  hole's HOST (the ink piece left of its top-left run, from an
  8-connected labelling of the ink) and fills the hole white unless it
  is a letter's counter = at least 25% of the host's height (A/R/e ≈
  0.3, D/B ≈ 0.45, O ≈ 0.6; the digits, dashes and discs inside a
  printed block are a few percent of it). Calibrated on the user's
  photo: an absolute 1.2 mm floor filled 64 holes INCLUDING the DANGER
  counters (touching bold letters make the host a whole word, so a
  width test fails too) while leaving the 5 px white dashes clear; the
  height-ratio rule with a 0.3 mm floor fills 191 holes = every white
  mark inside the two printed blocks and the panel stripes, no counter.
  `process_image(fill_holes=True)` applies it after clean_matte on
  white-keyed sources only (photo or neutral carrier), side = max(4 px,
  0.3 mm); `out["holes"]` counts. CANCEL: `_cancel_decals` →
  `cancel_all()` (shared flag + engine /interrupt + queue clear) and the
  Process worker now checks the flag per file and per page (it never
  did); Generate → SVG clears the flag at start; `_set_decal_buttons`
  keeps Cancel live only while `_decals_busy`; the Edit image tab's
  Cancel is `_cancel_generation` (the bottom-bar ✕). update_ui 402,
  vector_redraw 42.
- **Compare with the original (v2.18.0).** User: "To validate stickers,
  when generating an image or apply option to clean it, it would be
  essential add a compare with the original side by side within the
  interface so a user can tweak things." New module `app/compare_view.py`:
  `Pane` (image, ppi, offset on an inch grid, backing, optional svg),
  `render_frame(pane, W, H, s, ox, oy)` — a pure function (tested without
  a window) that crops the visible window in image px and resizes it
  (LANCZOS below 1:1, BILINEAR to 3×, NEAREST past that so pixels show
  honestly; an SVG pane past 1:1 goes through
  `vector_redraw.render_svg_region`), `wipe_frame`, and `CompareWindow`
  (Toplevel: side-by-side or wipe, wheel zoom about the cursor, drag pan,
  Fit, 1:1, Re-run). The two sides never share pixels, only INCHES: the
  original pane's ppi is the source dpi; the result pane's ppi is its
  pixel width over the original's inch width (a sheet) or the decal box's
  inch width (a preview, with its offset = box corner − crop corner), so a
  size_scale ≠ 1 result still overlays the original. App side: every
  decal entry now carries `src`, `page`, `kind` (process/preview/redraw),
  `src_dpi` and, for a preview, `box` (scan px); `_open_decal_compare`
  loads the page via `iter_sources` + `_prepare_source` with the cached
  orientation on a worker, posts `decal_compare_ready`; `_compare_rerun`
  calls `_process_decals(only=src)` / `_redraw_decals(preview, only=src)`
  (new `only=` on both; the source is added to the list if missing) and
  `_compare_refresh` swaps the result pane when the matching `decal_add`
  (src, page, kind) lands. The UI test runs the whole loop for real on
  the 200-dpi probe PDF: open → original 400×200 @ 200 ppi, result ppi
  200 → Re-run → process worker → new pane swapped in.
- **Run figures + progress bar on the Decals tab (v2.17.1).** User: "Make
  the vision model decal count show in green and the cost in red. Add a
  progress bar under showing the progress for the whole request." A Tk
  label holds one colour, so the status line keeps the words and two
  badges beside it carry the figures: `decal_count_badge` (style
  `Good.TLabel`, #22c55e) and `decal_cost_badge` (`Cost.TLabel`, #ef4444),
  with `decal_progress` (ttk.Progressbar, maximum 1000) underneath. One
  queue message feeds all three: `("decal_progress", fraction, green_text,
  red_text)`; `decal_done` without an error fills the bar;
  `_decal_progress_reset()` empties it at the start of a run. "Whole
  request" needs the total up front, so `_redraw_decals` now runs in two
  passes: pass 1 prepares + cuts out every page (`process_image` +
  `segment_decals` with the same gap/min_side as `redraw_sheet`, so the
  per-page count equals the n the callback reports), pass 2 redraws with
  `prog(i, n)` mapped to (decals done before this page + i) / total. The
  vision figures come from `stats` (ok / fallback / cost) that
  `make_vector_fn` keeps. Process decals reports by file.
- **Pre-flight assessment, DPI from the file, automatic photo path
  (v2.17.0).** User: "If the process can validate the picture, determine
  the quality and success rate based on picture or scan and give you a
  success rate and recommendation before trying to convert" / "image output
  should be analyzed automatically by the process, not a selectable field
  by user" / "talking about the DPI of the image being added".
  `decals.iter_sources()` yields (label, image, dpi): PDF dpi = embedded
  image px / `page.get_image_rects()` width in inches (pymupdf) —
  Cobra_001.pdf is 200 dpi, every other sample 300; raster dpi =
  `Image.info["dpi"]` when > 96 (72/96 are placeholders), else None; a photo
  (no tag) → `dpi_from_width` when the user typed the sheet width, else 300
  assumed and flagged in the report. `assess_source()` calibration (KW p3
  at 300 dpi: letters 23 px, sharpness 246, blockiness 1.35; Cobra_001:
  25 px, 117, 1.81): letters_px = median short side of pieces with
  short ≥ 9, long ≤ 90, long ≤ 4×short — the first cut took the smallest
  third of all pieces and read 8 px of specks; blockiness is measured on
  the RAW picture (the 8-px JPEG grid is lost once a photo is
  cropped/straightened); a photo's sharpness is divided by the flattening
  gain² (250 / sheet grey) or a dim, soft photo reads "sharp" (807 vs 107).
  Scores: ramps detail 12..55 px, focus 40..300, clean 1.4..1.9, even
  σ 8..30; separation penalty ×0.35 when the opaque share ∉ [2%, 60%].
  Photo path: `looks_like_photo` (border not bright/neutral) → `find_sheet`
  (largest region differing from the border colour, 4 extreme corners) →
  `straighten` (PIL QUAD, 1% inset) → `normalize_photo` (brightest colour
  mode ≥ 2% = the sheet; block-wise illumination field of sheet-coloured
  pixels, holes filled from neighbours; picture × 250/field → sheet white,
  cast removed) → `process_image(photo=True)` keys on `PHOTO_WHITE` and
  `_drop_border_fringe` clears border-HUGGING long pieces that are thin
  (< 10%) or sparse (< 30% of their box) — `_label` is a BFS on a 4× grid.
  DEAD ENDS: painting "table-coloured" pixels white (the raw border median
  was the RED decal block, since the sheet filled the frame, and the red
  block got painted; the wood crescent and the black block are
  colour-identical in a dim shot — only shape separates them); a no-key
  orientation heuristic (centroid-alignment entropy of letter-sized pieces:
  the upright Z06 sheet reads "sideways", grids of decals align both ways)
  — orientation is the vision model's answer only
  (`vector_redraw.ask_orientation`: one ≤ 1024 px picture, effort low),
  cached per (source, page) in `App._decal_orient`, cleared with the list.
  Result on the user's phone photo (1440×1920, sideways on dark wood, no
  EXIF): 30 decals cut (the red and black printed blocks as one, 29
  stickers), the sliver gone; white-on-white stickers lost by design.
  update_ui 376, vector_redraw 32.
- **Taskbar, second attempt (v2.16.3).** After v2.16.1 the user still saw
  the pin unlit and "clicking it starts a new session". app.log showed no
  second `start` line and no "Already running" prompt — the second copy DID
  focus the running window, but only after main()'s "Waiting for the
  previous copy to finish closing…" box had polled the mutex for up to 12 s
  (that wait ran BEFORE the focus check), on top of ~15 s of one-file
  extraction: it looked like a fresh launch. FIX: focus-first — when the
  mutex is held, `_focus_running_instance()` runs immediately (SHIFT = old
  prompt), the wait box only when no window is on screen. The unlit pin:
  the taskbar caches a pin's identity; a stamped .lnk may not be re-read
  until Explorer restarts, and a copy relaunched by the updater
  (`--after-update`) sat under a different identity. FIX: `set_window_aumid`
  — the per-window PKEY_AppUserModel_ID via SHGetPropertyStoreForWindow +
  IPropertyStore, called through raw COM vtable slots in ctypes (QI 0,
  AddRef 1, Release 2, GetCount 3, GetAt 4, GetValue 5, SetValue 6,
  Commit 7; PROPVARIANT 24 bytes: vt + 3 reserved WORDs, then the union;
  c_wchar_p field keeps the string alive). GOTCHA: Tk's top-level HWND
  (`GetParent(root.winfo_id())`) is 0 until the first event-loop pass —
  a tag in the first line of App.__init__ silently failed; it now runs from
  `root.after(0)` with retries. `get_window_aumid` reads it back (test).
  update_ui 357.
- **"Even with AI the results are terrible, pixelated, missing characters"
  (v2.16.2).** app.log: the user's first big vision run (Killer Whale p1,
  ~145 decals) hit `You have reached your specified API usage limits. You
  will regain access on 2026-11-01 at 00:00 UTC` (the Anthropic workspace's
  monthly cap on the key seeded from CDG Spec Forge) after 17 drawings;
  187 calls were refused and `make_vector_fn` silently fell back to the
  clean trace for each → 224 traced vs 17 drawn files; the user judged the
  TRACE. FIX: an account-level refusal (usage/spending limit, credit,
  billing, PermissionDenied, Authentication) now RAISES → the run stops
  with "Anthropic stopped the calls: … raise the limit at
  console.anthropic.com → Settings → Limits". ASKED whether Jev (now in AI
  Vision v1.6.0 as `eye_judge`) would help drawing: NO — Jev is a text
  yes/no/pick-one decision model over OCR text, it neither sees pixels nor
  draws. OCR EXPERIMENT (winocr, system Python 3.12, 4× crops, 4 rotations,
  binarised ink masks): reads FUEL and the title, but the ~24 px rotated
  white labels come back as ':QALINFO' / '11' — at 300 dpi those letters
  are not in the data; the vision model says UNSURE on the same crops and
  the trace renders them jagged. The honest lever is the SCAN: 600–1200
  dpi for sheets with 2 mm labels (since v2.17 the app reads the file's
  own DPI; a better scan is the lever). update_ui 355, vector_redraw 31.
- **Taskbar pin never "lit", clicking it started a second copy (v2.16.1).**
  User: "When loading the app, the icon does not show its loaded, so when you
  click on it, it tries to restart it." The taskbar matches a running window
  to a pinned button by AppUserModelID: the process sets
  `SetCurrentProcessExplicitAppUserModelID(APP_AUMID)`, so a pin Windows
  creates from the RUNNING window carries that ID (the old H: pin did), but a
  pin/shortcut made from the exe FILE (the "(2)" pin, my Start Menu .lnk)
  carries none → a different identity → no running state, and a click
  launches a new copy (→ "waiting for the other copy… open anyway?"). FIX
  NOW: stamped `System.AppUserModel.ID` on the three .lnk files via
  IPropertyStore (C# through PowerShell: SHGetPropertyStoreFromParsingName
  GPS_READWRITE, PKEY fmtid 9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3 pid 5,
  VT_LPWSTR; Shell.Application's ExtendedProperty read is STALE after a
  write — read back through the store; release the store before reopening
  the same file or you get ERROR_SHARING_VIOLATION). FIX IN THE APP:
  `_shortcut_fix_script(exe)` + `_ensure_shortcuts()` run once per launch
  (thread, PowerShell -File from %TEMP%): create the Start Menu entry if
  missing, stamp the ID on every .lnk in Start Menu / User Pinned\TaskBar /
  Desktop whose target file name is ours, retarget a pin whose target is
  gone; never unpin/delete. `_focus_running_instance()` (EnumWindows by
  title prefix, SW_RESTORE, ALT-tap + SetForegroundWindow) makes a launch
  while running bring the open window forward; SHIFT held = the old "open
  another window?" question. The taskbar may only pick up a changed pin ID
  after Explorer restarts / next sign-in — tell the user: unpin, then pin
  from the running window if it still doesn't light. update_ui 355.
- **Redraw to vector rebuilt: clean trace / vision model / re-imagine
  (v2.16.0).** User on the diffusion redraw of Killer Whale sheet 2: "That is
  some terrible work, redrawing svg, all look terrible" — rightly: SDXL
  img2img on 2 mm text ("FUEL" → "FU:L"), the symbol panel and the sensor
  decals turned to mush; the white-ink decals were there as pure white but
  INVISIBLE on the white gallery preview. Asked whether Jev (Jarvis) could
  help: no — Jev is TypeSafe's TEXT decision API, no image input. What does:
  a VISION model DESCRIBING the decal and writing the SVG (demo by hand on
  FUEL + B11 convinced the user: `output/decals/_demo_scan_vs_diffusion_vs_
  vector.png`). BUILT: `app/vector_redraw.py` — official `anthropic` SDK
  (1.11, bundled via collect_all; httpx2/pydantic ride along), `draw_decal`
  = image block (crop on white, enlarged to ≥1024 px for legibility, but the
  viewBox stays the crop's own pixels) + printed size + palette hex →
  `claude-opus-5-5` default / `claude-sonnet-5-5` option, adaptive thinking
  (no `thinking` param), `output_config.effort=high`, `fallbacks="default"` +
  beta `server-side-fallback-2026-07-01` via `client.beta.messages.create`
  (plain `messages.create` on a 400), stop_reason=="refusal" → raise;
  `text_to_paths` = fontTools glyph outlines from C:\Windows\Fonts\arialbd/
  ariblk (x/y, font-size, text-anchor, textLength, dominant-baseline central,
  transform) so the SVG needs no font; `check_against_scan` = dilated-mask
  IoU ≥0.45 + BLURRED mean colour ≤110 (the first per-pixel version failed a
  correct B11 because a different font shifts glyphs; a colour-share
  histogram test was then tried and DROPPED — on the first live run it
  rejected the title, a triangle and FUEL at 0.96/0.80/0.89 overlap because a
  small decal's anti-aliased edge pixels skew its colour shares); `make_vector_fn` = the closure redraw_sheet calls (AuthenticationError
  raises → run stops; other errors / rejected drawings → None → clean trace;
  stats calls/ok/fallback/cost). KEY: `AIImageGeneratorSuite/anthropic` in the
  Windows Credential Manager via ctypes CredReadW/CredWriteW (UTF-16 blob, the
  pywin32/keyring convention), ANTHROPIC_API_KEY env wins; seeded once for
  this user from his `CDG Spec Forge:anthropic` entry (Jarvis holds only a
  `jev@Jarvis AI Suite` key); the key is NEVER in settings.json (asserted in
  update_ui), logs, repo or zip — release step greps for `sk-ant-`. METHODS in
  the tab (`decal_method_var`: trace default | vision | diffusion): trace =
  `build_decal_upscale_graph` (LoadImage→ESRGAN→ImageScale, no sampler; plain
  LANCZOS when the engine is down) + palette snap + vectorize; the diffusion
  path is "Re-imagine" with its strength slider. `decals.redraw_sheet` gained
  `vector_fn` (returns (svg, raster) in crop px → placed with a bare
  translate) and `limit` (👁 Preview one decal → decals\_preview, gallery shows
  the single decal). GROUPING: `_group_boxes` size-aware — pieces join within
  `gap` only when one is SMALL (<48 px), big+big only when within `touch`=6 px
  (fragments of a faint decal); `gap` from the UI in mm (1.0 default →
  12 px at 300 dpi; 1.4 let the "LOAD INFO" label chain letter by letter
  into the "M" badge above it; 0 = none); bridge dilation only when
  `bridge//down ≥ 1` (at quarter scale any dilation merged 8 px). Counts at
  gap 17 px: Cobra_001 5, Cobra 19 (faint ghost logos fragment), KW p1 145 /
  p2 23 / p3 53 — every separate sticker is its own decal now, so a vision
  run on p1 is ~145 calls: the cost line + Preview-one exist for that. BAND
  RULE FIX: thin white-ink text on its own vanished in the clean trace (the
  light pixels in the edge band were dropped as fringe) → a band pixel is
  dropped only when the SCAN was not light there (`~scan_light`). PREVIEWS: `_on_film(rgba, carrier)` composites on the sheet's
  carrier colour (neutral → (205,215,225)) so white ink shows. TWO MORE
  LIVE LESSONS: (a) the crop was sent to the model ON WHITE → white-ink
  labels were invisible to it and left out of otherwise-accepted drawings
  → `_crop_png_b64` now composites on a grey-blue BACKING (170,185,195) and
  the ask says the backing is not part of the decal; (b) with the labels
  visible, Sonnet drew small ROTATED white text as glyph-like junk and a
  rotated "M" as a bowtie, and the silhouette check passed them (0.79/0.90
  overlap) → the system prompt now tells the model to answer UNSURE instead
  of guessing when it cannot read a word (`Unsure` → fallback to the trace,
  counted in stats["unsure"]); the clean trace renders those labels
  legibly, so the fallback is the right answer. Opus read the "M" correctly
  where Sonnet did not — Opus stays the default. USER ASK (13:5x): "validate
  the data showing are svg, these should zoom without losing quality and
  those are the files we can download" → vector results now carry
  params svg/png/film/size_in, the gallery entry path IS the SVG,
  `_draw_frame` renders the zoomed region from the SVG via
  `vector_redraw.render_svg_region` (pymupdf clip render, doc cached), the
  caption says "SVG vector · W × H in", `_save_as` offers SVG (PNG alt;
  `_save_source` picks by extension), `_delete_paths` removes the twin.
  Tests: vector_redraw_test 30 (fake client — no network), update_ui 350.
- **White-paper scan → one giant decal + invented art (v2.15.2).** The user's
  first real AI-redraw run was Cobra_001.pdf: a white sticker sheet (mostly
  white-ink decals: blank rectangles/circles, white "GIJ-74J S-653x" text)
  scanned on WHITE paper, carrier (250,247,246). `_carrier_alpha` protects
  neutral white (`is_white`) so white ink survives on the blue film — on white
  backing that kept 970k of 974k px opaque, `segment_decals` returned the whole
  page as ONE box, the AI got a near-blank 976×998 canvas and wrote "LEAN"
  into it (the prompt's own word "clean" leaking in as typography). FIX:
  `is_neutral_carrier()` (chroma < 14, brightness > 225) → the keyer drops the
  white protection and keys on plain distance; the redraw worker notes the
  sheet was on white paper (white-ink decals are left out — they cannot be
  told from the backing and would not print at home anyway). Validated
  headless: tol 79 → the 5 coloured/dark decals (two red "3" badges, the red
  arrow, the crosshair, the black panel) as separate boxes; tol 52 also picks
  up faint sticker-edge fragments. Also: the engine wait left "Loading the
  model…" on the MAIN status after a decal job (decal jobs report on the
  Decals line) → decal_done now resets status_var + the bar. The user's
  "software is not on screen but still loaded" was simply the window
  minimised (IsIconic; restored with eye_raise) — nothing hung. update_ui 335.
- **"Update failed: the release zip has no AIImageGeneratorSuite_old_16396.exe"
  (v2.15.1).** The user's v2.13.1→v2.14.0 self-update renamed the running exe
  aside as `AIImageGeneratorSuite_old_16396.exe`; the old copy's PyInstaller
  bootloader then sat for hours on a "Failed to remove temporary directory
  _MEI465402" box (one file, MSVCP140.dll, still held — most likely by the
  engine the old copy's boot-retry had just restarted, which inherited the
  bootloader's DLL search path), so the new copy's startup `sweep_old_exes`
  could not delete the leftover. Two hours later the user, browsing the
  install folder, double-clicked the look-alike `_old_16396.exe` (twice);
  `APP_EXE = Path(sys.executable).name` became that name, `stage_update`'s
  `rglob(APP_EXE)` found nothing in the zip → the error. FIX: `self_update.
  canonical_exe_name()` strips `_old_<pid>` so APP_EXE is always the real
  name (update + install + relaunch all target it; `sweep_old_exes` pattern
  right again); `main()` detects a start from a leftover and offers to open
  the real exe next to it; the sweep also runs on close and 15 min after
  start (threading.Timer — no Tk from the worker). RULE: never leave a
  look-alike exe in the install folder longer than necessary; the user WILL
  click it. The install was brought to v2.15.1 by copying the release files
  over `AIImageGeneratorSuite.exe` directly (it was not the running image).
  self_update_test +6, update_ui +1.
- **AI redraw of decal scans → SVG at the printed size; equal-width scrolling
  tabs; red Decals buttons (v2.15.0).** User: "wire the redraw to vector so AI
  is used to redraw the provided pdf image decals if they are low quality and
  re-create them in svg in the correct size", "make sure the tabs are all the
  same size and there is an arrow to move left and right if there are more
  tabs later", and the three Decals action buttons "in red like the generate
  buttons from the other tabs". DESIGN (`decals.redraw_sheet`): the scan is
  the authority for what the AI must not invent — each decal's SILHOUETTE
  (alpha cut from the faithful cleanup) and its COLOURS (`palette_of` the
  crop, `snap_palette` the redraw onto it); the AI (image-to-image on the
  crop, `build_decal_refine_graph`: LoadImage → RealESRGAN → ImageScale →
  VAEEncode → KSampler at the Redraw-strength denoise, main tab's model +
  LoRAs) supplies clean edges and detail. `segment_decals` = run-based
  union-find connected components on a 4× reduced, 16 px-bridged mask (no
  scipy, so nothing new in the exe). Sheet SVG nests each decal's paths in
  `<g transform="translate(x0 y0) scale(cw/wr ch/hr)">` with the page in
  scan px and width/height in INCHES (`svg_set_physical_size`), so it prints
  at size; per-decal SVG/PNG too; PNGs carry DPI. THREE LESSONS FROM THE LIVE
  RUNS (user's Cobra.pdf, DreamShaperXL-Turbo + Decals LoRA, 12 decals, ~50 s):
  (1) a pink/grey FRINGE traced around every decal — the scan's silhouette is
  ~2 px wider than the shape the AI draws, the gap held the AI's white
  background, and the palette had pink edge-blend entries from the
  anti-aliased scan edge → palette now sampled 2 px INSIDE the silhouette with
  <1% entries dropped, and LIGHT pixels in a 1.2%-wide band along the outline
  are made transparent (white ink deeper inside stays); (2) "bold clean
  outlines" in the style prompt made the model draw a grey contour around
  each decal → removed, "outline/border/drop shadow" in the negative; (3)
  vtracer STACKED mode paints the DOMINANT colour as a full-canvas base layer
  — a decal filling >50% of its crop traced as a solid rectangle once the key
  paths were dropped → `vectorize` now traces on a 25%-a-side key margin
  undone with `<g transform="translate(-pad -pad)">` (this also fixes the
  plain Vectorize/Generate→SVG paths for tight crops). Scan "white" ink reads
  as film-tinted (230,235,234): snapped to pure white (printers leave white
  bare; a faint grey fill would print). TABS: `TabStrip` (canvas-drawn, one
  shared width = max(MIN 80, longest word, avail//n), arrows enabled only when
  n×width > strip, selected kept in view, wheel scrolls) above a
  `Left.TNotebook` whose `.Tab` layout is disabled — note `Style.layout(name,
  [])` sets the layout to the word "null" (reads back as [("null", {})]).
  At the default 470 px pane the five tabs are 82 px each, no scrolling;
  narrowed to 300 px they scroll 3-at-a-time. FIXED ALONG THE WAY: Decals
  "AI upscale" called `self._upload_pil/_await_images/_fetch_image/client_id`
  on the App (they're Generator's) inside a bare except → silently returned
  the input; now borrows a `Generator(self.ui_queue)`. Vector mode + AI
  upscale ticked used to drop the figure-scale conversion. update_ui 331.
- **Decal generator + solidify/smooth restoration tools (v2.14.0).** (User went
  autonomous, "select the best recommendation".) GENERATE→SVG: a prompt box +
  "🖊 Generate → SVG" button generates ORIGINAL art from the user's text prompt
  (NOT reproducing an uploaded copyrighted decal — declined that repeatedly) via
  the MAIN tab's model/LoRA/RAG, then traces to SVG. Reuses `_generate` through a
  `_decal_gen` flag set only across the synchronous param build: `_generate`
  reads `decal_prompt_box`, appends a flat-vector style, forces transparent, and
  tags `params["decal_svg"]`; `_finish_image` then `decals.vectorize`s the result
  → SVG+PNG in `output/decals`. Pure txt2img (no image ref → no reproduction).
  VALIDATED LIVE (dev 8189, Juggernaut, original geometric shield prompt →
  vectorize → 281KB SVG w/ paths; scratchpad gen_svg_validate.py). RESTORATION
  TOOLS: `solidify_black` (default ON) snaps patchy near-black+low-sat pixels to
  #000 (bad scan of black ink = mottled grey) — greys/dark colours untouched;
  `smooth_flats` (optional) edge-preserving de-mottle: median only where local
  variance (integral-image `_box_mean`) is low, so flat areas flatten but edges/
  text stay sharp and colours don't shift. Both wired as tab toggles + process_
  image params. IP LINE HELD: declined to build an "AI redraw of the copyrighted
  decal art / LLM-gated reproduction" — restoring the user's own scan + a general
  vectorize/trace + generating ORIGINAL art from a prompt are all fine. update_ui
  292.
- **Decals edge/halo cleanup + Redraw-to-vector button (v2.13.2).** User: final
  sheets have "pixel noise around the images" + residual scan lines. Diagnosed
  (on grey checker) = a faint semi-transparent CARRIER HALO hugging the art +
  bg speckle (the soft-alpha keying fringe), NOT interior noise. NEW
  `clean_matte(rgba, alpha_floor=70, despeckle=3)`: alpha<floor→0 (drop the
  halo), morphological open (kill speckle) + close (fill pinholes) on the ALPHA
  only — RGB/opaque-interior untouched, so pixel-faithful holds. Wired via
  `tidy_matte` (process_image, default True) + tab toggle "Clean up edges". Also
  destripe `z_thresh` 6→5 (catches fainter lines, still surgical + thin-run≤3;
  across sheets touches 0.2–4.5% of cols, streak-free untouched). NEW "🎨 Redraw
  to vector (SVG)" button = `_process_decals(force_mode="vector")` (mechanical
  vtracer trace of the user's OWN image — dual-use utility). IP: DECLINED an
  "AI redraw of the copyrighted decal art / LLM-gated reproduction" feature (the
  GI Joe/Cobra/commercial designs) — building a reproduction tool is off-limits;
  restoring the user's own scan + a general trace button are fine. update_ui 285.
- **Decals PIXEL-FAITHFUL restoration (v2.13.1).** User: "polish restoration,
  compare pixel by pixel, must be perfect" (after rejecting vector as colour-
  unfaithful; I declined a REDRAW of the copyrighted GI Joe/Cobra/commercial
  decal art — that's reproduction — restoration of their own scan is fine). NEW
  `exact=True` path in `process_image`: RGB left byte-identical (no denoise/
  unsharp/white-balance), only alpha + optional destripe change. Tab cleanup uses
  exact when auto-colour is OFF (default). BUG FIXED: white_balance neutralised
  the carrier tint BEFORE `_carrier_alpha` (which keys BY tint) → carrier not
  removed when auto-colour on; now the transparency MATTE is computed from the
  ORIGINAL tinted image and applied to the (optionally colour-processed) RGB, so
  keying works either way. DESTRIPE rewritten SURGICAL: robust z-score of per-
  column offset magnitude, keep only THIN runs (<=3px), interpolate each streak
  column from nearest clean neighbours; leaves a streak-free scan 100% untouched
  (no false positives — the old version nudged 58% of columns). VERIFIED on the
  real KW p2 sheet: opaque art pixels 100.0% byte-identical to the scan; destripe
  touched 37/2546 cols (1%); streak col 110->197. Degenerate perfectly-FLAT test
  fields explode the z-score (mad=0) — test on NOISY fields (real scans have
  noise). update_ui 279. `_final/sheets` regenerated exact.
- **DECALS tab — scan cleanup / vectorize to print-ready transparent art
  (v2.13.0).** User: digitize imperfect scanned GI Joe waterslide decal sheets
  (6 PDFs in `Stickers/`, all Fujitsu ScanSnap JPEG-in-PDF at ~300 DPI, flat
  cartoon art + text on a light-blue carrier film). NEW module `app/decals.py`
  (importable + CLI): `iter_source_images` (PDF via pymupdf → largest embedded
  image per page; + PNG/JPG/WEBP/BMP/TIFF), `detect_carrier` (median of a border
  frame), `_carrier_alpha` (keys the carrier by its TINT DIRECTION so faint WHITE
  ink — neutral — is kept while the tinted carrier goes transparent; the core
  trick, since white ink and a light carrier are near-equal in plain distance),
  `clean` (median denoise + unsharp), `remove_background`, `trim`, `vectorize`
  (vtracer color trace → magenta-key → pymupdf rasterize → re-key transparent),
  `process_image`/`process_source`. NEW "Decals" tab (5th left tab,
  `_build_decals_tab`): Add files/Clear + Listbox, method radio cleanup|vector,
  remove-bg + sensitivity(tol) slider, trim, output/scan DPI, Process → worker
  thread → queue msgs `decal_status`/`decal_add`/`decal_done` → gallery preview
  (composited on white) + transparent PNG(+SVG) saved to `output/decals`. Runs
  IN-PROCESS; pymupdf+vtracer BUNDLED via spec `collect_all` (VERIFIED in the
  frozen exe: `AIImageGeneratorSuite.exe --selftest-decals` →
  {'pymupdf':True,'vtracer':True,'cleanup':True}). LIMITS: faint white-on-light
  ink can't be separated (data limit — use keep-bg); home printers can't print
  white (white decal paper + cut). Deps added to venv: PyMuPDF 1.28.2, vtracer
  (cairosvg/svglib were dead ends — need native cairo on Windows; pymupdf renders
  SVG fine). PIPELINE ADDITIONS (after user quality feedback): `destripe()`
  removes vertical scanner streaks by subtracting each column's median offset
  from a horizontal moving-average baseline (clipped so bold art survives);
  `white_balance()` neutralises the carrier tint (gain = C.mean()/C per channel)
  so reds/whites are true (the cleanup was coming out dull/maroon otherwise);
  both are in `clean()` (flags remove_lines/balance), exposed as tab checkboxes.
  VECTORIZE upgraded for commercial output: median-smooth + MEDIANCUT quantize
  to flat colours before tracing, then STRIP the magenta key AND light blue-grey
  halo paths by parsing each `fill=` (the key traces to a FAMILY of near-magenta
  shades, not one hex) and render `alpha=True` → clean transparent edges + a
  transparent SVG. VERDICT (validated on the real sheets): vectorize = commercial
  grade for an ISOLATED logo (orca showcase), but WHOLE dense sheets trace with
  colour drift + white-ink halos → use CLEANUP+wb+destripe for whole sheets (true
  colour, readable text, transparent, de-streaked, 600 DPI) and vectorize
  per-logo. SCALE CONVERSION: `SCALE_PRESETS`/`scale_factor(src_n,tgt_n)` +
  From/To pickers + `size_scale` (3.75"/1/18 → Classified/1/12 = 1.5×). Final
  deliverable folder for the user: `Stickers/_final/` (sheets/ transparent 600dpi
  + logos/ orca SVG+PNG + README). update_ui 276.
- **Qwen/Kontext dropped as face-swap methods (v2.12.0).** USER: Qwen/Flux
  Kontext swaps gave "a completely different face" + very slow prep; insightface
  "Face swap" was much closer. ROOT CAUSE (not a bug): Qwen/Kontext are diffusion
  EDITORS that re-render the head from a prompt+ref (re-imagine, not swap) — poor
  identity, esp. at fp8; and they load an 11–28 GB model first (minutes off the H:
  HDD). insightface warps the actual identity (~0.88–0.91 sim), tiny model, fast.
  FIX: `CLONE_METHODS` reduced to one entry `("Face swap","faceswap")`; the Method
  dropdown UI (cmrow/`clone_method_dd`) removed (no `clone_method_dd` widget now);
  `clone_method_var` kept for persistence/recipe (always faceswap). `_clone_method`
  still returns faceswap. Old saved configs with a Qwen/Kontext label fall back to
  faceswap (`_apply_ui_state` guards `cm in CLONE_METHODS`). Qwen/Kontext graph
  builders + `_swap_face_pass` editor branches KEPT (Edit-image tab still uses them;
  swap_test still exercises the two-step pass via a fake) — just unreachable from
  the swap tool. update_ui 250.
- **Optimized delete + tag-safe generation (v2.11.1).** DELETE PERF: `_delete_paths`
  called `_rebuild_gallery` which destroyed EVERY thumbnail button and re-rendered
  all (LANCZOS per image) on each delete — O(N) per delete. Now thumbnail buttons
  store `btn._path` and resolve their index LIVE via new `_thumb_index(btn)`
  (`_thumb_btns.index`) instead of capturing idx in the closure, so `_add_thumb`'s
  command/binds never need rebinding. New `_prune_thumbs(goneset)` destroys ONLY
  the deleted buttons + filters `_thumb_btns` (no re-render); `_delete_paths` calls
  it instead of `_rebuild_gallery` (which is now unused but kept). `_select`/
  `_thumb_to_animator` guard a None index. TAG-SAFE GEN: the `finished_image`
  handler set `self.current = newest` + `_show_current` on every generation,
  stealing the selection mid-tagging (risk: a new image slips into a delete). Now
  it only moves the selection when `not self.tagged`; `_add_thumb` also skips the
  auto-scroll-to-newest while tagging. VERIFIED: delete of a middle image = 0
  `_add_thumb` calls, survivors re-index, click selects right image; new image
  while tagging leaves current unchanged, without tagging it auto-selects.
  update_ui 242→250.
- **Hi-res fix REMOVED (v2.11.0).** User: "remove the Hi-res fix" (after the
  v2.9.0 denoise fix still didn't satisfy). Fully deleted: the build_graph
  block (nodes 60/61 LatentUpscaleBy+KSampler), `hires_var`/`hires_scale_var`
  widgets (QUALITY qrow — FreeU re-gridded to col0, Upscale 4× to col1), the
  `hires`/`hires_scale` keys in `_collect_ui_state`/`_apply_ui_state`, the two
  vars from `_wire_autosave`, and `hires=`/`hires_scale=` from the `_generate`
  params. build_graph now ignores a stale `p.get("hires")` (old saved recipes/
  clones with hires=True load fine — the flag is a no-op; VERIFIED in tests).
  Kept detail paths: Anatomy guard (native→upscale) + Upscale 4×. update_ui
  241→242 (removal checks: no toggle, one KSampler, stale flag ignored).
- **QoL batch: window/sash persistence, batch ETA, keep-resident, threaded
  rebuild (v2.10.0).** WINDOW STATE: `_collect_window_state()` (geometry or
  zoomed + `sash_h`/`sash_v`) saved in `settings["window"]` by `_persist`;
  `__init__` restores geometry after `_load_settings`; `_init_sashes` restores
  the saved sashes (clamped) else the computed defaults. BATCH ETA: module
  `_fmt_duration(secs)` ('45s'/'4m'/'1m 30s'/'1h 1m'); Generator batch loop
  tracks `t_batch`/`done_ct` and appends "~<eta> left" to the per-image status.
  KEEP MODEL RESIDENT: `keep_resident_var` checkbox in QUALITY (arow col1) →
  `_on_keep_resident` writes `settings["prefs"]["keep_model_resident"]`;
  `start_engine()` reads SETTINGS_FILE prefs and appends `--highvram` (module
  fn, no App instance). Off by default (shared 5090). Needs restart. THREADED
  REBUILD: `_rebuild_history` now spawns a worker that `Image.open().load()` +
  `_params_from_png` (both Tk-free) OFF the UI thread and posts `rebuild_add`
  per image + `rebuild_done`; `_handle_msg` makes the Tk thumbnail on the UI
  thread (same pattern as generation). `_rebuilding` guard blocks re-entry.
  VALIDATED LIVE (scratchpad rebuild_test.py, real Tk, 6 PNGs): grew by 6,
  6 thumbs, idempotent, no freeze. update_ui 234→241.
- **Variations→100, hi-res denoise fix, preview debounce (v2.9.0).**
  VARIATIONS: `batch_sb` max 10→100 (stored as `self.batch_sb`); the batch loop
  already generates in turn with Stop/keep-done. HI-RES FIX (user: "hi-res makes
  pictures much worse, fix or remove"): the second-pass KSampler (node 61) ran
  at denoise 0.45 — regenerates ~45% of the upscaled latent → changed faces /
  extra limbs, worst when STACKED on the anatomy guard's own upscale+resample
  (nodes 62/63) = a third sampling. Now `hires_den = p.get("hires_denoise") or
  (0.30 if anatomy_guard else 0.35)`. VALIDATED LIVE A/B (dev 8189, Juggernaut,
  same seed, scratchpad ab_hires.py): base 832×1216, hi-res 1248×1824; mean
  deviation from base OLD 0.45=14.39 vs NEW 0.35=13.01 (new hallucinates less)
  — both clean on a simple standing pose (the gross artifacts are pose/hands/
  stacking-dependent, not every seed), so KEPT the feature with the gentler
  value rather than removing it. PREVIEW DEBOUNCE: `_draw_frame` LANCZOS-resizes
  the full-res preview on every canvas <Configure>; dragging the new paned
  sashes fired a storm of them → lag. New `_on_canvas_configure` coalesces via
  `after(60, _show_current)` (cancels the prior; `_show_current` clears
  `_redraw_after`). MODEL RELOAD (user ask "don't force reload if same"):
  confirmed the app forces an unload ONLY at the swap's `/free unload_models`
  (kontext/qwen path, off by default now); ComfyUI's exec cache already reuses
  the resident checkpoint when the graph's loader node is unchanged, so
  normal/recipe-replay gen does NOT reload — the reloads the user saw were the
  swap `/free` + the shared 5090 evicting the model. Did NOT add `--highvram`
  (would fight the 24GB of other resident AI work). update_ui 229→234.
- **Resizable panels + Cloning UI trim (v2.8.1).** RESIZABLE PANELS: the
  root layout was a fixed 2-column grid (col0 left_wrap minsize 450, col1
  right). Now `self.main_paned` = `ttk.PanedWindow(root, orient=horizontal)`
  holding `left_wrap` (weight 0) + `right` (weight 1); inside `right`,
  `self.right_paned` = vertical PanedWindow holding `preview_wrap` (canvas +
  incog_cover, weight 4) + `lower` (brow + gwrap, weight 1). Incognito still
  works because canvas/incog_cover/gwrap keep their own grid cells inside the
  new pane frames (grid_remove/grid unchanged). `_init_sashes()` (deferred
  `after(300)`) sets sane starting positions (left ~470, preview = h-190),
  clamped; validated at 1400×900 → main sash 470, right sash 684. CLONING
  TRIM (user: "remove Create more + Make, Generate handles it"): removed the
  Cloning `vrow`'s "Make" label + `var_count_var` Spinbox + section "Create
  more" button — the dropdown is now the whole row. `_create_more` no longer
  reads var_count_var / sets batch_var; it just calls `_generate()` (the
  generate page's "Variations" = `batch_var` is the count). Gallery `brow`
  "Create more" kept (rewired to batch_var). GOTCHA: don't screenshot a test
  window with ImageGrab at fixed coords while the user's app is open — it
  grabbed THEIR window (overlapping at the same screen coords), showing the
  old Make/Create-more and a false alarm; the headless update_ui suite (real
  App, withdrawn window, no mutex/engine — mutex lives in __main__, not
  App.__init__) is the authority. update_ui 223->229.
- **Cloning = recipe-replay by default + optional face-lock; full-res swap
  (v2.8.0).** USER INSIGHT: the original person was made by a plain batch (the
  "Variations" count = `batch_var`, new seeds, one recipe) — NOT a face swap —
  so replaying that exact recipe reproduces her at full quality with no extra
  step. Cloning now does that by default. NEW `self.clone_lock_face_var`
  (BooleanVar, default False) + `clone_lock_cb` checkbox in the Cloning body
  (row 3): OFF = pure recipe replay (no swap); ON = ALSO run the insightface
  swap for a hard face-lock. `_swap_active()` and the Cloning branch of
  `_swap_face_source()` now require `clone_lock_face_var` on (so Cloning-off =
  no swap = normal generation with the loaded recipe). `_apply_variation` no
  longer force-switches to SDXL or injects `_variation_ref` as IP-Adapter for
  replay — those happen only when the lock is on, via new `_ensure_clone_model()`
  (SDXL switch gated on lock) and the `_variation_ref`→rag_refs block now gated
  on lock. New `_on_clone_lock_toggle()` (switches SDXL model + status when the
  toggle flips). `_create_more` gallery-transient branch sets `clone_lock_face_var
  =True` (no saved recipe to replay for an ad-hoc image, so lock its face).
  Generate already makes clones via `_swap_active`/loaded recipe. FULL-RES SWAP
  FIX: `_generate`'s pending_swaps loop passed `out_size=(canvas)` to
  `_swap_face_pass`, which resized the (possibly hi-res) base DOWN and set
  `upscale=False` — the clone/swap lost the detail pass. Now `sw_out = None` for
  the `faceswap` editor (local insightface only reworks the face region, keep
  full res) and canvas size only for kontext/qwen (they redraw the whole image).
  VALIDATED LIVE (dev engine 8189, Juggernaut-XL): real portrait → swap rc=0,
  output stayed 1248×1824 (was downscaling to 832×1216); scratchpad
  faceswap_validate.py. FACE-SWAP-BASE BUG (user report): a face-ONLY photo in
  plain Face Swap made the render RECREATE the headshot instead of drawing the
  prompt's scene, because `_generate` added `swap_face` to `rag_refs` (IP-Adapter
  "PLUS high strength") to "draw the base with the person in mind" — fine for
  Cloning (ref = whole saved scene) but wrong for a headshot (IP-Adapter
  reproduces the headshot). FIX: new `_swap_guides_base()` (True only for a
  Cloning face-lock: var_enable+variation_sel+clone_lock) now gates that
  injection; plain Face Swap draws the base from the PROMPT ALONE, then swaps.
  Cloning didn't have the bug because its ref is a full scene. `clone_lock_face` is NOT persisted in `_collect_ui_state`
  (a live session toggle, not part of the saved recipe). SAVE-CLONE NAMING:
  `_save_variation` now `simpledialog.askstring` (prefilled with the auto-name;
  Cancel aborts) — reverses the earlier "no popup" since the user asked to name
  clones. update_ui 216->219 (recipe-replay-no-swap + face-lock-on checks).
- **Clone image decoupled into the Cloning section (v2.7.9).** USER BUG:
  "when selecting a clone, the image goes into Face Swap — it should go into
  its own Using section." Root cause: `_apply_variation` set `self.face_paths =
  [row["face_path"]]` + `face_source_var="file"`, which fed the clone's photo
  into the Face Swap section's state and its `face_using` strip (via
  `_refresh_face_list`). FIX: added `self._variation_face` (the clone's face,
  held apart from `face_paths`) + a `clone_preview_lab`/`clone_preview_name`
  "Using:" row inside the Cloning body (`vurow`, row 2 of `vb`), fed by
  `_update_clone_preview(path)` (keeps a `self._clone_preview_img` ref so Tk
  doesn't GC it; prefers `ref_path`, falls back to `face_path`). `_apply_variation`
  now sets `_variation_face`/`_variation_ref` + calls `_update_clone_preview`,
  and FORCES the Face Swap section empty/off (`face_paths=[]`, `swap_rag=False`,
  `_refresh_face_list`) so the clone never lands there. `_swap_face_source()`
  returns `[self._variation_face]` first when Cloning is active (var_enable +
  variation_sel + _variation_face) — so the swap still runs off the clone's
  face with the Face Swap checkbox OFF. `_on_variation_toggle` (off),
  `_on_clone_toggle`, `_on_variation_pick` (none), `_clear_variation`, and the
  `_create_more` gallery-transient branch all clear/set `_variation_face` +
  preview instead of touching `face_paths`. The transient "Create more" (no
  saved clone picked) now builds a transient `variation_sel` dict + enables
  Cloning rather than turning on Face Swap. `_drop_variation_face` is now dead
  (kept as a harmless legacy clearer). update_ui 216 (added 8 decoupling
  checks: clone face not in `face_paths`, `_swap_face_source`==clone face,
  Face Swap strip doesn't show the clone, off clears preview).
- **Rename + mutual exclusion + Face Swap independence + rebuild history
  (v2.7.8).** RENAMES (labels only, internal var names unchanged): "CLONE TOOL"
  section -> "FACE SWAP", `swap_cb` text -> "Swap this face…"; "VARIATIONS"
  section -> "CLONING", `var_cb` -> "Use a saved clone", gallery "Save
  Variation" -> "Save Clone", VAR_NONE -> "— no clone —". MUTUAL EXCLUSION:
  Face Swap (`swap_rag_var`) and Cloning (`var_enable_var`) can't both be on —
  `_on_clone_toggle` turns Cloning off (+ drops the clone face), `_on_variation_toggle`
  turns Face Swap off. Since Cloning drives the swap internally, added
  `_swap_active()` = `swap_rag_var OR (var_enable and variation_sel)`; the swap
  trigger in `_generate` and `swap_mode` now use it (so Cloning swaps with the
  Face Swap checkbox OFF). `_apply_variation` NO LONGER sets swap_rag on, and
  FORCES swap_rag OFF after `_apply_ui_state` (the restored recipe carried
  swap_rag=on -> was re-enabling Face Swap). FIX (user bug): `_apply_variation_lock`
  used to grey the Face Swap Browse/✕/model/method whenever a clone was active
  (merged-design leftover) — neutered to a near-no-op (just un-greys model_dd);
  Face Swap body is governed only by its own `_apply_clone_enabled`. Carried-over
  face fixed via `_face_from_variation` flag + `_drop_variation_face()` (clears
  a clone-sourced face on section switch; a user's manual `_pick_face` sets the
  flag False so it's kept). NEW "↻ Rebuild from pictures" button (gbtns, above
  Clear history) -> `_rebuild_history`: loads newest 200 OUTPUT/*.png into the
  session (dedup by path), params via `_params_from_png` (reads the PNG's
  "comic_art_creator" JSON + "parameters" text; defaults model="image" so
  `_show_current` doesn't KeyError on foreign PNGs). update_ui 208.
- **Variations promoted to its own section (v2.7.7).** Split out of the Clone
  Tool into a standalone "VARIATIONS (optional)" section with its own
  `var_enable_var` checkbox (OFF by default) + a `variation_body` frame greyed
  by `_apply_variation_enabled` (mirrors `_apply_clone_enabled`) until ticked.
  Body holds only variation controls: the `variation_dd` dropdown, Make count +
  Create more, Delete/Export/Import. `_on_variation_toggle` calls
  `_apply_variation_enabled` first; `_on_variation_pick` no longer toggles the
  enable (the checkbox owns enable/grey, the dropdown just selects — none clears
  the lock but keeps the section on); `_apply_variation` sets var_enable on +
  ungreys so a gallery Create-more keeps the section consistent. Clone header
  reverted to "CLONE TOOL (optional)". update_ui 205.
- **Engine-boot false alarm + SD3.5 diffusers dep + Create-more visibility
  (v2.7.6).** From a user error-log: engine.log showed the SD3 InstantX node
  failing with `ModuleNotFoundError: No module named 'diffusers'` — the embedded
  engine python lacks diffusers (dev happened to have it). `_ensure_sd3_support`
  now `pip install diffusers` into `engine_python()` when the node is present
  but the import fails (returns needs-restart). The recurring "engine stopped
  while starting" was a FALSE ALARM: `_boot_engine` sets crashed when
  `proc.poll() is not None`, and the engine was crashing transiently on the
  first start (VRAM clearing / init) then recovering ("Engine ready" ~26s
  later). Now `_boot_engine` auto-retries up to 2× on a start crash before
  showing the error (guard `_boot_retries`, reset on a start that gets
  through). NB the engine reaches "To see the GUI"/serves even with the SD3
  node import failing (non-fatal warning), so diffusers wasn't the crash cause
  — the crash is transient. Create-more button was packed after 6 others in the
  gallery `brow` and overflowed off-screen; moved it + Save Variation to the
  FRONT (leftmost) so both are always visible. update_ui 202.
- **Variations UI simplified + seamless recall (v2.7.5).** Gallery "More of
  this person" -> two buttons: `_save_variation` (now NO simpledialog — auto-
  named via `_auto_variation_name` from the prompt) and `_create_more`
  (generates `var_count_var` images of the selected person: applies the picked
  variation, or locks the selected gallery image transiently if none picked;
  sets batch_var + `_generate()`). The popup picker `_pick_variation` (Toplevel)
  is DELETED — replaced by an inline `variation_dd` Combobox (`_refresh_variation_dd`
  builds `_variation_by_label`; `_on_variation_pick` locks/clears). Inline
  `_delete_variation` (no confirm popup) + Export/Import now status-message (no
  messagebox). Header renamed "CLONE TOOL & VARIATIONS". SEAMLESS RECALL:
  `_apply_ui_state` no longer re-parses the RAG map when the config's
  ragmap_path is already loaded + unchanged (sig match) — recalling a variation
  whose big map is already open is instant (was a forced `_load_ragmap_async`).
  Tests: update_ui 202. NB simpledialog import now unused (left in place).
- **Pinned Generate bar (v2.7.4).** Moved the image Generate (`go_btn`) + `+Q`
  + progress + Cancel out of the scrolling `_page_gen` into a `self._gen_pinned`
  frame at row 0 of `_page_bottom` (the always-visible zone), above the batch
  queue — so you never scroll to run. `status_var` label sits at
  `_page_bottom` row 1 (outside the pinned frame) so status shows on ALL tabs;
  the batch queue starts at row 2. `_on_left_tab` (bound to
  `<<NotebookTabChanged>>`, add="+") grid_removes `_gen_pinned` off the
  Image-generation tab (index 0) — Animation/Borders/Edit keep their own
  generate buttons. The old `_gen_row` placement is dead (left set, unused).
  Tests: update_ui 197.
- **Anatomy negative embedding (v2.7.3).** The deferred piece from v2.7.1.
  `ANATOMY_EMBED = "negativeXL_D"` — a trained SDXL negative TI from
  `gsdf/CounterfeitXL` (VERIFIED: HF resolve URL 200, 128KB, keys clip_g
  (16,1280)+clip_l (16,768) = real SDXL dual-encoder). Wiring: (1) manifest
  entry {repo gsdf/CounterfeitXL, remote_file embeddings/negativeXL_D.safetensors
  (subpath works via resolve/main/<remote_file>), local negativeXL_D.safetensors,
  dir embeddings} — one-time ~130KB download; (2) start_engine yaml gained an
  `embeddings: embeddings` line (ComfyUI had no embeddings path before); (3)
  `_generate` prepends `embedding:negativeXL_D, ` to the negative ONLY when
  anatomy_on AND MODELS/embeddings/negativeXL_D.safetensors exists (so it's
  graceful without the download). VERIFIED live: a gen with
  `embedding:negativeXL_D` in the negative completed with NO "embedding does
  not exist" warning in the engine log (ComfyUI warns loudly when missing), so
  it loaded. NB the default ComfyUI embeddings dir is ComfyUI/models/embeddings
  (used on the plain dev launch); the app's yaml points embeddings ->
  MODELS/embeddings. Tests: update_ui 194 (+3: constant + manifest entry
  well-formed).
- **Incognito toggle (v2.7.2).** Eye button in the top-right badge row
  (`incog_btn`, next to lora/rag badges). `_toggle_incognito` grid_removes the
  preview `self.canvas` + the gallery `self._gwrap` and grids `_incog_cover`
  (a "🙈 images hidden" label over row 1); toggling back restores + repaints.
  `_show_current` early-returns while `incognito_var` is on. The icon is drawn
  with PIL (`_eye_icon`): open = ellipse outline + pupil; closed = a lid crease
  line + downward arc + 3 lashes (variant chosen by eyeballing renders). Icon
  colour = FG at build time (a mid-session theme switch keeps the build colour;
  minor). Session-only (not persisted; defaults OFF/shown each launch).
  Tests: update_ui +7 (191).
- **Anatomy guard + red Delete-all (v2.7.1).** `anatomy_var` checkbox in the
  QUALITY section. When on (SDXL only, not editing/border/img2img): `_generate`
  appends `ANATOMY_NEG` to the negative and sets `p["anatomy_guard"]`;
  build_graph draws the EmptyLatentImage base at `ANATOMY_NATIVE_MAX`=1152 (max
  dim, /8-rounded) then `LatentUpscale` (node 62) to the requested (w,h) + a
  refine KSampler (node 63, steps*0.5, denoise 0.45), and the hi-res block (if
  also on) chains off `sampler_out` after it. VALIDATED at the same seed: a
  1408 base grew a duplicated floating pair of legs; guard on (native 1152 base
  -> upscaled) = one clean figure, correct hands. Root cause of extra limbs =
  base bigger than SDXL's ~1024 training size. `Danger.TButton` red style
  added; the existing "Delete art files" button (delfiles_btn ->
  `_delete_history_files`, deletes ALL of OUTPUT+RAW_OUT with a red-text
  confirm) is now styled red + relabelled "🗑 DELETE ALL art files" (its
  confirm dialog already asked for Danger.TButton, which didn't exist until
  now). Tests: update_ui 184 (+5).
- **Variations — save/recall a person (v2.7.0).** New subsystem
  `app/variations_db.py` (`VariationsDB`): a per-install store at
  `<project>/variations/` = SQLite index `variations.db` + copied image files
  under `images/` (never referenced in place). A Variation row = name,
  description, face_path, ref_path, `config` (JSON = `_collect_ui_state()`, the
  generation recipe), seed. API: add/list/get/delete/`config_of`/export_zip/
  import_zip (portable across installs; import copies images in under fresh
  `secrets.token_hex` names so a same-second burst can't collide; zip-slip
  guarded). UI: a "🧬 More of this person" button in the gallery `brow`
  (`_save_variation` — saves the SELECTED session image + current
  `_collect_ui_state()` + that image's seed + a description) and a **Variations
  section** in the Clone Tool with its OWN enable checkbox `var_enable_var`
  (page-level, NOT inside clone_body, so it isn't greyed by the clone toggle).
  `_apply_variation`: `_apply_ui_state(config_of(row))` restores the recipe,
  then LOCKS identity — swap on, method=faceswap (CLONE_METHODS[0][0]),
  face_paths=[face], random_seed ON (new pose each run), `_variation_ref` =
  the full image fed as an SDXL IP-Adapter ref in `_generate` (appended to
  rag_refs, SDXL-only), and switches to `_best_sdxl_model()` if the model is
  flux/sd3. `_apply_variation_lock` greys model_dd + clone_method_dd + the face
  source rows. GOTCHA (fixed): `_refresh_editor_state`->`_apply_clone_enabled`
  ungreys clone_body children, so it used to un-grey the locked method; fix =
  `_apply_clone_enabled` re-asserts `_apply_variation_lock()` at its end so the
  lock always wins regardless of call order. Variation state is SESSION-ONLY
  (not persisted to settings) by design. Tests: variations_test 18 (Lodestone
  census: add/list/get/delete-removes-files/unique-names/export-import/
  zip-slip), update_ui +11 (179). Headless recall path verified end to end.
  NB the "generate more of the SAME person across new seeds" only holds because
  the FACE SWAP locks identity — prompt+seed alone give different people.
- **RAG artifact fix + preset auto-balance + SD3.5 self-heal (v2.6.1).**
  (1) IP-Adapter `end_at` was 1.0, so an embeds map's source photos dragged
  their baked-in lens-flares/watermarks into the final image (glaring after the
  hi-res pass sharpened them). Capped at a module const `IPA_END_AT = 0.6`
  (both the IPAdapterEmbeds and IPAdapter image nodes) — VALIDATED on the user's
  real embeds: flare/watermark gone, RAG steer kept (diff still ~70). Confirmed
  it's a RAG artifact not hi-res: plain(no-embeds)+hires was CLEAN.
  (2) Style-preset↔RAG auto-balance: in `_generate`, `style_lead = preset
  active AND (rag_refs or rag_embed_paths)`; when set, `rag_weight *= 0.5` and
  `ipa_end = 0.35` (passed as `p["ipa_end"]`, build_graph uses
  `p.get("ipa_end", IPA_END_AT)`). VALIDATED: Noir+LoRA+RAG at 0.4/0.35 renders
  genuine B&W noir while keeping the reference's composition (at 0.8/1.0 it was
  full-colour, preset ignored). The preset itself was never broken — image RAG
  (colour photos) + photoreal LoRA simply overrode the B&W *text*; alone the
  Noir preset renders perfect B&W.
  (3) SD3.5 self-heal: the InstantX adapter copy to ENGINE_DIR/models/ipadapter
  (the node hardcodes that path) + node install were only done in boot-time
  autoheal (once per session, gated on a sd3 checkpoint being present). The
  ~15GB SD3.5 checkpoint finishes downloading AFTER boot, so autoheal saw no
  checkpoint and skipped it -> `sd3_ipa_ready()` stayed False -> SD3.5 RAG
  silently off. Factored into `_ensure_sd3_support()` (returns needs-restart),
  called from autoheal AND the post-download update block. NB SD3.5 CANNOT use
  embeds-only maps (its adapter needs a SigLIP image, not SDXL CLIP-H embeds);
  `_refresh_mode_badges` now reds the RAG badge for fam=="sd3" + `_embeds_only`
  (`rag_dead`) instead of a false green.
  (4) Two blue `_rule()` dividers added above the Generate button and above the
  Batch queue. Tests: update_ui 168.
  NB dev==H: engine is byte-identical (ComfyUI 0.34.0, torch 2.11, IPAdapter
  node) so build_graph output reproduces faithfully on dev 8189 — the whole
  RAG investigation ran there with the user's Juggernaut+LoRA+embeds copied over.
- **SD3.5 Large image-guided RAG via InstantX IP-Adapter (v2.6.0).** The third
  family that can now steer on a RAG map's IMAGES (SDXL=IP-Adapter,
  Flux=Redux, SD3.5=InstantX). `model_family()` returns `"sd3"` for any name
  containing sd3/sd35 (checked FIRST, before flux/sdxl). `FAMILY_DEFAULTS["sd3"]`
  = steps 25, cfg 4.5, euler/normal. `build_sd3_graph(p)` is a SEPARATE builder
  (build_graph returns it early for fam=="sd3"): `CheckpointLoaderSimple`
  (all-in-one fp8 checkpoint — uses its `["1",1]` CLIP output DIRECTLY; do NOT
  add TripleCLIPLoader, the checkpoint carries all three text encoders) ->
  CLIPTextEncode pos/neg -> `EmptySD3LatentImage` -> KSampler(euler/normal) ->
  VAEDecode -> SaveImage. RAG block (when style_imgs present and adapter+siglip
  there): `IPAdapterSD3Loader`(SD3_IPA, "cuda") + `CLIPVisionLoader`(REDUX_SIGLIP,
  reused from Redux) + LoadImage(style_imgs[0]) + CLIPVisionEncode(crop=center)
  + `ApplyIPAdapterSD3`(model, ipadapter, image_embed, weight=min(style_weight,
  0.7), start=0, end=1). Only ONE ref image (SD3 node takes a single embed).
  CONSTANTS: `SD3_IPA="ip_sd35l_instantx.bin"`, `SD3_NODE_DIR=
  "ComfyUI-InstantX-IPAdapter-SD3"`, `SD3_NODE_ZIP=` Slickytail fork main.zip.
  `sd3_ipa_ready()` checks ENGINE_DIR/models/ipadapter/SD3_IPA +
  MODELS/clip_vision/REDUX_SIGLIP + ENGINE_DIR/custom_nodes/SD3_NODE_DIR.
  GOTCHAS: (1) the node is UNOFFICIAL (`ComfyUI-InstantX-IPAdapter-SD3`,
  Slickytail fork) — installed by `_autoheal_addons` via
  `engine_files.install_node_zip` at UPDATE time (only if a sd3 checkpoint is
  present), engine restarts after. (2) `IPAdapterSD3Loader` HARDCODES
  `folder_paths.models_dir/ipadapter` for the .bin — the manifest downloads it
  to MODELS/ipadapter, and autoheal ALSO copies it to
  ENGINE_DIR/models/ipadapter so the node finds it (WinError 2 otherwise). (3)
  the checkpoint is ALL-IN-ONE: an earlier TripleCLIPLoader attempt couldn't see
  the 3 separate text encoders (folder mapping) — dropped both the loader and
  the 3 encoder manifest entries. Manifest: sd3.5_large_fp8_scaled.safetensors
  (checkpoint, ~15 GB) + ip_sd35l_instantx.bin (repo
  InstantX/SD3.5-Large-IP-Adapter, remote_file ip-adapter.bin -> ipadapter dir,
  ~1.6 GB). VALIDATED live on 8189: plain-vs-IP-Adapter mean pixel diff 53.3,
  coherent output (red sports car carrying the reference's purple palette, 80s
  on the 5090). update_ui asserts model_family=="sd3", sd3_ipa_ready(), and
  build_graph has ApplyIPAdapterSD3+IPAdapterSD3Loader (not
  IPAdapterUnifiedLoader/StyleModelApply) (168).
- **Exe renamed ComicArtCreator.exe -> AIImageGeneratorSuite.exe (v2.4.0).**
  The one thing v2.0 deliberately DIDN'T rename (update safety). Done now via
  a migration that keeps existing installs updating: `self_update.APP_EXE` is
  now DYNAMIC = `Path(sys.executable).name` when frozen (else "ComicArtCreator.exe"
  in dev), so the updater always finds/installs/relaunches whatever exe is
  actually running. `install_staged` copies only the exe matching APP_EXE, so
  each install keeps its own filename. THE TRANSITION TRICK: every release zip
  ships BOTH `AIImageGeneratorSuite.exe` (the real build, from the spec's
  `name=`) AND a byte-identical copy named `ComicArtCreator.exe`, so an old
  install (hardcoded APP_EXE="ComicArtCreator.exe") still finds its exe in the
  zip and updates, while a new install finds the new name. Also renamed:
  version_app.txt InternalName/OriginalFilename, the spec `name=`, Setup's
  "Launch …" messages. The USER's own H: install was renamed in place (exe +
  shortcut) since they're the primary user (they chose "rename your install
  directly"). self_update_test unchanged (dev APP_EXE stays ComicArtCreator.exe
  so its hardcoded-name zips still match). Once old installs age out, drop the
  ComicArtCreator.exe copy from the zip.
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
