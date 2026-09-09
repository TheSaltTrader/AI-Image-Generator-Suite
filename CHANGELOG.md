# Changelog — AI Image Generator Suite

## v2.5.0 — 2026-09-08
- **RAG maps now guide Flux with images, not just captions.** Flux couldn't
  use a RAG map's reference images before — only its text. It now steers Flux
  generations through **Redux**, Flux's native image conditioning (the
  equivalent of what IP-Adapter does on SDXL), applied at a moderate strength
  so your prompt still leads. The two small Redux models (~575 MB total)
  install automatically with the model updates.
- Validated end to end on a real Flux run (the reference image measurably
  steers the picture).
- Note: an *embeds-only* RAG map has no viewable images, so Flux still applies
  those maps as text — Redux needs actual reference images to guide from.
- **Fixed a startup crash box** ("failed to execute script … application has
  been destroyed"). If a second copy launched while one was running — or during
  an update relaunch — the extra copy could throw an unhandled error instead of
  bowing out quietly. It now exits cleanly.

## v2.4.0 — 2026-09-08
- **The program file is now `AIImageGeneratorSuite.exe`,** matching the app's
  name. New installs get the new filename. Existing installs keep updating
  normally — the updater now acts on whatever the app is actually named, and
  releases carry both filenames during the changeover so nobody's auto-update
  breaks. (You'll only notice this if you look at the folder; the app is
  launched from its shortcut.)

## v2.3.2 — 2026-09-08
- **Fixed: VRAM not freed after closing the app.** The engine (which keeps
  the last model resident, up to ~17 GB) was only shut down when the app
  thought it "owned" it. After an update relaunched the app, that ownership
  no longer matched, so the engine — and its VRAM — kept running after every
  close. The app now always shuts down its own engine on close (still scoped
  to this install, so it never touches another program's ComfyUI).

## v2.3.1 — 2026-09-08
- **Fixed: generation failing with "engine rejected the request … IPAdapter".**
  When a RAG map's image guidance was rejected by the engine's IP-Adapter
  version, the whole generation failed (this broke the Clone Tool for every
  method, since each draws a base first). Now the app retries the picture
  once without the image guidance — your RAG LoRA and trigger word still
  steer it — so you always get an image, and it repairs the IP-Adapter
  add-on in the background.
- **Add-ons now set themselves up after an update, not on first use.** The
  face-swap engine for the Clone Tool installs automatically at start-up
  once (alongside the IP-Adapter add-on), so it's ready when you need it
  instead of interrupting your first swap.

## v2.3.0 — 2026-09-08
- **New QUALITY section** on the Image generation tab, grouping the
  extra-quality passes in one place (all off by default):
  - **Hi-res fix (add detail)** — a real second pass: the picture is
    upscaled in latent space and lightly re-sampled, so texture and detail
    are *added*, not just enlarged. The biggest single quality lever. Choose
    1.5× or 2×. (Skipped for edits and border jobs.)
  - **Extra detail (FreeU)** — a near-free contrast and fine-detail boost on
    SDXL models.
  - **Upscale 4×** — moved here; enlarges the finished image last of all.
- **Anime models get their detail from the enhancer too.** The ✨ built-in
  prompt enhancer now lightly emphasises your main subject so SDXL-family
  models stick to it (Flux, which ignores weighting, is left alone).
- Anime-family models already got clip skip 2 automatically in v2.2.1.

## v2.2.1 — 2026-09-08
- **Fixed: the face-swap setup got stuck in a loop.** Setting up the Clone
  Tool's face-swap engine failed right after installing and then asked to
  install again on every Generate. It now completes and the Clone Tool works.
  (The setup routine referenced an internal helper on the wrong object; the
  helper is now shared correctly.) While the setup is running, Generate now
  says "still setting up" instead of re-asking.
- **Anime models now use clip skip 2** automatically (Illustrious / NoobAI /
  Pony / Animagine families), which fixes their slightly-off colour and detail.

## v2.2.0 — 2026-09-08
- **New app icon** — an artist drawing (a pencil sketching on a canvas). It
  is the desktop shortcut, taskbar and window icon from this release on.
- **Security hardening (OWASP pass).** The download and install paths were
  re-reviewed against the OWASP Top 10 and tightened: the face-swap model is
  now verified against a pinned SHA-256 before it is used, the face-swap
  engine is pinned to a known version, the installer's archive extraction got
  the same anti-path-traversal guard the rest of the app already had, and
  model downloads are refused unless they are HTTPS.
- **Full validation run.** Added the Lodestone assessment kit (`kit/`): a
  census that runs each test suite and names what it could not check rather
  than averaging it into a score. This release: 7 suites pass (414 checks, 0
  failures); the live-engine, GPU and journey checks are named as not run.

## v2.1.0 — 2026-09-08
- **The Clone Tool actually looks like the person now.** The old face
  swap re-rendered the face with a huge editor model (Qwen/Kontext) that
  loaded for minutes off a hard drive — so in practice it was cancelled
  before it finished and you were left with the plain generated image
  (a face the model invented). The renamed **Clone Tool** now defaults to
  a real, local face-swap engine (insightface / inswapper): a one-time
  ~550 MB setup, then it runs in seconds and closely matches the person
  you chose.
- **Pick your Clone method.** A new **Method** dropdown in the Clone Tool
  lets you choose the engine: **Face swap** (fast, best likeness — the
  default), or the **Qwen** / **Flux Kontext** editors (re-render, slower,
  looser likeness). Each installs itself the first time you use it.
- **One finished image, not a faceless draft plus a broken swap.** With
  the Clone Tool on, Generate now produces a single final image — the
  scene with the chosen person's face already in it. If your model has
  IP-Adapter set up, the scene is also drawn with that person in mind
  before the face is locked in.
- Renamed the section from "Face / Character" to **Clone Tool**.

## v2.0.0 — 2026-09-08
- **New name: AI Image Generator Suite.** The app is now a general-purpose
  local image studio. The window title, installer and About all carry the
  new name, and the project moved to a new GitHub home. Existing installs
  update to v2.0 in place as usual — the updater and your settings carry
  over untouched (the program file on disk is still `ComicArtCreator.exe`
  internally, so nothing about your install path or shortcuts changes).
- **✨ Enhance now works out of the box.** The prompt enhancer used to need
  a local Ollama install, so with none running the dropdown was empty and
  the button did nothing useful. There is now a **Built-in (offline)**
  enhancer that always works: it keeps your words, folds in the style, and
  adds composition and quality phrasing suited to the model you picked. If
  you do run a local Ollama model you can still choose it for a smarter
  rewrite. The dropdown is never empty.
- **Face / Character "Using:" shows the actual images.** Instead of a text
  list, the Using area now shows small thumbnail samples of the face
  image(s) or the chosen person's kept photos, so you can see at a glance
  exactly what will be placed on the generated picture.
- **Clearer, faster engine start-up.** When the engine is starting (a first
  run, or right after an IP-Adapter or engine install) the status now shows
  how long it has been waiting instead of looking frozen. If the engine
  process actually stops while starting, the app now says so right away —
  with the last lines of `engine.log` — instead of waiting out the full
  timeout.

## v1.60.0 — 2026-09-08
- **A loaded Edit image no longer switches off LoRA and RAG.** Now that
  editing lives on its own **Edit image** tab, leaving an image loaded
  there used to quietly put the *whole* app into edit mode — so pressing
  **GENERATE** ran an edit and the LoRA/RAG badges went red even with a
  LoRA ticked and a RAG map ready. GENERATE now always generates with
  your preset, LoRAs and RAG map (and any face swap), whatever is sitting
  on the Edit tab. The Edit tab's **Apply edit** button is the only thing
  that edits the loaded image; pressing it with nothing loaded now asks
  you to load an image instead of doing nothing.

## v1.59.0 — 2026-09-08
- **Readable info pop-ups under the Daylight theme.** The hover tooltips
  (including the LoRA and RAG explanations) were pale text on a near-white
  box under Daylight and could not be read. Tooltips are now a soft cream
  background with dark text in Daylight, and light text on dark in the
  default theme — clear contrast in both.

## v1.58.0 — 2026-09-08
- **Cycle through a person's photos and drop the ones that don't fit.**
  When a database person has more than one picture, ◀ ▶ arrows appear
  by their thumbnail to step through the photos, and a **Drop** button
  removes the shown photo from what is sent to the model (press it again
  to add it back). Dropped photos are only excluded from this selection
  — nothing is ever deleted from your database. The "Using:" list shows
  how many of the photos will be sent, and your choices are remembered.

## v1.57.0 — 2026-09-08
- **A font choice in Settings, and the menu size now scales the whole
  left panel.** ⚙ Settings has a **Font** dropdown (Segoe UI, Arial,
  Calibri, Verdana, Tahoma, Georgia, Times New Roman, Comic Sans MS,
  Consolas). The **Menu size** setting scales every menu and the entire
  left panel. Apply changes on the spot, or Restart now to have the font
  and size take effect everywhere cleanly.

## v1.56.0 — 2026-09-08
- **"Edit a loaded image" is now its own tab.** A new **Edit image** tab
  holds the loaded-image editor, fully separate from the face swap on the
  Image generation tab, so the two never conflict. It has its own
  instruction box and an **Apply edit** button, plus a **Common edits**
  menu (also on right-click) with ready-made instructions — remove text,
  plain background, change background/outfit/colours, restyle, improve
  quality, fix hands — that fill the box for you.
- **The face-swap checkbox is now at the top of the Face / Character
  section**, and unticking it greys out the whole section.
- **Badges now explain themselves in the log.** Each time the LoRA/RAG
  badges refresh, app.log records whether each is green or red and why
  (no LoRA ticked, map still loading, model not SDXL, editing, or a swap
  setting) — so a "why is it red?" question can be answered from the log.

## v1.55.0 — 2026-09-08
- **A Daylight (light) theme, and a Settings menu.** A new **⚙ Settings**
  button at the bottom of the panel lets you switch between the **Dark**
  and **Daylight** themes and choose the **menu size** (Small, Normal,
  Large, Extra large) so the text and controls are as big as you like.
  Apply changes live, or Restart now to have them take effect cleanly.

## v1.54.0 — 2026-09-08
- **Fewer controls in the face/character section, as requested.** Under
  the swap there are now just two checkboxes — **LoRA** and **RAG** —
  which you can tick together, singly, or not at all to guide the base
  the swap draws. The separate quality toggle is gone (best quality is
  always used).
- **A "Using:" list** shows exactly which image or person the swap will
  place on the picture, whether you chose it from a file or a database.
- **The result always renders at the Canvas size**, so the "Output at
  Canvas size" option has been removed.

## v1.53.0 — 2026-09-08
- **The face/character section is simpler.** Instead of a tangle of load
  buttons, a reference database, a person picker and several checkboxes,
  it is now: pick a **Source — From a file or From a database** (one or
  the other), tick **Put this face on the generated image**, choose
  **Best or Fast** quality, and optionally **guide the base with the RAG
  map + LoRAs** with a single checkbox. Your prompt, model, LoRAs and RAG
  map draw the scene as usual, then the chosen face is placed on it.
- **Editing a loaded image is now its own clearly separate section**
  ("Edit a loaded image"), with its own Load button and a Change-amount
  slider — no longer mixed up with the face swap. A file chosen as a
  face and an image loaded for editing no longer collide.

## v1.52.0 — 2026-09-08
- **The loading bar no longer looks like it is racing.** While the engine
  loads a model the bar sweeps to show it is busy; that sweep was far too
  fast and read as a glitch, especially during the multi-minute face-swap
  model load. It now sweeps at a calm, normal pace.
- **Cancelling during a model load stops the sweep at once**, instead of
  leaving it running until the batch ends.
- **Validated the face swap end to end.** On a test engine a RAG map and
  LoRAs guided the base picture (confirmed different from an unguided
  one), and the Qwen swap then placed the chosen face on it while keeping
  the pose, costume, background and art style. The swap takes about two
  minutes because the 28 GB swap model loads from disk — on a hard
  drive it is slower still. If face swaps feel stuck, they are loading,
  not frozen; the Fast swap option avoids the big load, and moving the
  models folder to an SSD makes it far quicker.

## v1.51.0 — 2026-09-08
- **The update window asks before downloading.** At startup it opens on
  its own with the release notes and waits: **Download and install**,
  **Not now** (nothing downloaded; asked again next time), or **Skip
  this version**. Only after your yes does it download, verify and
  install — and then it asks once more before restarting (Restart now,
  or Later, and the new version starts the next time you open the app).
  Previously the download began the moment the window opened.

## v1.50.0 — 2026-09-08
- **A face swap on a RAG-guided base no longer comes back as the base.**
  After a swap, only the region that differs strongly from the base is
  merged back, so the rest keeps the base's exact pixels. The bar for
  "differs strongly" was relative to the picture's average change, and a
  photoreal base that the swap model re-renders all over pushed that bar
  above the face change itself — the merge found no head and returned
  the base, so the selected image looked unused. The bar is now capped,
  and when no head-sized region is found the swap itself is kept instead
  of the base. Each swap logs its numbers (average change, threshold,
  head share, which result was used).
- **Less VRAM pressure for the swap:** the engine's cache is freed as
  well as its models before the swap model loads.
- If a progress bar is ever found with stacked sweep timers again, the
  log says so.

## v1.49.0 — 2026-09-08
- **The progress bar no longer races on after a batch.** Each time a
  model load told the bar to sweep, the toolkit started another timer
  behind the scenes and its stop only cancelled the last one — after a
  batch of four the bar under Generate kept stepping, faster and faster,
  for as long as the app ran. Sweeps now start once, stop fully, and any
  stray timer is hunted down. The same guard covers the RAG loading bar
  and the loading strip above the tabs.

## v1.48.0 — 2026-09-08
- **A loading model no longer looks like a hang.** While the engine
  loads a model the progress bar now sweeps and the status counts the
  elapsed time; the real bar returns with the first step. Previously the
  bar sat at 100% from the last picture for the whole load, and the
  face-swap model (28 GB, read from disk) takes minutes on first use.
- **A swap batch loads the swap model once.** Every base picture is
  drawn first, then every swap — instead of base, swap, base, swap,
  which evicted and re-read the 28 GB swap model for each picture. The
  status says which picture is being swapped.
- **Under "Use RAG & LoRA for image swap", three checkmarks of their
  own:** *RAG map guides the base*, *LoRAs on the base*, and *Fast swap*
  (Flux Kontext instead of Qwen: no big reload because it fits next to
  the drawing model, at the cost of a likeness that may need a few
  Variations). The badges reflect the first two.

## v1.47.0 — 2026-09-08
- **You can always say "not this time" to an update.** The automatic
  update still downloads the new version on its own, but it now stops
  and asks before touching anything: **Install and restart**, **Not now**
  (nothing changes; you are asked again next time you open the app), or
  **Skip this version** (quiet until a newer one). Previously the
  automatic path installed first and only asked about the restart. The
  manual window's third button is now called Not now as well.
- **The mouse wheel over the left panel always scrolls the panel.** A
  dropdown used to change its value, and a text box or the LoRA list
  scrolled itself, when the wheel happened to be over them.
- **A loading strip above the tabs** lists what is still starting up —
  the engine, a RAG map (with its percentage), the update check, an
  add-on install — and disappears once everything is ready, so you know
  when to wait.

## v1.46.0 — 2026-09-08
- **Deleting a gallery image also removes it from wherever it was in
  use.** The image editor, the border maker's references and the
  animator all kept pointing at a picture after you deleted it from the
  gallery. With "Use RAG & LoRA for image swap" ticked, that meant the
  face for the swap was a file that no longer existed, so every swap was
  skipped with a one-line status and only the base picture appeared — the
  selected image looked as if it was never used. Now a deleted image is
  dropped from the editor, border and animator on the spot, and the
  status says so.
- **A swap whose face file is missing stops before generating** and
  tells you to load another image, instead of drawing a base and
  silently keeping it. The skipped-swap message is also logged with
  its reason.

## v1.45.0 — 2026-09-08
- **The RAG map has its own section**, right under the LoRAs on the
  Image generation tab, with its own heading and explanation instead of
  a row tucked into the LoRA controls.
- **A progress bar while a map loads.** A large map takes minutes; the
  bar shows the phase (reading the file, checking references with a
  percentage, reading the embeddings, building the word index) and
  disappears when the map is ready. Loading still runs in the
  background, so you can keep working.
- Note on variations: the images of one Generate share one set of
  references (one person, in different poses); the next Generate draws
  a fresh set. That is by design and unchanged.

## v1.44.0 — 2026-09-08
- **Tag several images and delete just those.** Ctrl+click or right-click
  a thumbnail (or select it and press **☑ Tag**) to tag it; tagged
  thumbnails wear a red frame. **🗑 Delete** then reads "Delete 3 tagged"
  and removes exactly those, from disk and from the gallery. With
  nothing tagged it deletes the selected image as before.
- **The delete confirmation opens right above the thumbnail it is
  about**, not wherever Windows drops a message box. Enter confirms,
  Escape cancels. The "Delete art files" confirmation now opens above
  its own button too.
- **A RAG map no longer gives you the same person every time.** For a
  given prompt the map used to return the same best-matching references
  on every Generate, so only the pose changed with the seed. The
  references are now drawn at random from the strongest matches, seeded
  like the image: a random seed brings a different person each time, a
  fixed seed brings the same references again.

## v1.43.0 — 2026-09-08
- **The left panel is now three tabs.** *Image generation* holds the
  prompt, model, presets, LoRAs, RAG map, reference database, canvas
  settings, the image editor and Generate; *Animation* holds the
  animator; *Borders* holds the border maker. Each tab scrolls on its
  own, so nothing is buried at the bottom of one long column. The batch
  queue and the version row stay under the tabs whichever one is open,
  and the app remembers which tab you had open.

## v1.42.0 — 2026-09-08
- **The copy started by an older version's update now restarts itself
  cleanly, always.** v1.41 recognised an inherited program folder only
  when it was more than a minute old; an update that completed in
  seventeen seconds slipped under that, and the new copy broke the same
  way (the "failed to remove temp directory" box is the old copy
  noticing). A copy started by any update from before this version now
  starts over on its own, in its own folder, before doing anything else.
- **The GPU badge has a blue bar** next to its percentage, matching the
  VRAM meter.

## v1.41.0 — 2026-09-08
- **The copy started after an update no longer breaks minutes later.**
  The program unpacks itself into a temporary folder when it starts. The
  copy launched by an update was inheriting the *previous* copy's folder
  instead of unpacking its own, and when the previous copy exited it
  deleted that folder from under the new one. Anything the app loaded
  later then failed: "cannot import name", "cannot find the path …
  numpy", a RAG map that could not be read, an update check that could
  not find its certificates. Now a copy the app starts always unpacks its
  own folder, and a copy that finds itself in someone else's folder
  restarts cleanly on its own. (The log made this findable — thank you.)
- **A GPU badge.** The top-right bar now shows how busy the GPU is
  (`GPU 37%`) next to the memory meter, which is now labelled **VRAM**.
- The start-up update check no longer dies silently when the network or
  certificate check fails; it says so in the status bar and the log.
- Scrolling the mouse wheel while a dropdown list is open no longer logs
  an error.

## v1.40.0 — 2026-09-08
- **Reopening the app right after closing it no longer asks about a
  second copy.** Since v1.37 the window disappears the moment you close
  it, while the program spends a few more seconds shutting the engine
  down; opening the app again in that gap met the "appears to be
  already running" question. A new copy now shows a small "waiting for
  the previous copy to finish closing" window and carries on by itself
  once the old one has gone. The question is only asked when a copy
  really is open — its window is on screen, or it did not close within
  twelve seconds.

## v1.39.0 — 2026-09-08
- **The app now keeps a log.** `app.log`, next to the program, records
  every message the status bar showed, every message box, every error
  the app reported and every exception it caught or failed to catch —
  including ones in background work and in window callbacks, which a
  windowed program otherwise loses without a trace — each with the
  time. A **📋 Log** button next to *Check for updates* opens it. When
  something goes wrong, send that file rather than describing the
  message from memory. The file is capped at a few megabytes.
- A failed self-update now also reports its reason in the status bar
  (and so in the log), not only inside the update window.

## v1.38.0 — 2026-09-08
- **Generate no longer pauses on a big RAG map.** Picking the example
  images for a prompt walked every entry of the map each time — on a map
  with 481,000 entries that was 5.5 seconds of frozen window per
  Generate. The map now builds a word index while it loads (in the
  background, as of v1.37), and retrieval looks up only the entries that
  share a word with your prompt. Same choices as before, measured
  identical across dozens of prompts, in a few milliseconds.

## v1.37.0 — 2026-09-08
- **No more "Not responding" while a RAG map loads.** A map can be
  hundreds of megabytes of JSON plus a multi-gigabyte embeddings file,
  and the app parsed it on the interface thread — at start-up, then
  again right after, and again on every model refresh. The window froze
  for as long as each parse took. Maps are now parsed in the background
  (the label reads "loading…" and you can keep working), an unchanged
  map is never parsed twice, and the words used for retrieval are worked
  out once at load time instead of on every Generate.
- **No more "appears to be already running" after an update.** The new
  version checked whether another copy was open while the copy that had
  just updated was still closing its engine, and asked whether to open
  another window. The new copy now waits for the old one to finish
  (it is told which one to wait for), and asks only if a genuinely
  separate copy is open.
- **Restarting after an update, and closing the app, no longer freeze
  the window.** Stopping the engine takes a few seconds; that now
  happens after the window has gone, not while it sits there greyed out.

## v1.36.0 — 2026-09-08
- **An engine update can no longer break the engine.** The old updater
  deleted the engine folder in place and put the fresh one down
  afterwards; if the delete stopped part-way — as it did on a real
  install — the engine was left as empty folders that could not start,
  and the same routine's clean-up then threw away the custom nodes it
  had set aside to keep. Now the fresh engine is downloaded, unpacked,
  checked and its packages installed *before* anything is touched; the
  live engine is then **renamed aside, never deleted**, your custom
  nodes, inputs and settings are copied into the new one, and only when
  all of that succeeded is the old copy removed. Any failure at any step
  leaves the engine you had exactly as it was, and says so.
- **A damaged engine repairs itself.** Before every start the app checks
  that the engine's core files are there; if an earlier update left it
  broken, the engine is rebuilt from the same version it recorded (your
  add-ons kept) and started. The same check runs from the engine log
  after a failed start, so a break the file check does not name is
  still caught.
- **"IP-Adapter install failed: Cannot create a file when that file
  already exists" is gone.** Every installer used one shared temporary
  folder and refused to run if a leftover in it would not delete — five
  stale files blocked RAG-map and Reference-DB set-up for weeks. Each
  run now gets its own fresh folder, a stuck leftover is stepped around
  and reported, and old folders are cleared at the next launch.
- **Two installers no longer trip over each other.** The start-up
  add-on check and the install offered when you load a RAG map could run
  at the same time; now one waits for the other.
- **If an engine update fails, the engine is restarted anyway.** It used
  to stay stopped until you relaunched the app.
- **Updates are now automatic.** When a newer release exists, the app
  downloads and installs it as it starts — and asks you only before
  **restarting**: Restart now, or Later, in which case the new version
  simply starts the next time you open the app. You keep working while
  it downloads. A box in the update window turns automatic updates off
  if you would rather be asked first (the Check for updates button
  always asks).
- **Validated end to end on a live engine:** LoRA-only, LoRA + RAG map
  images, LoRA + an embeddings-only map, both chained off one loader,
  and Flux + a Flux LoRA, each render fetched back and compared — the
  references demonstrably change the picture at a fixed seed.

## v1.35.0 — 2026-08-20
- **The app now shows you an update before it takes one.** When a newer
  release exists, a window opens with the new version number, the
  download size, the release date and the full "what's new" notes from
  GitHub — and three choices: **Update now**, **Skip this version**, or
  **Continue**. Nothing is downloaded until you press Update now, so you
  can read what changed and decide. Previously it was a bare yes/no box
  with no notes and no size.
- **Skip a version and it stays skipped.** The startup check goes quiet
  for that release and speaks up again only when there is a newer one.
- **A "Check for updates" button** now sits at the bottom of the left
  panel next to the version number, so you can look whenever you like —
  including for a version you skipped earlier. It tells you when you are
  already up to date instead of appearing to do nothing.
- **You can watch the download and stop it.** A progress bar shows MB
  downloaded of MB total, and Cancel stops it and leaves your current
  version exactly as it was.
- **Updates can no longer half-install.** The download is checked before
  anything is replaced: it must be a genuine Windows program reporting a
  version newer than the one you are running, so a damaged download or a
  mis-tagged release is refused rather than leaving you with an app that
  will not start. If the swap fails part way, your previous version is
  put back.
- **Updates now refresh the documentation and the model list**, not just
  the program. Those files used to stay frozen at whatever version first
  installed the app — which is how installs ended up not being offered
  models that had been added since. Your own `presets.json` and settings
  are still left alone.
- **Old copies of the app are cleaned up.** The previous version is kept
  aside during the swap (Windows will not overwrite a running program)
  and deleted at the next launch, instead of piling up forever.

## v1.34.0 — 2026-08-14
- **Fixed: engine updates were burying your add-ons.** Every engine
  update moved the preserved `custom_nodes`, `input` and `user` folders
  *inside* the new engine's own folders instead of merging with them, so
  each update pushed your content one level deeper. After enough updates
  the engine could no longer see the IP-Adapter add-on at all, and
  generations that use RAG maps or a person's photo failed with "Node
  'IPAdapterUnifiedLoader' not found". Restoring now merges properly, so
  this cannot happen again.
- **Existing installs repair themselves at startup.** The app now
  un-nests those folders every time it launches, putting your add-ons
  (and any input images that got buried) back where the engine looks,
  keeping the most recent copy. It says so in the status line when it
  fixes something, and does nothing once the folders are healthy.
- **Missing add-ons install themselves after the engine starts**, rather
  than failing mid-generation: if the IP-Adapter node still isn't there,
  the app fetches it and restarts the engine on its own.

## v1.33.0 — 2026-08-14
- **➡ To editor now loads all of a person's photos**, not just the first
  one — up to the editor's image limit (4 for Flux Kontext, 3 for Qwen),
  skipping any already loaded. The status line says how many went in and
  how many didn't fit, so the editor can see the person from several
  angles in one edit.
- **The browser's ◀ ▶ arrows now choose the picture.** Whichever photo
  is on screen when you hit "Use this person" becomes the one that gets
  used — for face swaps, for editor references and for plain-generation
  face guidance — and the others follow after it. The person's name
  shows which photo is in use ("· photo 2/3"), flipping the arrows for
  the person already selected re-picks straight away, and reopening the
  browser lands on the picture you chose. Your choice is remembered
  across restarts.

## v1.32.0 — 2026-08-13
- **The photo arrows stay put.** The picture now sits in a fixed-size
  box, so tall, wide or small photos no longer push the ◀ ▶ arrows and
  the details around — they stay exactly where you left them as you flip
  through a person's pictures.
- **Columns fill the window, and you can resize them.** The grid always
  spans the full panel and follows along as you resize the window or
  maximise it. Drag any column border in the header to make that column
  wider or narrower — the others give way, and the grid stays full-width
  (the cursor changes to a resize arrow when you're over a border).

## v1.31.0 — 2026-08-13
- **Row numbers.** The database browser has a # column showing each
  row's position in the current (searched + sorted) list, so you always
  know where you are while scrolling thousands of entries.
- **No more cut-off bottom row.** The table only ever draws rows that
  fit completely — the height math now asks the widget what actually
  fits instead of estimating, and the visible block always ends with a
  clean closed border (the old dangling divider looked like a clipped
  row even when nothing was cut).
- Full-list scrolling double-checked end to end: scrollbar, mouse wheel
  and page jumps reach the very last row of the largest databases.

## v1.30.0 — 2026-08-13
- **The database browser scrolls through the entire list now.** The
  1,500-row display limit is gone: the scrollbar spans the whole
  database — all 47,000 characters if that's what's loaded — and the
  grid renders the rows as you scroll (scrollbar drag, mouse wheel, or
  page jumps). Search and sorting still consider every row.

## v1.29.0 — 2026-08-13
- **The database browser has a real grid now.** Every cell is boxed in
  clean white lines, and the text is white on black (replacing the green
  terminal look) — much easier to scan. Same search, click-to-sort
  headers (with the ▲▼ marker), row highlight, photo arrows and buttons.
  Very large databases show the first 1,500 matches and tell you how
  many more the search can narrow down.

## v1.28.0 — 2026-08-13
- **The app now reads every database the DB-builder tools produce.**
  📇 Reference DB… accepts the comic-character databases
  (`dc_characters.sqlite` / `marvel_characters.sqlite`) alongside the
  person ones: the browser switches to character columns (Name, Real
  name, Type, Year, Appearances, Publisher — sorted famous-first), the
  detail pane shows the character's blurb, and the portrait feeds
  editing, IP-Adapter guidance and the face swap exactly like a person's
  headshot.
- **Multi-photo enrichment actually shows up.** The photo arrows now read
  the `actor_photo` / `character_photo` tables the enrichment tools
  write (the older `photo` table still works), with the primary
  headshot/portrait always first — so enriched databases browse and
  swap with every picture they hold.
- **Singer databases show their music.** A person database built with
  the singers tool displays genres, voice type, active years and the
  bio right in the browser's detail pane.
- **Reading while enriching.** Database reads now wait out brief write
  locks, so a database mid-enrichment can still be browsed; if an open
  is refused outright, close and reopen it a moment later.

## v1.27.0 — 2026-08-13
- **The Reference DB browser lives in the art panel now.** 👤 Person… no
  longer opens a popup: the browser appears where the picture preview is,
  and ✖ Close (or choosing a person) brings the art panel back exactly
  as it was. The table is green-on-black, terminal style, so the rows
  read clearly — same search box and click-to-sort columns as before.
- **Photo arrows.** ◀ ▶ under the person's picture flip through every
  photo the database holds for them (with a 1/N counter). Databases with
  one headshot per person work as before; when your actor database gains
  multiple photos per person they browse — and feed the face swap —
  automatically.

## v1.26.0 — 2026-08-12
- **Detail-preserving swap.** The swapped picture used to be a full
  re-render of the base — softer reflections, flatter sunlight, lost
  micro-detail. Now the swap result is merged back onto the base: only
  the genuinely changed region (the new head, and anything else the swap
  really altered) comes from the swap; everywhere else you get the
  base's exact original pixels. Measured on a photoreal test scene, the
  final image deviates from the base ~9× less than before, with the
  face transfer at full strength.
- **Style-aware swap wording.** The swap instruction no longer forbids
  photographic rendering outright — it now matches the base: a
  photorealistic base keeps a photorealistic face, a stylized base keeps
  a stylized one.
- **Multiple face photos.** Load several pictures of the same person
  (🖼 Load… multi-select) and the swap uses up to two of them together
  for a sharper likeness. The Reference DB person flows through the same
  pipeline — when the actor database gains multiple photos per person,
  they'll be used automatically.
- **Zoom the preview.** Scroll the mouse wheel over the picture to zoom
  in (anchored at the cursor), drag to pan while zoomed, double-click to
  fit again. Works on stills and playing GIFs; switching pictures resets
  the view.

## v1.25.0 — 2026-08-12
- **Qwen is part of the base install, and updates now reach every
  install.** Fresh Setup runs download the full model pack including the
  Qwen editor. More importantly for existing installs: the app-updater
  used to replace only the exe, leaving the on-disk model list
  (`models_manifest.json`) frozen at whatever version you first
  installed — which is why models added in later versions (like Qwen)
  never appeared in your update popup. The exe now carries its own model
  list and refreshes the disk copy at startup, so after any app update
  the "Updates available" popup offers exactly what the new version
  needs. Accept it once and the swap runs on Qwen from then on.

## v1.24.1 — 2026-08-12
- **Wrong-face fix (the "bearded man" bug).** The swap instructions
  mentioned concrete traits ("beard, glasses…") as examples of what to
  copy — and with some inputs the words themselves leaked into the
  picture, turning a woman's face into a bearded man. Both swap prompts
  are now trait-neutral: they defer entirely to the reference photo and
  explicitly add nothing the person doesn't have.
- **"Engine stopped responding" during the swap fixed.** The swap now
  waits up to 30 minutes instead of 10 (a first-time 19–28 GB swap-model
  load from disk could blow the old limit), and it unloads the base
  model from VRAM before the swap engine loads, so the two no longer
  fight for memory mid-run.
- **One-time Qwen offer.** If the Qwen editor isn't installed when you
  tick the swap box, the app now offers the one-time ~28 GB download —
  it's much more reliable at landing the face than the Kontext fallback.
  Decline and it won't ask again (Kontext is used quietly).

## v1.24.0 — 2026-08-12
- **Face swap is now a checkbox, not a button.** 🔀 **Use RAG & LoRA for
  image swap** (in the image-editor section) replaces the "Swap into
  selected" button. Tick it and Generate runs two steps: your prompt +
  preset + LoRAs + RAG map draw the styled picture as usual, then the
  face from your loaded image (or the chosen 👤 Person if nothing is
  loaded) is applied to the person in it. Both pictures land in the
  gallery — the styled base and the face-swapped one. While ticked, a
  loaded image is the face, not an edit target; untick to go back to
  normal instruction editing. Variations = N base+swap pairs. The
  setting is remembered across restarts.
- **The face actually lands now: the swap runs on Qwen Image Edit** when
  it's installed (it self-installs on first use like any editor; needs
  ~24 GB VRAM). Qwen takes multiple images natively, and in live testing
  it transferred the face correctly on every pair — including ones where
  Flux Kontext kept the scene but ignored the face entirely. Without
  Qwen the swap falls back to Kontext, which preserves the scene but may
  need a few Variations for the identity to land.
- **Security hardening + delta review.** The LoRA trigger reader now
  refuses oversized sidecar files (>5 MB) and caps any trigger phrase at
  200 characters before it can reach the prompt. A full OWASP delta
  review of everything added since v1.20 is appended to `SECURITY.md` —
  verdict: no new attack surface; all new file parsing is bounded.

## v1.23.0 — 2026-08-12
- **Stronger face transfer.** The swap instruction now explicitly demands
  the head and face be replaced with the second image's person — facial
  structure, eyes, skin tone, hair, beard — and the swap runs at higher
  guidance (3.0) so Kontext follows the instruction instead of just
  restyling the base picture. If a swap still comes back with the original
  face, try Variations 3–5 and keep the best — identity transfer varies
  by seed.
- **Canvas size is respected everywhere in a swap.** The swapped picture
  now always lands at your Canvas size in gen-then-swap (previously a
  4x-upscaled base dragged the swap to 4x size), and a direct swap always
  keeps the selected picture's exact size (previously Kontext snapped to
  its own ~1-megapixel dimensions unless "Output at Canvas size" was
  ticked).

## v1.22.0 — 2026-08-12
- **Face swap actually swaps now.** The Kontext swap was sending the two
  pictures stitched side-by-side, and the model redrew them side-by-side
  (or ignored the face) instead of transferring it. Both swap paths now
  feed the base picture and the face as two separate chained references,
  and the face really lands on the person — verified live against the old
  behavior on the same seed.
- **Variations applies to swaps.** Set Variations to N and 🔀 makes N
  complete base+swap sets (nothing selected) or N differently-seeded swap
  attempts on the selected picture — keep the best. The direct swap also
  honors your Random/fixed seed choice now.
- **The 🔀 Swap button is red and bigger**, so the one-click action stands
  out from the row of utility buttons around it.

## v1.21.0 — 2026-08-12
- **Generate + swap in one click.** 🔀 no longer needs a picture selected in
  the gallery: with nothing selected it first generates a base image from
  your prompt + preset + LoRAs + RAG map, then swaps the chosen face into it
  with Flux Kontext — and keeps both pictures. With a gallery picture
  selected it still swaps straight into that one. Your styling applies to
  the base generation; the swap pass only changes the person.
- **LoRA trigger words are applied automatically.** When you tick a LoRA,
  its activation keyword(s) are read from the LoRA's own metadata (kohya /
  `modelspec`) or a CivitAI sidecar file next to it, and quietly added to
  the prompt sent to the engine — you never have to remember or type them.
  Already-present words aren't doubled, and your typed prompt is untouched.
  The CivitAI downloader now saves the trained words beside every LoRA it
  fetches, and ➕ Add LoRA file… carries an existing sidecar along.
