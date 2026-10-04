"""Quality gate: the verified decal sheets must keep coming out the same.

    python tools/golden_gate.py record  --src "Killer Whale Stickers.pdf"
    python tools/golden_gate.py check   --src "Killer Whale Stickers.pdf"

`record` runs the app's Redraw recipe (decals.recipe_opts / recipe_gap,
pages worked at 300 dpi, Recraft + text sweep + AI quality check with
Claude Opus 5.5, reuse copies) on the sheets and keeps the result as the reference ("golden"):
each page's SVG, its render and every decal's box. Paid answers go to a
cache (api_cache), so `check` replays them for free; only requests that a
code change made different are paid for (the cost is printed).

`check` runs the same recipe and compares every reference decal with the
new drawing at the same place: shape overlap (IoU of the inked pixels) and
colour. A decal under the limits is flagged, a report image marks it in
red, and the exit code is 1. Run it before every release.

The scans and the reference stay on this machine (golden/ and cache/ are
not in git).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import api_cache  # noqa: E402
import decals  # noqa: E402
import recraft_vectorize as rv  # noqa: E402
import vector_redraw as vr  # noqa: E402

IOU_MIN = 0.90          # a decal's ink must overlap the reference this well
COLOUR_MAX = 40.0       # mean RGB difference where both are inked


def run_page(raw, file_dpi, stats, model="claude-opus-5-5"):
    """The app's Redraw of one page (comic_art_creator.py work())."""
    photo = decals.looks_like_photo(raw)
    img, _note = decals.prepare_photo(raw, rotate=0, auto_crop=photo,
                                      normalize=True)
    dpi = int(round(file_dpi)) if (file_dpi and not photo) else 300
    img, dpi = decals.to_working_dpi(img, dpi)
    res = decals.process_image(img, **dict(decals.recipe_opts(), native_dpi=dpi,
                                           target_dpi=dpi, photo=photo,
                                           carrier=None))
    gap = decals.recipe_gap(1.35, dpi)
    import anthropic
    client = api_cache.wrap(anthropic.Anthropic(api_key=vr.get_api_key(),
                                                timeout=120.0))
    vfn = rv.make_vector_fn(rv.get_key(), 300, stats=stats)
    tfn = vr.make_text_fn(client, model, 300, stats=stats)
    jfn = vr.make_judge_fn(client, model, stats=stats)     # app default: on
    out = decals.redraw_sheet(res["rgba"], None, native_dpi=dpi, target_dpi=300,
                              gap=gap, vector_fn=vfn, text_fn=tfn, stats=stats,
                              judge_fn=jfn)
    return res["rgba"], out


def render(svg, size):
    im = vr.render_svg(svg, size[0]).convert("RGBA")
    if im.size != size:
        im = im.resize(size, Image.LANCZOS)
    return im


def compare(gold_png, new_png, boxes):
    """Per decal: (box, iou, colour, ok)."""
    g = np.asarray(gold_png).astype(np.int32)
    n = np.asarray(new_png).astype(np.int32)
    rows = []
    for b in boxes:
        x0, y0, x1, y1 = b
        ga, na = g[y0:y1, x0:x1], n[y0:y1, x0:x1]
        gm, nm = ga[..., 3] > 128, na[..., 3] > 128
        u = (gm | nm).sum()
        iou = float((gm & nm).sum()) / u if u else 1.0
        both = gm & nm
        col = (float(np.abs(ga[..., :3][both] - na[..., :3][both]).mean())
               if both.any() else 0.0)
        rows.append((b, iou, col, iou >= IOU_MIN and col <= COLOUR_MAX))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("record", "check"))
    ap.add_argument("--src", default=str(Path.home() / "Desktop" / "Stickers"
                                         / "Killer Whale Stickers.pdf"))
    ap.add_argument("--golden", default=str(ROOT / "golden"))
    ap.add_argument("--cache", default=str(ROOT / "cache" / "api"))
    ap.add_argument("--pages", default="all", help="'all' or e.g. p1,p3")
    a = ap.parse_args()
    gold = Path(a.golden)
    gold.mkdir(parents=True, exist_ok=True)
    api_cache.set_dir(a.cache)
    want = None if a.pages == "all" else set(a.pages.split(","))
    failed, total_cost, report = 0, 0.0, []
    for label, raw, file_dpi in decals.iter_sources(a.src):
        tag = label.rsplit("_", 1)[-1]
        if want and tag not in want:
            continue
        stats = {}
        t = time.time()
        base, out = run_page(raw, file_dpi, stats)
        cost = float(stats.get("cost", 0.0))
        total_cost += cost
        size = base.size
        new_png = render(out["svg"], size)
        if a.mode == "record":
            (gold / f"{tag}.svg").write_text(out["svg"], encoding="utf-8")
            new_png.save(gold / f"{tag}.png")
            (gold / f"{tag}.json").write_text(json.dumps(
                {"size": size, "boxes": [list(it["box"]) for it in out["items"]],
                 "src": str(a.src)}), encoding="utf-8")
            print(f"{tag}: recorded {len(out['items'])} decals "
                  f"({time.time() - t:.0f}s, ${cost:.2f})")
            continue
        meta = json.loads((gold / f"{tag}.json").read_text(encoding="utf-8"))
        gold_png = Image.open(gold / f"{tag}.png").convert("RGBA")
        if gold_png.size != size:
            new_png = new_png.resize(gold_png.size, Image.LANCZOS)
        rows = compare(gold_png, new_png, [tuple(b) for b in meta["boxes"]])
        bad = [r for r in rows if not r[3]]
        failed += len(bad)
        vis = Image.new("RGB", (gold_png.width * 2 + 12, gold_png.height),
                        (255, 255, 0))
        for i, im in enumerate((gold_png, new_png)):
            bg = Image.new("RGB", im.size, (128, 128, 128))
            bg.paste(im, mask=im.split()[3])
            vis.paste(bg, (i * (gold_png.width + 12), 0))
        d = ImageDraw.Draw(vis)
        for b, iou, col, ok in rows:
            if ok:
                continue
            for off in (0, gold_png.width + 12):
                d.rectangle((b[0] + off, b[1], b[2] + off, b[3]),
                            outline=(255, 0, 0), width=3)
        vis.save(gold / f"report_{tag}.png")
        worst = min(rows, key=lambda r: r[1]) if rows else None
        print(f"{tag}: {len(rows)} decals, {len(bad)} drifted "
              f"(worst overlap {worst[1]:.3f}, colour {worst[2]:.0f}) — "
              f"{time.time() - t:.0f}s, ${cost:.2f}")
        for b, iou, col, ok in bad:
            report.append({"page": tag, "box": list(b), "iou": round(iou, 3),
                           "colour": round(col, 1)})
    print(f"paid this run: ${total_cost:.2f}; cache hits {api_cache.STATS['hits']}, "
          f"misses {api_cache.STATS['misses']}")
    if a.mode == "check":
        (gold / "report.json").write_text(json.dumps(report, indent=1),
                                          encoding="utf-8")
        print("GATE", "PASS" if not failed else f"FAIL ({failed} decals drifted;"
              f" see {gold}/report_*.png)")
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
