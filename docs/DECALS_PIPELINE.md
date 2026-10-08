# Decals pipeline — how it works today (v2.26.1)

The single place to read before touching the Decals tab. `HANDOFF.md` §3 has
the history of how each piece arrived; `KNOWLEDGE_BASE.md` has every fix with
its measured reason. This file is the **current** design, the **rules the
user set**, and **how to check a change**.

Code: `app/decals.py` (pipeline), `app/vector_redraw.py` (Claude calls: text
sweep, vision drawing, quality judge, batch), `app/recraft_vectorize.py`
(Recraft via fal.ai), `app/api_cache.py` (paid answers remembered),
`app/compare_view.py` (Compare window + right-click fixes),
`app/print_export.py` (print layout, white-ink layers), UI and workers in
`app/comic_art_creator.py`.

---

## 1. The path of one sheet

```
source file ──iter_sources──► RGB page (+ dpi from the file)
   │  a transparent PNG is flattened onto a key colour far from the art
   ▼
prepare  (looks_like_photo → find_sheet/straighten/normalize — real photos only)
   ▼
to_working_dpi (pages above 315 dpi → 300)
   ▼
is_digital_art ?  ── yes: DIGITAL artwork (rendered sheet, logo file)
   │               └ no : a SCAN of real print (or a photo)
   ▼
process_image (cleanup: background → transparent; rules in §3)
   ▼
segment_decals → join_split_decals → find_copies
   ▼
redraw_sheet: per decal, first method that works (§2); copies placed (§4)
   ▼
sheet SVG + PNG at printed size, + one SVG/PNG per sticker, decals.json
```

### Scan or digital? (`is_digital_art`)

Two measurements, both on the page itself:

| | digital artwork | scan of print |
|---|---|---|
| backdrop (`solid_backdrop`) | ≥ 35 % of the border one exact colour (±6) | sensor noise, never one value |
| ink grain (`ink_grain`, \|px − median3\| on flat ink) | 0.00 – 0.34 measured | 0.36 – 1.83 measured |

Threshold 0.5, but **0.15 for a PDF's embedded scan** (`from_scan=True`): the
user's scanned PDFs have a flat white border yet grainy ink. The app passes
`digital=` into `process_image` and `redraw_sheet`; decals.json stores it so
Compare's right-click fix uses the same rules.

---

## 2. Drawing methods, in order (`_draw_one`)

| # | method | when | what |
|---|---|---|---|
| 1 | `text` | lettering found | the words read by Claude, set in a real font (bundled OFL faces), spelling checked; stripes/arrows beside the words drawn by the next methods |
| 2 | `geometric` | straight-edged shapes | outlines straightened, stars rebuilt (banners, flags) |
| 3 | `smooth` | **digital only** (or right-click), flat-colour art (`flat_colour_art`) | the art's own outlines traced into curves — round stays round, points stay sharp |
| 4 | `detail` | **digital only** (or right-click) | the original traced as it is: many colour layers, every stroke |
| 5 | `vector` | a key is set | Recraft (fal.ai) or the Claude vision model draws it |
| 6 | `trace` | always | clean trace of the scan |