- **Hover tooltips everywhere.** The whole left panel (prompt, negative,
  model, presets, LoRAs, RAG map, canvas, steps, variations, seed), the
  animator, and the gallery/save buttons now explain themselves on hover,
  matching the editor tooltips added in v1.20.0.

## v1.20.0 — 2026-08-12
- **LoRA / RAG status badges, top-right.** Two badges next to the GPU meter
  show at a glance whether LoRAs and your RAG map will affect the next
  generation: **green = will apply**, **red = won't**. LoRA is green when at
  least one is ticked and you're not editing a loaded image; RAG is green
  when a valid map is loaded, you're not editing, and the model is SDXL (RAG
  image guidance needs SDXL, so it's red on Flux). Loading an editor image
  turns both red; clearing it turns them back green.
- **Swap into selected now takes a loaded image as the face too** — not just
  the Reference DB person. When both are available it asks which to use. The
  loaded image is consumed by the swap and cleared afterwards, so LoRAs and
  RAG switch back on for your next generation. It no longer requires a DB at
  all — load a face and swap it into any selected picture.
- **Hover tooltips** explain every image-editor control (Load, Use selected,
  Reference DB, Person, To editor, Swap, editor engine, canvas size, and the
  new badges). The wordy image-editor heading is now a brief line, with the
  detail moved into the tooltips.
