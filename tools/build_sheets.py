"""Redraw sticker sheets OUTSIDE the app, with exactly the app's defaults,
and write the finished files to a folder (how the user's "Ready" sheets
were made, 2026-10-05).

    venv\\Scripts\\python.exe tools\\build_sheets.py OUT_DIR SOURCE [SOURCE ...]

For every page of every SOURCE (PDF or image): the app's prepare step, the
verified cleanup recipe (decals.recipe_opts), digital-art detection, then
redraw_sheet with the text sweep + AI quality check (Opus 5.5), Recraft when
a fal key is set, and the local answer cache (reruns cost $0). Writes
<label>.svg + <label>.png and a "<label> - decals" folder (one SVG + PNG
per sticker). A file open in another program is not overwritten: the sheet
is saved as "<label> (new)" instead.

Keys come only from the Windows Credential Manager (vector_redraw /
recraft_vectorize); nothing is written to disk but the answers cache.
"""
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import anthropic                                    # noqa: E402
import api_cache                                    # noqa: E402
import decals                                       # noqa: E402
import recraft_vectorize as rv                      # noqa: E402
import vector_redraw as vr                          # noqa: E402


def main(out_dir, sources):
    api_cache.set_dir(ROOT / "cache" / "api")
    client = api_cache.wrap(anthropic.Anthropic(api_key=vr.get_api_key(), timeout=120.0))
    model = vr.DEFAULT_MODEL
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    total = 0.0
    for src in sources:
        src = Path(src)
        for label, raw, file_dpi in decals.iter_sources(src):
            photo = decals.looks_like_photo(raw)
            img, _note = decals.prepare_photo(raw, rotate=0, auto_crop=photo, normalize=True)
            dpi = int(round(file_dpi)) if (file_dpi and not photo) else 300
            img, dpi = decals.to_working_dpi(img, dpi)
            digital = (not photo) and decals.is_digital_art(
                img, from_scan=src.suffix.lower() == ".pdf")
            res = decals.process_image(img, **dict(decals.recipe_opts(), native_dpi=dpi,
                                                   target_dpi=dpi, photo=photo,
                                                   carrier=None, digital=digital))
            st = {}
            key = rv.get_key()
            vfn = (rv.make_vector_fn(key, 300, stats=st) if key
                   else vr.make_vector_fn(client, model, 300, stats=st))
            t = time.time()
            sheet = decals.redraw_sheet(
                res["rgba"], None, native_dpi=dpi, target_dpi=300,
                gap=decals.recipe_gap(1.35, dpi), stats=st, vector_fn=vfn,
                text_fn=vr.make_text_fn(client, model, 300, stats=st),
                judge_fn=vr.make_judge_fn(client, model, stats=st), digital=digital)
            name = label
            for name in (label, label + " (new)", label + " (v3)", label + " (v4)"):
                try:                     # a copy open in another program is locked
                    (out / f"{name}.svg").write_text(sheet["svg"], encoding="utf-8")
                    sheet["rgba"].save(out / f"{name}.png", dpi=(300, 300))
                    break
                except OSError:
                    continue
            d = out / f"{name} - decals"
            d.mkdir(exist_ok=True)
            for old in d.glob("decal_*"):        # no stale stickers from a bigger run
                old.unlink()
            for i, it in enumerate(sheet["items"], 1):
                (d / f"decal_{i:02d}.svg").write_text(it["svg"], encoding="utf-8")
                it["rgba"].save(d / f"decal_{i:02d}.png", dpi=(300, 300))
            cost = float(st.get("cost", 0.0))
            total += cost
            kinds = dict(Counter(it["source"] for it in sheet["items"]))
            print(f"{name}: {len(sheet['items'])} decals {kinds} digital={digital} "
                  f"{time.time() - t:.0f}s ${cost:.2f}", flush=True)
    print(f"total ${total:.2f} -> {out}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2:])