The AI quality check (`judge_fn`) scores each drawing 0–10 beside its scan;
under 7 it tries up to two other methods and replaces the first only on a
clear win (+2) or a typeset tie. **On digital sheets `detail` / `smooth` are
never judged away** (Recraft dropped the HasLab stickers' navy bodies).

---

## 3. Cleanup rules (`process_image`) — set by the user, keep them

1. **White is background only when the background is white.** On any other
   backdrop, white ink and white outlines stay.
2. **Transparent PNGs keep their transparency** (the RGB hidden under clear
   pixels used to become a fake backdrop).
3. **A dark backdrop is never colour-balanced against** (red went magenta).
4. **Uniform strips along an edge** (a screenshot's white bar) are dropped.
5. **Digital sheet whose stickers have an outline** (`outline_colour` finds
   it; `sticker_bodies` applies it):
   - everything **inside each outline is the sticker** — its body (the navy,
     same colour as the sheet) stays; only the sheet outside is clear;
   - the outline is taken from the **original**, kept exactly, and closed
     where broken (bridges up to ~6 px — wider joins neighbouring stickers);
   - the outline is **one even colour** (most saturated line pixels) and the
     body **one exact backdrop colour** — no blue/teal blotches;
   - an opening with **its own outline all round** (a snake's mouth, ≥ 4000
     px) is **clear**, keeping the navy band between it and the art.
6. **Digital art on white:** white enclosed by a sticker's outline is its
   white ink (the small GI JOE logo's letters).
7. **Specks:** tiny clear holes inside the art take the colour of the ink
   next to them (`fill_specks`); every SVG shape gets a 2 px edge in its own
   colour so neighbours overlap and no white seam shows when zoomed
   (`seal_seams`).
8. **Scans** keep the verified recipe (`recipe_opts`: cleanup, tol 52,
   balance, exact, tidy matte, fill holes; gap `recipe_gap` ≥ 1.35 mm).

---

## 4. Repeated stickers (`find_copies`, `redraw_sheet`)

- Same picture printed several times → **drawn once**, placed on every copy
  (fit-checked against each copy's own scan).
- Matching: the verified RGB picture match first; then shape (silhouette +
  inner edges) for quarter turns, flips and other colours.
- One design in two colours → drawn from the **cleanest** colour group, the
  other colour **recoloured** (white detail → clear film), placed at the
  clean print's size.
- A decal printed in **two halves** is joined again when it matches a whole one.
- **Lettering** keeps best-of-3 copies (one read can pick the wrong face) and
  is **never placed mirrored**.

---

## 5. Checking a change — do all three

1. **Tests** (venv python): `app/update_ui_test.py`, `app/vector_redraw_test.py`,
   `app/print_export_test.py`, `app/recraft_test.py`, `app/api_cache_test.py`.
2. **Quality gate** (local, the user's verified whale sheets):
   `python tools/golden_gate.py check` — must say `GATE PASS` (0 of 118
   decals drifted). Reference in `golden/`, answers in `cache/api` (both
   gitignored, backed up — see §7). Nearly free from the cache.
3. **Many kinds of stickers** (free): `python tools/sticker_census.py
   <folders> --sample 12 --out <dir>` over the user's Stickers folder and the
   Decals LoRA library (`Scrapper\Decals\…`). Want kept ≥ 0.99, leak ≤ 0.01,
   scans classified as scans; look at the side-by-sides.

Then validate on **the smaller sheet first**, show the user zoomed in
(original beside result, on magenta so clear film is visible), and only after
their OK run the bigger ones (user's instruction).

---

## 6. Producing finished sheets

In the app: Decals tab → Add files → 🖊 Redraw to vector (all defaults are the
best settings). Outside the app, same result:

```
venv\Scripts\python.exe tools\build_sheets.py "C:\Users\renoi\Desktop\Stickers\Ready" ^
    "C:\Users\renoi\Desktop\Stickers\Haslab Rattler.png" ...
```

The user's approved finals (2026-10-05) are in `Desktop\Stickers\Ready`:
Cobra AIR Force (1 decal), Haslab Rattler (26), Hasslab Rattler 2 (92).

---

## 7. Local-only data (not in git) and its backup

| path | what | if lost |
|---|---|---|
| `golden/` | the verified whale sheets the gate compares against | re-record only from a build the user approved (`golden_gate.py record`) |
| `cache/api/` | paid Claude/Recraft answers by request hash (text only, no keys) | reruns cost money again |
| `Desktop\Stickers\` | the user's sources, `Ready\` finals | user data |

Backed up with `tools\backup_local.ps1` to `D:\Backups\AI-Image-Generator-Suite\<date>`.
Keys are **only** in the Windows Credential Manager
(`AIImageGeneratorSuite/anthropic`, `AIImageGeneratorSuite/fal`) — never in a
file, the repo, a backup or a release.

---

## 8. Known limits

- Digital sheets are traced as they are: text that is blurred/misspelled in
  the original (HasLab "DANOER") comes out the same — nothing is invented.
- ~11 single-pixel specks remain at 2× zoom on a 12.7" sheet (1/600 in).
- Outline gaps wider than ~6 px are not closed.
- A sheet must be under ~4000 px per side for the census tool (thumbnailed).