- The three primary **Generate buttons** (art, animation, border) are now the
  same size.

## v1.19.0 — 2026-08-12
- **LoRA + RAG + a person now combine in one generation.** When an
  embeddings-only ("private") RAG map and a Reference DB person are both
  active, they no longer fight — the map's guidance and the person's face
  chain on a single IP-Adapter, so your LoRAs and RAG map build the styled
  body and scene while the person steers the identity, all in one pass.
  (Previously the chosen person replaced the private map for that run.)
  Image-based RAG maps already combined with a person and still do; SDXL
  models only, as before.
- **🔀 Swap into selected — one-click person swap.** A new button by the
  Reference DB sends the selected picture plus the chosen person straight to
  Flux Kontext with a ready-made "replace the person, keep the pose, framing,
  costume, lighting and art style" instruction. It turns the two-step
  "build a styled body, then swap the face in" flow into a single click, and
  keeps the output at the source picture's size. The button is enabled once
  you have a generated or selected image and a person chosen.
- The ℹ what-uses-what help now documents both: how LoRAs, RAG and a person
  combine in plain generation, and the new one-click swap.

## v1.18.0 — 2026-08-11
- **Reference database.** A new part of the Image editor section loads a
  people database made with the separate Actor DB Builder tool (a portable
  SQLite file built by the user from IMDb lists: name, age, sex and a
  photo per person — nothing ships with the app). 📇 Reference DB… loads
  the file; 👤 Person… opens a picker — searchable, every column sortable
  ascending/descending by clicking its header, with a photo preview — and
  the chosen person shows as a small thumbnail. The photo is extra context
  for the model, applied to whatever you're doing:
  - *Editing* (editor images loaded): the photo joins the editor's
    reference images exactly as if it had been loaded with 🖼 Load…, so
    Kontext/Qwen can draw that person into the result. ➡ To editor loads
    the photo as an editor reference directly for person-only edits.
  - *Plain generation* (no editor images): the face guides via IP-Adapter
    with your presets and LoRAs still active (SDXL models only). If an
    embeddings-only RAG map is also active, the explicitly chosen person
    wins for that run and the status bar says so.
  This is not RAG: nothing is retrieved — the selected picture is sent
  straight through. The DB path and chosen person survive restarts.
