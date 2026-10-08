"""Check the cleanup on MANY kinds of stickers at once (free: no model
calls). Run it after any change to the cleanup / keying / digital rules.

    venv\\Scripts\\python.exe tools\\sticker_census.py FOLDER [FOLDER ...] [--out DIR] [--sample N]

Every PDF / PNG / JPG under the folders (N random per folder, default all)
goes through the app's prepare + cleanup. Printed per page: photo / digital
detection, backdrop colour, outline colour (digital sticker sheets), decal
count. For pictures with their OWN transparency there is ground truth:
  kept  = share of the original's opaque pixels still opaque (want >= 0.99)
  leak  = share of its clear pixels made opaque            (want <= 0.01)
With --out, a side-by-side PNG (original | result on magenta) per page.

Folders used on 2026-10-05: the user's Desktop\\Stickers (16 pages) and the
"Decals" LoRA's library, Scrapper\\Decals\\{GI Joe,He-Man,Transformers}\\png
and Scrapper\\Decals\\_training\\{sheets,individual} (52 sampled: kept >=
0.991, leak <= 0.005).
"""
import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import numpy as np                                  # noqa: E402
from PIL import Image                               # noqa: E402
import decals                                       # noqa: E402

EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


def pages_of(path):
    """(label, original picture or None, rgb) — the picture keeps its alpha."""
    if path.suffix.lower() == ".pdf":
        for label, rgb, dpi in decals.iter_sources(path):
            yield label, None, rgb, dpi, True
        return
    im = Image.open(path)
    im.load()
    if max(im.size) > 4000:
        im.thumbnail((4000, 4000))
    yield path.stem, im, decals._rgb_source(im), 300, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--sample", type=int, default=0)
    a = ap.parse_args()
    out = Path(a.out) if a.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    random.seed(7)
    kept_all, leak_all = [], []
    for folder in a.folders:
        files = sorted(p for p in Path(folder).rglob("*")
                       if p.suffix.lower() in EXTS and "redraw" not in p.name)
        if a.sample and len(files) > a.sample:
            files = random.sample(files, a.sample)
        for f in files:
            try:
                for label, orig_im, rgb, dpi, from_pdf in pages_of(f):
                    photo = decals.looks_like_photo(rgb)
                    img, _n = decals.prepare_photo(rgb, rotate=0, auto_crop=photo, normalize=True)
                    dpi = int(round(dpi or 300))
                    img, dpi = decals.to_working_dpi(img, dpi)
                    digital = (not photo) and decals.is_digital_art(img, from_scan=from_pdf)
                    res = decals.process_image(img, **dict(
                        decals.recipe_opts(), native_dpi=dpi, target_dpi=dpi,
                        photo=photo, carrier=None, digital=digital))
                    A = np.asarray(res["rgba"])
                    kept = leak = None
                    if orig_im is not None and decals.has_transparency(orig_im):
                        al = np.asarray(orig_im.convert("RGBA"))[..., 3]
                        if al.shape == A.shape[:2]:
                            kept = float((A[..., 3][al > 128] > 128).mean())
                            leak = float((A[..., 3][al < 16] > 128).mean())
                            kept_all.append(kept)
                            leak_all.append(leak)
                    oc = None
                    if digital:
                        oc = decals.outline_colour(A)
                    n = len(decals.segment_decals(res["rgba"],
                                                  gap=decals.recipe_gap(1.35, dpi), min_side=24))
                    print(f"{label[:34]:34} photo={photo!s:5} digital={digital!s:5} "
                          f"backdrop={decals.solid_backdrop(img)} outline={oc} decals={n} "
                          f"kept={None if kept is None else round(kept, 3)} "
                          f"leak={None if leak is None else round(leak, 3)}", flush=True)
                    if out:
                        g = Image.new("RGBA", res["rgba"].size, (255, 0, 255, 255))
                        g.alpha_composite(res["rgba"])
                        x, y = img.convert("RGB"), g.convert("RGB")
                        x.thumbnail((900, 900))
                        y.thumbnail((900, 900))
                        o = Image.new("RGB", (x.width + y.width + 10, max(x.height, y.height)))
                        o.paste(x, (0, 0))
                        o.paste(y, (x.width + 10, 0))
                        o.save(out / f"{label[:60]}.png")
            except Exception as e:                       # one bad file never stops it
                print(f"{f.name}: FAILED {e!r}")
    if kept_all:
        print(f"ground truth on {len(kept_all)} transparent pictures: kept min "
              f"{min(kept_all):.3f} median {float(np.median(kept_all)):.3f}; leak max "
              f"{max(leak_all):.3f}")


if __name__ == "__main__":
    main()
