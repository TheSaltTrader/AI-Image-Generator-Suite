"""print_export — layout and writers, no window. Run with the venv python."""
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from PIL import Image, ImageDraw

import print_export as pe

PASS = FAIL = 0


def check(name, ok, info=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ok  ", name)
    else:
        FAIL += 1
        print("  FAIL", name, f" ({info})" if info else "")


print("svg bits")
check("lengths in inches", pe._length_in("2.5in") == 2.5
      and abs(pe._length_in("25.4mm") - 1.0) < 1e-9 and abs(pe._length_in("96") - 1.0) < 1e-9
      and pe._length_in("50%") is None)
_svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="2.0000in" height="1.0000in" '
        'viewBox="0 0 400 200"><rect x="0" y="0" width="200" height="200" fill="#cc1020"/>'
        '<rect x="200" y="0" width="200" height="200" fill="#1040cc"/></svg>')
_parts = pe.svg_parts(_svg)
check("svg_parts reads the viewBox and the inches",
      _parts is not None and _parts[1] == (0.0, 0.0, 400.0, 200.0) and _parts[2] == 2.0 and _parts[3] == 1.0)

print("pieces")
_png = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
ImageDraw.Draw(_png).rectangle([0, 0, 199, 199], fill=(204, 16, 32, 255))
ImageDraw.Draw(_png).rectangle([200, 0, 399, 199], fill=(16, 64, 204, 255))
_p = pe.Piece("two", 2.0, 1.0, svg=_svg, png=_png)
_half = _p.crop(0.0, 0.0, 0.5, 1.0, tile="part 1 of 2")
check("a crop keeps vector (viewBox) and raster in step",
      abs(_half.w_in - 1.0) < 1e-9 and _half.png.size == (200, 200)
      and 'viewBox="0.000 0.000 200.000 200.000"' in _half.svg and _half.tile == "part 1 of 2")
_td = Path(tempfile.mkdtemp())
_sheet = Image.new("RGBA", (600, 300), (0, 0, 0, 0))
ImageDraw.Draw(_sheet).rectangle([20, 20, 220, 120], fill=(204, 16, 32, 255))
ImageDraw.Draw(_sheet).ellipse([300, 40, 560, 280], fill=(16, 64, 204, 255))
_sheet.save(_td / "sheet.png")
_sheet_svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="2.0000in" height="1.0000in" '
              'viewBox="0 0 600 300"><rect x="20" y="20" width="200" height="100" fill="#cc1020"/>'
              '<ellipse cx="430" cy="160" rx="130" ry="120" fill="#1040cc"/></svg>')
(_td / "sheet.svg").write_text(_sheet_svg, encoding="utf-8")
_entry = {"model": "decal", "seed": "sheet", "png": str(_td / "sheet.png"),
          "svg": str(_td / "sheet.svg"), "size_in": (2.0, 1.0), "kind": "process"}
_pcs = pe.pieces_from_entry(_entry, gap_in=0.1)
check("a processed sheet is cut into its decals, each at its inch size, vector kept",
      len(_pcs) == 2 and all(p.svg and p.png for p in _pcs)
      and abs(sum(p.w_in for p in _pcs) - (201 + 261 + 24) / 300.0) < 0.05, [(p.w_in, p.h_in) for p in _pcs])
_entry_prev = {"model": "decal", "seed": "one", "png": str(_td / "sheet.png"),
               "size_in": (2.0, 1.0), "kind": "preview"}
check("a preview is one piece", len(pe.pieces_from_entry(_entry_prev)) == 1)
# a redraw folder with per-decal files
_rd = _td / "x_redraw"
_rd.mkdir()
for i in (1, 2, 3):
    (_rd / f"decal_{i:02d}.svg").write_text(_svg, encoding="utf-8")
    _png.save(_rd / f"decal_{i:02d}.png")
(_td / "x_redraw.svg").write_text(_sheet_svg, encoding="utf-8")
_entry_rd = {"model": "decal", "seed": "x_redraw", "svg": str(_td / "x_redraw.svg"),
             "png": str(_td / "sheet.png"), "size_in": (2.0, 1.0), "kind": "redraw"}
_rpcs = pe.pieces_from_entry(_entry_rd)
check("a redraw's per-decal files become the pieces (sizes from their SVGs)",
      len(_rpcs) == 3 and all(abs(p.w_in - 2.0) < 1e-9 and abs(p.h_in - 1.0) < 1e-9 for p in _rpcs))

print("layout")
_small = [pe.Piece(f"s{i}", 2.0, 1.0, png=_png) for i in range(6)]
_pages, _size = pe.layout(_small, (8.5, 11.0), margin_in=0.5, gap_in=0.25)
check("six 2×1 in pieces fit on one Letter page, three per row",
      len(_pages) == 1 and len(_pages[0]) == 6 and _pages[0][0][1] == 0.5 and _pages[0][0][2] == 0.5
      and abs(_pages[0][3][2] - 1.75) < 1e-9, [(x, y) for _p, x, y in _pages[0]])
_many = [pe.Piece(f"m{i}", 3.0, 2.0, png=_png) for i in range(16)]
_pages2, _ = pe.layout(_many, (8.5, 11.0), margin_in=0.5, gap_in=0.25)
check("sixteen 3×2 in pieces spill onto a second page, none scaled",
      len(_pages2) == 2 and sum(len(pg) for pg in _pages2) == 16
      and all(p.w_in == 3.0 for pg in _pages2 for p, _x, _y in pg), len(_pages2))