- **What-uses-what help.** An ℹ button beside the Reference DB explains
  which extras apply in each mode — editing bypasses presets, LoRAs and
  RAG maps (the editor pipeline doesn't use them), plain generation keeps
  them all — and the Image editor header now says so too.

## v1.17.0 — 2026-08-11
- **Remove a LoRA from the list.** A new "🗑 Remove" button next to
  ➕ Add LoRA file… deletes the ticked LoRA(s) from your LoRA folder — the
  list is a view of that folder, so that is what removing means. It asks
  first and says plainly that files are being deleted, warns if you are
  removing the frame LoRA the Border maker uses, and tells you if one
  could not be deleted because the engine still has it open.
- **Remove a RAG map.** The cryptic ✕ beside the RAG map is now a labelled
  "🗑 Remove" that takes the map out of use and forgets it, so it does not
  come back next launch. The map file on disk is left alone.
- **Missing files no longer linger.** LoRAs and RAG maps are re-checked
  whenever the lists refresh. A LoRA deleted or moved outside the app is
  dropped from your ticked selection instead of being sent to the engine
  and failing the job; a RAG map whose file has gone is unloaded with a
  note. If only some of a map's references are missing, the map keeps
  working and the label says how many are gone — "12 imgs (3 missing)".

## v1.16.1 — 2026-08-11
- **Fix: private (embeds) RAG maps now actually generate.** The embed path
  passed the basic IP-Adapter node's `weight_type: "standard"`, but the
  `IPAdapterEmbeds` node is an advanced node whose weight list has no
  "standard" — so the engine rejected the graph (HTTP 400) and no picture was
  made. Now uses "linear" (the advanced-node equivalent). Caught by a live
  end-to-end test in the real engine: with the fix, an embeds map renders an
  image essentially identical to feeding the reference images directly (mean
  pixel difference under 2 on 0–255), confirming the precomputed embeds
  condition IP-Adapter the same way the images would.

## v1.16.0 — 2026-08-11
- **Private RAG references.** A retrieval map made in Laura-Trainer with the
  new "Private references" option carries no viewable pictures — instead it
  ships each reference as a precomputed IP-Adapter embed. The app now reads
  those maps and feeds the embeds through the same "PLUS" IP-Adapter it uses
  for image references, so a map can guide your pictures without ever handing
  over openable copies of the training images. Ordinary image maps work
  exactly as before; the app picks the right path automatically. If a map has
  no usable references at all, it still falls back to using the captions as
  prompt text.
- Under the hood: retrieval, near-duplicate skipping, the paired-LoRA
  auto-apply and the trigger word behave identically for both kinds of map;
  only the final guidance step differs (stock `IPAdapterLoadEmbeds` →
  `IPAdapterCombineEmbeds` → `IPAdapterEmbeds` nodes for the embed path).

## v1.15.0 — 2026-08-11
- **Your setup is remembered properly.** Ticking a LoRA is now saved the
  moment you do it, so the LoRAs you work with are still ticked next
  time. A loaded RAG map and the prompt you last typed come back too.
  (The LoRA list had no way to trigger a save, so the choice only
  survived if some other setting happened to change before you closed.)
- **Blue dividers between the sections** of the left panel, so picture
  making, the image editor, the animator, the border maker and the batch
  queue are told apart at a glance.
- **Cancel now actually cancels.** Pressing ✕ during a Variations run
  stopped the picture being drawn and then quietly started the next one —
  so cancelling a set of six only ever skipped one picture. Measured on a
  set of five: the old Cancel still produced four of them. The button now
  stops the whole run at once: the current picture, the rest of the set,
  and anything waiting in the batch queue. Pictures that had already
  finished are kept, and the status line tells you where it stopped
  ("Cancelled — stopped after 2 of 6").
- Cancel is noticed within about a second instead of only between
  pictures, the engine's own queue is cleared so nothing restarts behind
  your back, and a border's automatic second pass and an animation's
  post-processing both stop as well.

## v1.14.0 — 2026-08-11
- **Add your own base model.** A new "📁 Add model…" button beside the
  model dropdown browses to any `.safetensors` on your disk — one you
  trained yourself, or downloaded from anywhere — and makes it available
  straight away, selected and ready to generate with. No restart needed,
  even if the engine is already running.
  - On the same drive the model is linked in rather than copied, so it
    takes no extra space at all. If it lives on another drive it has to
    be copied, and you're told the size before that starts; the copy runs
    in the background with the progress bar.
  - `.ckpt` and `.pt` files are refused with an explanation. They are
    pickle files and can run code when loaded, which is why this app only
    ever accepts safetensors.

## v1.13.1 — 2026-08-10
- **Closing the app now frees your graphics card.** The engine is a
  separate program that keeps the last model it used loaded — 7 GB after
  an SDXL job, up to ~17 GB after a Flux one. It used to keep running
  after you closed the window, so that memory stayed occupied until you
  rebooted or hunted the process down, and the leftover engine went on
  holding port 8188 against your next launch. Closing the window now
  shuts it down as well, and a leftover engine from a session that
  crashed is cleaned up too. An engine belonging to a second app window
  you still have open is left alone.
- **Fixed the stop-the-engine command, which never worked.** It looked
  for processes whose command line contained "ComfyUI", but the engine is
  started from inside that folder and its command line says only
  "main.py" — so it matched nothing, every time. Anything that needed to
  restart the engine (installing IP-Adapter, applying a model update,
  repairing the model paths at startup) quietly failed to stop the old
  one, then hit "port 8188 already in use" and carried on talking to the
  stale engine. It now matches by this installation's own folder.

## v1.13.0 — 2026-08-10
- **Prompt enhancer (optional, local).** A ✨ Enhance button under the
  prompt expands a few words into a full comic-art prompt using Ollama,
  a free local LLM runner — nothing leaves your machine. ↩ puts your own
  wording back. Ollama is never installed or required by this app: if it
  isn't running, the button explains what it is and everything else works
  exactly as before. Reasoning-model padding, "Sure, here's…" lead-ins and
  stray quoting are stripped from what comes back.
- **Reads the maps a trainer actually writes.** The 🧭 RAG map… button now
  takes a map file, the folder holding one, or a trainer's `index.json` —
  and understands both the flat `.ragmap.json` and the earlier
  `lora-retrieval-map` layout (`images_dir`, `embeddings_file`, entries
  with no keywords, a LoRA named without its extension). See RAGMAP.md.
- **The paired LoRA installs itself.** If the map names a LoRA you don't
  have and the file is sitting next to the map — which is where a trainer
  leaves it — the app offers to copy it into your LoRA folder. Matching
  is case- and extension-tolerant, so a map made on another machine still
  finds its LoRA here.
- **Varied references.** When a map ships CLIP embeddings, an example
  image that is a near-duplicate of one already retrieved is passed over
  for the next-best different one, so a generation isn't guided by the
  same picture four times.
- **Nothing missing is fatal.** A map with no images now loads and
  contributes its captions as text rather than being rejected; a map
  whose LoRA isn't installed still guides with its images; corrupt or
  mismatched embeddings are ignored instead of failing the load.

## v1.12.0 — 2026-08-10
- **RAG maps.** New "🧭 RAG map…" button in the first section loads a
  `.ragmap.json` produced alongside a trained LoRA (see RAGMAP.md for the
  format). At generation it retrieves the example images most relevant to
  your prompt and feeds them as IP-Adapter visual guidance, while
  auto-applying the paired LoRA and trigger word — so results stay
  faithful to what the LoRA was trained on. SDXL models only; offers a
  one-time IP-Adapter download, and falls back to caption text if it's
  not installed. The loaded map is remembered between sessions.

## v1.11.3 — 2026-08-09
- Simplified the editor checkbox label to just "Output at Canvas size".
- The Border maker now has its own **Variations (1-10)** control, so one
  prompt can auto-generate several borders (each a different seed) to
  pick the best from. Previously borders quietly used the main section's
  Variations value; now it's a dedicated, persisted setting in the
  Border section.

## v1.11.2 — 2026-08-09
- Removed the in-app LoRA training feature to keep the app simple and
  fully self-contained (it was the only feature that needed extra tools
  installed). "⭐ Add to training set" stays — it still builds a
  captioned dataset you can train with an external tool (see
  TRAINING.md).

## v1.11.1 — 2026-08-09
- Dropdowns now show their saved selection in readable text at all times.
  Previously most readonly dropdowns (aspect, size, keep, loop, motion,
  editor, presets, border model/style, video) rendered their value in a
  colour that vanished against the dark field, so they looked empty until
  clicked. Fixed the base combobox style to keep light text on the dark
  field in every state.

## v1.11.0 — 2026-08-09
Major feature release — six additions:
- **Hi-res upscaling.** New "Upscale 4x" toggle runs generations and
  borders through a RealESRGAN pass for print/4K-quality output
  (downloaded automatically by Setup / the update check).
- **In-app LoRA training.** "🎓 Train LoRA…" trains a style LoRA from your
  training set on your GPU, then installs it automatically. First use
  sets up the training toolkit (needs git + Python 3.10-3.12; one time).
- **Animator video + sprite sheets.** Export animations as MP4 or WebM
  (self-contained, no install needed) and/or a packed sprite-sheet PNG
  with a JSON atlas for game engines — alongside the existing GIF.
- **Batch queue.** ＋Q next to each Generate button adds the current
  settings as a job; "Run all" processes the whole batch unattended.
- **App self-updater.** On launch the app offers to download and install
  a newer release itself — no manual exe swapping.
- **Single-instance guard.** Warns if a second copy is opened and if a
  foreign engine already holds the port, preventing lost results.

## v1.10.5 — 2026-08-09
- The LoRA list now shows only the **current** border-frame LoRA;
  superseded versions (e.g. an older SDXL_BorderFrames_v#) are hidden so
  old attempts that didn't work don't clutter the list. Other LoRAs are
  unaffected.

## v1.10.4 — 2026-08-09
- **Auto-clean center (2nd pass).** When a theme puts a character or
  scene in the middle of a border, the app now automatically runs the
  frame through Flux Kontext to empty the center — keeping the ornate
  frame intact — then cuts the transparent center. This fixes the last
  failure mode (centered figures blocking the cutout). On by default;
  turns itself off with a note if Kontext isn't installed / won't fit
  VRAM, and falls back to the original frame if the pass fails. It only
  runs when the center is actually filled, so clean frames aren't slowed.

## v1.10.3 — 2026-08-08
- **Borders now float with a transparent margin** instead of running snug
  to the screen edge, matching the reference bezels.
- **Transparent center follows the frame's real inner silhouette**
  (octagon, arch, scalloped) — it keys the plain center by its own color
  instead of cutting a straight rectangle (falls back to a soft rectangle
  only when the center isn't cleanly empty).
- Lower border-LoRA strength and a stronger negative prompt reduce
  stray centered figures and gibberish text in the frame.
- Tip: use Variations 3–5 for borders and keep the best — some themes
  put content in the center or under-draw on a given seed.

## v1.10.2 — 2026-08-08
- **Much better borders.** The Border maker no longer forces generation
  into a flat rectangular edge band (which produced "a strip at the edge
  and a rectangle hole"). It now generates a complete ornate frame — like
  the reference set — and carves the center transparent along the frame's
  REAL inner silhouette, so ornaments that reach inward are preserved.
- Border prompt pushes intricate, varying-depth, corner-and-cartouche
  ornamentation; the negative prompt now fights scene-fill in the center.
- If a theme still bleeds into the middle, the center falls back to a
  clean rectangular cut so every border ends up with a usable
  transparent center.
- When the trained border LoRA is installed but your model is Flux (which
  can't use it), the Border maker auto-routes to Juggernaut-XL + the LoRA.

## v1.10.1 — 2026-08-08
- **Trained border LoRA (SDXL_BorderFrames_v1)** — a model trained on a
  curated bezel-frame collection that teaches the actual concept of a
  decorative frame (ornamental edges, corner medallions, themed
  cartouches) instead of drawing a full scene with a hole cut out. The
  Border maker auto-applies it on SDXL-family models (pick Juggernaut-XL
  for best results). Downloaded automatically by Setup / the update
  check.
- **➕ Add LoRA file…** button under the LoRA list: browse for any
  `.safetensors` LoRA and it's copied into your LoRA folder, ticked
  immediately, and loaded automatically on every restart. Plus a
  **📁 LoRA folder** shortcut to open the folder directly.
- Model downloads now support direct URLs, so custom-trained models can
  ship through the same Setup / update pipeline as the HuggingFace ones.

## v1.10.0 — 2026-08-08
- Border maker gets its own **Model** and **Style** dropdowns (defaults:
  "Same as main", no style) — pick a different model/style for borders
  without touching the main controls. Both persist across sessions.
- **✕ Cancel buttons** next to all three progress bars (main, animator,
  border) — stop a running generation at any time.
- **"Loading the model…" feedback**: the status line now says when the
  engine is loading a model into GPU memory, explaining the pause
  before the progress bar starts (this was the "nothing happens" wait).
- New first-run defaults: BW Manga + Graphic Novel + LineArt Manga
  LoRAs preselected, Wide canvas, Marvel House Style, "Output at canvas
  size" checked; the Editor now always starts on Flux Kontext.
- Groundwork for the trained border LoRA (SDXL_BorderFrames_v1): when
  installed, the Border maker auto-applies it on SDXL-family models
  with its trigger word for properly designed frames.

## v1.9.1 — 2026-08-08
- Fixes "NameError: make_collage is not defined" when generating a
  border with reference images — the reference-collage helper was lost
  in an earlier refactor and has been rebuilt (one ref letterboxes,
  several tile in a grid on the border canvas).
- Editor model downloads triggered from the Border maker or Animator
  now drive that section's own progress bar instead of the main
  Generate bar.

## v1.9.0 — 2026-08-07
- Animator preset actions: a new "Preset" dropdown above the Action box
  with 25 hand-tuned animation prompts in alphabetical order (Attacking,
  Blocking, Casting a spell, Celebrating, Climbing, Crouching, Dancing,
  Dodging, Dying, Falling, Flying, Idle, Jumping, Kicking, Laughing,
  Punching, Running, Shooting, Slashing, Sneaking, Stomping walk,
  Taunting, Walking, Walking (side view), Waving). Selecting one fills
  the Action box with a prompt written to produce strong, loopable
  full-body motion — edit it freely afterwards to tailor it to your
  character.

## v1.8.1 — 2026-08-07
- Fixes near-static animation loops from short prompts: the auto-cutter
  now only considers segments that actually contain motion (it could
  previously pick the quietest chunk of a weak clip). Terse actions like
  "walking" are automatically expanded into an explicit full-body cycle
  description, and the default duration is 3 seconds.

## v1.8.0 — 2026-08-07
- Live GPU memory meter top-right: green bar + "used / max MB" readout,
  refreshed every 3 seconds from the driver — watch VRAM fill as models
  load and free up when jobs end.
- Editor entries now show their VRAM requirement (e.g. "needs ~24 GB");
  editors beyond the card's memory are greyed out and can't be selected,
  with an explanation of exactly how much they need.
- Three-tier VRAM handling for models and editors: green = comfortable,
  RED = tight fit — it will load and run but slower (one-time warning
  explains it), grey = blocked. Close-to-the-limit cards can now use
  everything that physically loads.

## v1.7.7 — 2026-08-07
- Defringe corrected (v1.7.6's version could worsen the outline): the
  background color is now measured from the RAW frame's corners before
  cutting (sampling the cut frame read black transparent pixels and
  neutralized the un-blend), and the silhouette parameters returned to
  moderate (1 px tighten + halo-dust removal). Validated best-of-three
  on the walking character.

## v1.7.6 — 2026-08-07
- Loop menu finalized: Seamless auto-cut is the permanent default (fresh
  at every launch), with Ping-pong and Crossfade as per-run options. The
  in-place generated mode is removed from the menu — it froze
  walking-type actions. All remaining options are guarded against short
  clips and cannot break the pipeline.
- Validated: 4-second walks give the auto-cutter several stride cycles;
  loop seam measurably below general motion level (17.7 vs 22.7).
- Defringe made adaptive and more aggressive: the background color is
  sampled from each frame (the model repaints the stage, so it drifts),
  silhouettes erode 2 px, and faint halo pixels are dropped — removes
  the residual outline on transparent frames.

## v1.7.5 — 2026-08-07
- Clean edges on transparent animation frames: new defringe pass
  un-blends the known staging color out of semi-transparent edge pixels
  (removes the white/gray outline), then tightens and smooths the
  silhouette. Applied to every transparent animation frame and its GIF.

## v1.7.4 — 2026-08-07
- New default loop mode "Seamless motion (auto-cut)": the clip is
  generated with free, fluid motion, then automatically cut at its two
  most similar frames so it loops playing forward — walks and runs loop
  without ping-pong reversal. Longer durations (3–5 s) give tighter
  seams. "Seamless in-place (generated)" remains best for capes/idles.
- Auto-prep on every animation: the character is extracted from its
  background and staged on neutral gray before animating — fixes frozen
  or mushy motion from black backgrounds and hard cutouts.
- Seamless in-place quality: 30 steps / cfg 5.5.

## v1.7.3 — 2026-08-07
- Fixes near-static animations ("only the eyes blinked"): seamless mode
  anchors first and last frames to the same pose, and the model would
  take the laziest path. New **Motion** control (Subtle / Normal /
  Strong, default Strong) plus a rewritten motion-demanding prompt
  template. Measured ~10x more motion with the loop still closing
  cleanly.

## v1.7.2 — 2026-08-07
- Each section has its own progress bar + percent: the main bar serves
  only the main generation; the Animator and Border maker got dedicated
  bars under their generate buttons.
- Sections reordered: Animator above, Border maker at the very bottom.
- Bezel composer removed (superseded by the Image editor + border
  references); its code is deleted.

## v1.7.1 — 2026-08-07
- Progress everywhere, with percentages: Setup shows one overall bar +
  percent across the entire installation (runtime, packages, engine,
  every model byte); the app's generate bar shows percent and now also
  covers animations (which previously ran silently) and in-app model
  downloads.
- Clicking any Generate while a job runs now offers to cancel it —
  long animation jobs can no longer lock the app with no way out.
- "Make GIF" is now a checkbox in the Animator; generated GIFs land in
  the history, PLAY animated in the preview when selected, and Save As
  saves the .gif.
- New "Use selected" button in the Image editor: one click points the
  editor at the currently selected history image (label shows
  "selection"); greyed out while the history is empty.

## v1.7.0 — 2026-08-07
- True seamless loops: new "Seamless (generated loop)" mode — the
  animation is generated to start AND end on the character's exact pose
  (Wan 2.1 first-last-frame, ~18 GB, self-installs), so it loops playing
  forward. No ping-pong moonwalking, no crossfade. Now the default loop
  mode; ping-pong/crossfade remain as fast alternatives.
- Double-click a history thumbnail to load that image straight into the
  Animator's character slot.
- Model governance: a permanent audit test verifies every model file the
  app references is in the update manifest and resolves on HuggingFace —
  Setup always installs the latest of everything, and the startup check
  validates the full set (24 files, ~129 GB fully loaded).

## v1.6.0 — 2026-08-07
- ANIMATOR: animate a character image into sprite frames and a GIF
  (Wan 2.2, Apache 2.0, ~18 GB, self-installs). Pick a character (any
  image, or the current gallery selection), describe the action, choose
  duration (1–5 s), frames to keep (24/12/8/6 fps), loop mode
  (Ping-pong = perfect loop, Crossfade, or none), and canvas. Output:
  transparent PNG frames folder + looping GIF + optional zip — made for
  old-school animated sprites.
- Mouse wheel now scrolls the left panel from anywhere inside it.

## v1.5.3 — 2026-08-07
- Border maker defaults to widescreen 16:9 HD (1920x1080). Borders keep
  the exact-canvas-size logic from v1.5.2 (references condition the art,
  the selected aspect sets the output dimensions).

## v1.5.2 — 2026-08-07
- New "Output at canvas size" checkbox in the Image editor: the loaded
  image is used purely as reference while the output is generated at the
  canvas size selected in the settings — e.g. a portrait character can
  be re-staged into a widescreen scene. Off by default so in-place edits
  (like text removal) stay pixel-faithful to the original size.
- Borders from references now always output at the exact selected
  aspect/dimensions (no more resolution snapping).

## v1.5.1 — 2026-08-07
- Border maker simplified: the Style selector and the section's own
  Model/Variations controls are removed — borders now mimic the main
  generation settings (model, LoRAs, Variations, Steps, Seed, Editor).
  The section only sets border prompt, aspect, thickness and references.
- Full validation pass: main generation (3 model families), prompt-only
  masked borders, and border-from-reference via the editor all
  regression-tested.

## v1.5.0 — 2026-08-07
- Border references now use the image editor (same engines and logic as
  the Image Editor section): the reference is shaped to the border canvas
  and redrawn as the frame — style, characters and composition carry
  over. The old img2img seeding for border refs is removed, along with
  its influence slider. Prompt-only borders keep masked generation.

## v1.4.1 — 2026-08-07
- Setup now recalculates its numbers live from the model manifest: full
  model-pack size, how many GB are still to download on this machine, and
  the estimated total disk use when installation completes.
- Running the app before Setup now states plainly that it cannot create
  art until Setup completes the installation.

## v1.4.0 — 2026-08-07
- The reference feature is replaced by a true **image editor**
  (Gemini-style): load image(s) and the prompt is the instruction —
  change settings, remove words/titles, move characters to new scenes,
  increase details, combine images. Two engines: **Flux Kontext** (best
  overall) and **Qwen Image Edit** (best text removal, Apache 2.0),
  selectable in the editor row; models self-install on first use.
- Border-reference influence slider moved into the Border maker section.

## v1.3.4 — 2026-08-07
- "🗑 Delete image" button: permanently deletes the selected image from
  disk and the history strip (with confirm) — generate variations, keep
  only the one you want.

## v1.3.3 — 2026-08-07
- GPU compatibility check: the app detects your NVIDIA card's VRAM at
  startup. Models that fit show green in the model dropdowns; models
  beyond the card's memory are greyed out, labeled "exceeds GPU memory",
  and can't be selected or generated with (a clear warning explains why).
  Machines without an NVIDIA GPU get an explicit incompatibility notice.

## v1.3.2 — 2026-08-07
- Reference images simplified to a single override behavior (the three
  modes are gone): loading references makes them the source of art style,
  composition and characters — the prompt picks what to use from them,
  and the art-style preset + LoRA selections are automatically ignored
  while references are active. "Change amount" is now "Reference
  influence".
- Fixes "Prompt outputs failed validation (CheckpointLoaderSimple)": a
  stale engine left over from an old version could be running with wrong
  model paths. The app now detects an engine that can't see the model
  folder and restarts it with correct settings — automatically at
  startup, and with a prompt at generate time.

## v1.3.1 — 2026-08-07
- Fixes "HTTPError 400 … /prompt" for users who updated from older
  versions: the update popup could deliver the IP-Adapter models but not
  the engine node. The app now detects the missing add-on when a
  style/character reference is used and installs it itself (node +
  models + engine restart) after a yes/no prompt.
- Engine 400 responses now surface the real reason (invalid nodes,
  missing models) instead of a bare HTTP error.

## v1.3.0 — 2026-08-07
- Reference images now have three modes (fixes "the model just repeats
  the image"): **Redraw composition** (img2img, keeps layout — all
  models), **Copy the style** (paints new scenes in the reference's look),
  and **Use the character** (puts the reference's subject in whatever
  scene the prompt describes). Style/character modes use IP-Adapter and
  work with SDXL-family models.
- IP-Adapter node + models added to Setup and the update manifest;
  engine updates now preserve custom nodes.

## v1.2.3 — 2026-08-07
- First-run experience fix (blank model list on fresh PCs): the app now
  detects a missing engine or empty model folder, explains it plainly,
  and offers to launch Setup.exe directly. The status bar also says what
  to do when no models are found.

## v1.2.2 — 2026-08-07
- Portability fix for public release: the model-paths config is now
  written by the app with the machine's own absolute path at every engine
  start — the folder works wherever it is unzipped.
- First GitHub release.

## v1.2.1 — 2026-08-06
- Border maker takes a full multiline prompt (not just a theme), with a
  checkbox to either wrap it in the frame-structure wording (recommended)
  or use it 100% verbatim for maximum precision.

## v1.2.0 — 2026-08-06
- Bezel composer (🧩 button): composite character/art PNGs onto any
  border — position presets (side panels, corners, top/bottom), size,
  flip, nudge, optional screen-area clipping, preview, one-click save.
  In-app directions explain which borders and images to use.
- New model: Flux.1-schnell fp8 (Apache 2.0 — outputs fully unrestricted,
  including commercial use). 4-step sampling wired in automatically.
- Startup update check now also covers tools: the ComfyUI engine is
  checked against GitHub and offered in the same update popup as models;
  updating preserves user data, refreshes packages, restarts the engine.
- Multiple reference images for the main generation (several = collage),
  and the Border maker now takes reference images too — they guide the
  border art (influence via the Change amount slider) while the center
  stays empty.
- "⭐ Add to training set" button banks any result (image + prompt as
  caption) into training\dataset — the raw material for training your own
  style LoRA. See the new TRAINING.md for the full path from dataset to
  LoRA and how styles carry across model generations.

## v1.1.12 — 2026-08-06
- ROOT-CAUSE FIX for values vanishing from dropdowns (aspect ratio, model,
  LoRAs…): Tk's selection-export made a box's displayed value disappear
  whenever another box was clicked. All 12 input widgets now disable
  selection export — selections always stay visible, including after
  restart.
- Full code review pass; border variations no longer force random seeds
  (fixed seed + variations is reproducible again).

## v1.1.11 — 2026-08-06
- No selection is ever cleared by another choice: switching models only
  adapts the preset dropdown's option list; your current preset, border
  style, border model and aspect all stay exactly as you set them.
- Border model selector is fully independent of the main model selector.

## v1.1.10 — 2026-08-06
- Model is now selected first (moved above Art Style), and the preset
  dropdown only lists styles suited to the selected model's family.
- Selecting a preset no longer changes the model (the old auto-switch
  that reset the model selection is removed).

## v1.1.9 — 2026-08-06
- Border maker theming: style selector (Franchise for games/movies/comics,
  Arcade cabinet bezel, Material/concept), its own model selector, and
  its own Variations 1–10 to generate multiple borders per theme and pick
  the best.
- Borders now use masked generation — art can only be painted in the
  border zone, so the center stays empty no matter how busy the theme.
- Anti-text hardening baked into the border prompts (works even on Turbo
  models where negative prompts are weak).

## v1.1.8 — 2026-08-06
- Border maker moved from a popup into the bottom of the main control
  panel; its settings (theme, aspect, thickness) are remembered like
  everything else.
- The left control panel scrolls vertically (scrollbar + mouse wheel)
  when the window is too small to show all options.

## v1.1.7 — 2026-08-06
- Border maker: generate themed 4:3 / 16:9 (720p or 1080p) border frames
  with a fully transparent center — thickness slider 6–30%, hard-wired
  no-text negative prompt, uses the main window's model/LoRAs/variations.
  Saved as `border_*.png` with alpha.

## v1.1.6 — 2026-08-06
- Reference image (img2img): load any picture and the AI modifies /
  redraws / reinterprets it per your prompt and style preset. "Change
  amount" slider (10–95%) controls how far it strays from the original.
  Works with all models, variations, LoRAs and transparency; the
  reference's name and change amount are recorded in the PNG metadata.

## v1.1.5 — 2026-08-06
- "Batch" renamed to "Variations" and extended to 1–10: each variation
  runs the same prompt with a different seed.
- Clarified: the pipeline has no content filter or safety checker of any
  kind — generation limits come only from the chosen model's training.

## v1.1.4 — 2026-08-06
- "❌ Delete art files…" button under Clear history: permanently deletes
  all generated PNGs (output folder + engine scratch) after a red-text
  confirmation dialog showing file count and size.

## v1.1.3 — 2026-08-06
- Session gallery: horizontal scrollbar + mouse-wheel scrolling through
  thumbnails; newest result auto-scrolls into view.
- "Clear history" button at the bottom right empties the gallery strip
  (saved PNGs in `output\` are not touched).

## v1.1.2 — 2026-08-06
- LoRA selection redesigned: the three dropdowns are gone — one list where
  you tick any number of LoRAs (plus a single strength slider). This also
  eliminates the selection-eats-selection bug for good.
- Settings now auto-save on every change (debounced), not just on
  generate/close — verified to survive even a force-killed process.
- UI message loop hardened: an error while handling a result can no longer
  freeze the interface mid-generation.

## v1.1.1 — 2026-08-06
- Everything stays inside the app folder: pip cache disabled, Setup temp
  files moved in-folder, background-removal model and HuggingFace caches
  relocated to `models\` — nothing is written to the user profile or
  AppData.
- Setup always verifies models against their **latest releases** on
  HuggingFace (missing *and* outdated files are fetched), using the same
  check as the app — Setup and the app can no longer disagree, so no more
  surprise multi-GB prompts right after a completed setup.
- Setup grays out to "Installed" when everything is present and current,
  both on launch and after a successful run.
- `ComicArtCreator.exe` is now a single file in the main folder next to
  `Setup.exe` (no more `ComicArtCreator\` subfolder).
- LoRA fixes: the same LoRA can no longer be selected in two slots
  (it silently stacked twice); added a third LoRA slot.

## v1.1.0 — 2026-08-06
- Zero-prerequisite installs: Setup.exe now bootstraps a private embedded
  Python 3.12 runtime inside the app folder (`python\`) — target machines
  need nothing but 64-bit Windows 10/11 and an NVIDIA driver. Nothing is
  installed system-wide.
- App prefers the bundled runtime and falls back to a dev venv.
- `Setup.exe --cli [--skip-models]` headless mode (logs to setup.log).

## v1.0.2 — 2026-08-06
- Batch-file-free release: `Setup.exe` (graphical installer with progress
  log) replaces `setup.bat`/`setup.ps1`; the app launches from
  `ComicArtCreator.exe` only.
- Setup.exe no longer requires git — the engine is fetched as a zip.
- Both exes carry proper Windows version metadata; built for 64-bit
  Windows 10 and 11.

## v1.0.1 — 2026-08-06
- Version bump / packaging release (no functional changes from v1.0.0).

## v1.0.0 — 2026-08-06
Initial release.
- Tkinter studio app driving a headless ComfyUI engine (loopback only,
  auto-started, auto-spawned if not running).
- 4 starter checkpoints: Flux.1-dev fp8, Juggernaut XL v9,
  DreamShaper XL Turbo v2.1, Animagine XL 4.0 — sampler/steps/CFG
  auto-tuned per model family; model sizes + VRAM guidance shown in-app.
- 21 art style presets with editable style text, examples, and per-preset
  model hints.
- 7-LoRA starter pack (Flux_/SDXL_ prefixed) + 2 LoRA slots with strength
  sliders + CivitAI downloader (DPAPI-encrypted API key).
- Transparent-background generation (rembg isnet-general-use).
- Auto-save with full recipe embedded in PNG metadata; session gallery;
  Save As; seed control + reuse; batch up to 8.
- UI-state persistence across restarts; Clear button; user-typed negative
  prompts never overwritten by preset switching.
- Startup model-update check against HuggingFace with yes/no popup.
- Packaged exe (PyInstaller onedir), setup.ps1 for fresh machines.
- OWASP-mapped security review (SECURITY.md): safetensors-only policy,
  path-traversal-safe downloads, HTTPS-only, loopback engine.