_big = pe.Piece("big", 12.0, 4.0, svg=_svg, png=_png)
_pages3, _ = pe.layout([_big], (8.5, 11.0), margin_in=0.5, gap_in=0.25)
_tiles = [p for pg in _pages3 for p, _x, _y in pg]
check("a 12 in wide piece is tiled across pages at full size with an overlap",
      len(_tiles) == 2 and all(p.tile for p in _tiles)
      and abs(_tiles[0].w_in - 7.5) < 1e-9 and abs(_tiles[1].w_in - (12.0 - 7.3)) < 1e-6
      and abs(_tiles[0].h_in - 4.0) < 1e-9, [(p.w_in, p.tile) for p in _tiles])
_pages_l, _size_l = pe.layout([_big], (8.5, 11.0), margin_in=0.5, gap_in=0.25, landscape=True)
_tl = [p for pg in _pages_l for p, _x, _y in pg]
check("landscape turns the page (wider tiles: 10 in + the rest)",
      _size_l == (11.0, 8.5) and len(_tl) == 2 and abs(_tl[0].w_in - 10.0) < 1e-9, (_size_l, [p.w_in for p in _tl]))

print("writers")
_out = _td / "out"
_out.mkdir()
_pdf = pe.write_pdf(_pages2, (8.5, 11.0), _out / "t.pdf", title="test")
import pymupdf
_doc = pymupdf.open(_pdf)
check("the PDF has one page per layout page at Letter size",
      _doc.page_count == 2 and abs(_doc[0].rect.width - 612) < 0.5 and abs(_doc[0].rect.height - 792) < 0.5)
_pdfv = pe.write_pdf([[(_p, 0.5, 0.5)]], (8.5, 11.0), _out / "v.pdf")
_docv = pymupdf.open(_pdfv)
check("a vector piece is placed as vector on the PDF page", len(_docv[0].get_drawings()) >= 2)
_pm = _docv[0].get_pixmap(dpi=72, alpha=True)
check("…at its place and size (0.5 in in, 2 in wide): red then blue, clear elsewhere",
      _pm.pixel(60, 60)[:3] == (204, 16, 32) and _pm.pixel(150, 60)[:3] == (16, 64, 204)
      and _pm.pixel(300, 300)[3] == 0, (_pm.pixel(60, 60), _pm.pixel(150, 60), _pm.pixel(300, 300)))
_pngs = pe.write_pngs(_pages, (8.5, 11.0), _out, 100)
_pg1 = Image.open(_pngs[0])
_a = np.asarray(_pg1)
check("a PNG page is the paper size at the dpi, transparent, with the pieces on it",
      _pg1.size == (850, 1100) and _a[5, 5, 3] == 0 and _a[60, 60, 3] == 255 and tuple(_a[60, 60, :3]) == (204, 16, 32)
      and round(float(_pg1.info.get("dpi", (0, 0))[0])) == 100, (_pg1.size, _a[60, 60]))
_svgs = pe.write_svgs([[(_p, 0.5, 0.5), (pe.Piece("r", 1.0, 1.0, png=_png), 3.0, 0.5)]], (8.5, 11.0), _out)
_txt = Path(_svgs[0]).read_text(encoding="utf-8")
check("an SVG page nests vector pieces and embeds raster ones",
      'width="8.5000in"' in _txt and '<svg x="48.000" y="48.000"' in _txt and 'href="data:image/png;base64,' in _txt)
_res = pe.export([_entry, _entry_prev], _td / "root", paper="A4 210 × 297 mm", formats=("pdf", "png", "svg"), dpi=150)
check("export() runs end to end: folder, pages, files, README",
      _res["pages"] == 1 and _res["pieces"] == 3 and len(_res["files"]) == 3
      and (Path(_res["folder"]) / "README.txt").exists(), _res)
try:
    pe.export([], _td / "root2")
    _raised = False
except RuntimeError:
    _raised = True
check("nothing to print is an error, not an empty folder", _raised)

print("printing")
_seen_args = []
_wd = _td / "printwd"
_wd.mkdir()
_pages_pr, _size_pr = pe.layout([pe.Piece("big", 12.0, 4.0, svg=_svg, png=_png)], (8.5, 11.0), landscape=True)
_r = pe.print_pages(_pages_pr, _size_pr, dpi=50, title="t",
                    runner=lambda a: (_seen_args.append(a), "printed")[1], work_dir=_wd)
_a0 = _seen_args[-1]
_pp = sorted(_wd.glob("print_page_*.png"))
check("print_pages renders every page at the dpi on white and hands the script the true page size",
      _r == "printed" and len(_pp) == len(_pages_pr) == 2
      and Image.open(_pp[0]).size == (550, 425) and Image.open(_pp[0]).mode == "RGB"
      and Image.open(_pp[0]).getpixel((2, 2)) == (255, 255, 255)
      and "-PageW" in _a0 and _a0[_a0.index("-PageW") + 1] == "11.0000", (_r, len(_pp), _a0))
_res2, _n2 = pe.print_piece(pe.Piece("wide", 3.0, 1.0, png=_png), (8.5, 11.0), dpi=50,
                            runner=lambda a: (_seen_args.append(a), "printed")[1])
check("a wider-than-tall picture prints landscape",
      _n2 == 1 and _seen_args[-1][_seen_args[-1].index("-PageW") + 1] == "11.0000")
_chk = pe.print_pages(_pages_pr, _size_pr, dpi=50, title="t", check_only=True)
check("the real print script runs in PowerShell and loads the pages (check mode)",
      _chk == "ok 2", _chk)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
