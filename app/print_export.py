"""Print export for decals: every decal result in the gallery is cut into
its decals and laid out on printable pages at its TRUE physical size —
the size the decal was made at, figure-scale conversion included — then
written as a PDF (vector where the decal is vector), PNG pages at a
chosen resolution (transparent background) and/or SVG pages.

A decal that is larger than the printable area is tiled across pages
with a small overlap, so a sheet enlarged for a bigger figure still
prints at its new size. Nothing is ever scaled to fit.

Pieces: a Piece is one decal with its size in inches and its picture —
the SVG text when it has one (kept vector all the way to the PDF/SVG
page) and/or a transparent PNG. The whole module is plain data in,
files out, so it is tested without a window.
"""
import base64
import io
import math
import re
import time
from pathlib import Path

from PIL import Image

PAPERS = [
    ("Letter 8.5 × 11 in", 8.5, 11.0),
    ("Legal 8.5 × 14 in", 8.5, 14.0),
    ("Tabloid 11 × 17 in", 11.0, 17.0),
    ("A4 210 × 297 mm", 210 / 25.4, 297 / 25.4),
    ("A3 297 × 420 mm", 297 / 25.4, 420 / 25.4),
    ("A5 148 × 210 mm", 148 / 25.4, 210 / 25.4),
]
PAPER_IN = {name: (w, h) for name, w, h in PAPERS}
FORMATS = ("pdf", "png", "svg")
USER_PX_PER_IN = 96.0            # SVG user units per inch on an exported page


# ---------------------------------------------------------------- SVG bits
_UNIT_IN = {"in": 1.0, "mm": 1 / 25.4, "cm": 1 / 2.54, "pt": 1 / 72.0,
            "pc": 1 / 6.0, "px": 1 / 96.0, "": 1 / 96.0}


def _length_in(v):
    """An SVG length ('2.5in', '63.5mm', '240' = px) in inches, or None."""
    if v is None:
        return None
    m = re.match(r"\s*(-?[0-9.]+)\s*([a-z%]*)\s*$", str(v))
    if not m:
        return None
    num, unit = float(m.group(1)), m.group(2).lower()
    if unit == "%":
        return None
    return num * _UNIT_IN.get(unit, 1 / 96.0)


def svg_parts(svg_text):
    """(inner, viewBox (x, y, w, h), w_in, h_in) of an SVG. The viewBox
    falls back to the pixel width/height; the inches to None when the
    root has no physical size."""
    m = re.search(r"<svg\b([^>]*)>(.*)</svg>", svg_text, re.S | re.I)
    if not m:
        return None
    attrs = dict(re.findall(r'([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*"([^"]*)"',
                            m.group(1)))
    vb = None
    if attrs.get("viewBox"):
        try:
            x, y, w, h = [float(t) for t in
                          re.split(r"[\s,]+", attrs["viewBox"].strip())]
            vb = (x, y, w, h)
        except Exception:
            vb = None
    w_in = _length_in(attrs.get("width"))
    h_in = _length_in(attrs.get("height"))
    if vb is None:
        try:
            vb = (0.0, 0.0,
                  float(re.match(r"[0-9.]+", attrs.get("width", "")).group(0)),
                  float(re.match(r"[0-9.]+", attrs.get("height", "")).group(0)))
        except Exception:
            return None
    return m.group(2), vb, w_in, h_in


def make_svg(inner, vb, w_in, h_in):
    x, y, w, h = vb
    return ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'width="{w_in:.4f}in" height="{h_in:.4f}in" '
            f'viewBox="{x:.3f} {y:.3f} {w:.3f} {h:.3f}">{inner}</svg>')


# ---------------------------------------------------------------- pieces
class Piece:
    """One decal to print: its physical size and its picture(s)."""

    def __init__(self, label, w_in, h_in, svg=None, png=None, tile=""):
        self.label = label
        self.w_in = float(w_in)
        self.h_in = float(h_in)
        self.svg = svg                  # SVG text (already sized in inches)
        self.png = png                  # PIL RGBA
        self.tile = tile                # "part 2 of 4" for a tiled piece

    def crop(self, fx0, fy0, fx1, fy1, tile=""):
        """The part of this piece between the given fractions, as a new
        Piece (vector stays vector: the viewBox is cropped)."""
        w_in = self.w_in * (fx1 - fx0)
        h_in = self.h_in * (fy1 - fy0)
        svg = png = None
        if self.svg:
            parts = svg_parts(self.svg)
            if parts:
                inner, (x, y, w, h), _wi, _hi = parts
                svg = make_svg(inner, (x + w * fx0, y + h * fy0,
                                       w * (fx1 - fx0), h * (fy1 - fy0)),
                               w_in, h_in)
        if self.png is not None:
            W, H = self.png.size
            box = (int(round(W * fx0)), int(round(H * fy0)),
                   max(int(round(W * fx0)) + 1, int(round(W * fx1))),
                   max(int(round(H * fy0)) + 1, int(round(H * fy1))))
            png = self.png.crop(box)
        return Piece(self.label, w_in, h_in, svg=svg, png=png, tile=tile)


def pieces_from_entry(params, gap_in=0.15, min_side_in=0.06):
    """The decals of one gallery result (its params dict) as Pieces.
    A redraw has one SVG+PNG per decal on disk; a preview is one decal;
    a processed sheet is cut into its decals (the SVG, when there is one,
    is cropped by viewBox so each piece stays vector)."""
    import decals
    kind = params.get("kind")
    out = []
    if kind == "redraw" and params.get("svg"):
        base = Path(str(params["svg"])).with_suffix("")
        files = sorted(base.glob("decal_*.svg")) if base.is_dir() else []
        for f in files:
            svg = f.read_text(encoding="utf-8")
            parts = svg_parts(svg)
            if not parts:
                continue
            _inner, _vb, w_in, h_in = parts
            pngf = f.with_suffix(".png")
            png = Image.open(pngf).convert("RGBA") if pngf.exists() else None
            if (w_in is None or h_in is None) and png is not None:
                w_in = w_in or png.width / 300.0
                h_in = h_in or png.height / 300.0
            if w_in and h_in:
                out.append(Piece(f"{base.name}/{f.stem}", w_in, h_in,
                                 svg=svg, png=png))
        if out:
            return out
    png = None
    if params.get("png") and Path(str(params["png"])).exists():
        png = Image.open(str(params["png"])).convert("RGBA")
    svg = None
    if params.get("svg") and Path(str(params["svg"])).exists():
        svg = Path(str(params["svg"])).read_text(encoding="utf-8")
    size_in = params.get("size_in")
    if not size_in or not size_in[0] or not size_in[1]:
        parts = svg_parts(svg) if svg else None
        if parts and parts[2] and parts[3]:
            size_in = (parts[2], parts[3])
        elif png is not None:
            size_in = (png.width / 300.0, png.height / 300.0)
        else:
            return out
    w_in, h_in = float(size_in[0]), float(size_in[1])
    label = str(params.get("seed") or params.get("user_prompt") or "decal")
    if kind == "preview" or png is None:
        out.append(Piece(label, w_in, h_in, svg=svg, png=png))
        return out
    # a sheet: cut it into its decals at the gap given
    dpi = png.width / w_in
    gap = max(2, int(round(gap_in * dpi)))
    boxes = decals.segment_decals(png, gap=gap,
                                  min_side=max(4, int(round(min_side_in * dpi))))
    if not boxes:
        out.append(Piece(label, w_in, h_in, svg=svg, png=png))
        return out
    parts = svg_parts(svg) if svg else None
    W, H = png.size
    for i, (x0, y0, x1, y1) in enumerate(boxes, start=1):
        pw_in = (x1 - x0) / dpi
        ph_in = (y1 - y0) / dpi
        psvg = None
        if parts:
            inner, (vx, vy, vw, vh), _wi, _hi = parts
            psvg = make_svg(inner, (vx + vw * x0 / W, vy + vh * y0 / H,
                                    vw * (x1 - x0) / W, vh * (y1 - y0) / H),
                            pw_in, ph_in)
        out.append(Piece(f"{label} {i:02d}", pw_in, ph_in, svg=psvg,
                         png=png.crop((x0, y0, x1, y1))))
    return out


# ---------------------------------------------------------------- layout
def _tiles(piece, avail_w, avail_h, overlap):
    """Split a piece too big for the page into overlapping tiles."""
    step_w = max(0.5, avail_w - overlap)
    step_h = max(0.5, avail_h - overlap)
    nx = max(1, int(math.ceil((piece.w_in - overlap) / step_w)))
    ny = max(1, int(math.ceil((piece.h_in - overlap) / step_h)))
    tiles = []
    for r in range(ny):
        for c in range(nx):
            x0 = c * step_w
            y0 = r * step_h
            x1 = min(piece.w_in, x0 + avail_w)
            y1 = min(piece.h_in, y0 + avail_h)
            tiles.append(piece.crop(x0 / piece.w_in, y0 / piece.h_in,
                                    x1 / piece.w_in, y1 / piece.h_in,
                                    tile=f"part {r * nx + c + 1} of {nx * ny} "
                                         f"(row {r + 1}, column {c + 1}; "
                                         f"{overlap:.2f} in overlap)"))
    return tiles


def layout(pieces, paper_in, margin_in=0.4, gap_in=0.12, landscape=False,
           overlap_in=0.2):
    """Place pieces on pages of `paper_in` (w, h) at their true size, rows
    (shelves) from the top-left, taller pieces first. A piece wider or
    taller than the printable area is tiled first. Returns
    (pages, page_size_in) where a page is a list of
    (piece, x_in, y_in) and page_size_in is (w, h) in inches."""
    pw, ph = paper_in
    if landscape:
        pw, ph = ph, pw
    avail_w = pw - 2 * margin_in
    avail_h = ph - 2 * margin_in
    if avail_w <= 0.5 or avail_h <= 0.5:
        raise ValueError("the margins leave no room on the page")
    ready = []
    for p in pieces:
        if p.w_in > avail_w + 1e-6 or p.h_in > avail_h + 1e-6:
            ready.extend(_tiles(p, avail_w, avail_h, overlap_in))
        else:
            ready.append(p)
    order = sorted(range(len(ready)), key=lambda i: (-ready[i].h_in, i))
    pages = []
    page, x, y, row_h = [], 0.0, 0.0, 0.0
    for i in order:
        p = ready[i]
        if page and x + p.w_in > avail_w + 1e-6:
            x, y, row_h = 0.0, y + row_h + gap_in, 0.0
        if page and y + p.h_in > avail_h + 1e-6:
            pages.append(page)
            page, x, y, row_h = [], 0.0, 0.0, 0.0
        page.append((p, margin_in + x, margin_in + y))
        x += p.w_in + gap_in
        row_h = max(row_h, p.h_in)
    if page:
        pages.append(page)
    return pages, (pw, ph)


# ---------------------------------------------------------------- writers
def _png_bytes(img):
    b = io.BytesIO()
    img.save(b, format="PNG")
    return b.getvalue()


def write_pdf(pages, page_in, path, title="Decals"):
    """One PDF, one page per layout page; vector pieces stay vector."""
    import pymupdf
    pw, ph = page_in
    doc = pymupdf.open()
    for n, page in enumerate(pages, start=1):
        pg = doc.new_page(width=pw * 72, height=ph * 72)
        for piece, x, y in page:
            rect = pymupdf.Rect(x * 72, y * 72, (x + piece.w_in) * 72,
                                (y + piece.h_in) * 72)
            placed = False
            if piece.svg:
                try:
                    sdoc = pymupdf.open("svg", piece.svg.encode("utf-8"))
                    src = pymupdf.open("pdf", sdoc.convert_to_pdf())
                    pg.show_pdf_page(rect, src, 0)
                    placed = True
                except Exception:
                    placed = False
            if not placed and piece.png is not None:
                pg.insert_image(rect, stream=_png_bytes(piece.png))
        pg.insert_text(pymupdf.Point(pw * 72 - 90, ph * 72 - 10),
                       f"{title} — page {n} of {len(pages)}", fontsize=6,
                       color=(0.5, 0.5, 0.5))
    doc.set_metadata({"title": title, "producer": "AI Image Generator Suite"})
    doc.save(str(path), garbage=3, deflate=True)
    doc.close()
    return str(path)


def render_page_png(page, page_in, dpi):
    """A layout page as a transparent RGBA picture at `dpi`."""
    pw, ph = page_in
    out = Image.new("RGBA", (max(1, int(round(pw * dpi))),
                             max(1, int(round(ph * dpi)))), (0, 0, 0, 0))
    for piece, x, y in page:
        w_px = max(1, int(round(piece.w_in * dpi)))
        h_px = max(1, int(round(piece.h_in * dpi)))
        img = None
        if piece.svg:
            try:
                import vector_redraw
                img = vector_redraw.render_svg(piece.svg, w_px)
            except Exception:
                img = None
        if img is None and piece.png is not None:
            img = piece.png
        if img is None:
            continue
        if img.size != (w_px, h_px):
            img = img.resize((w_px, h_px), Image.LANCZOS)
        out.alpha_composite(img.convert("RGBA"),
                            (int(round(x * dpi)), int(round(y * dpi))))
    return out


def write_pngs(pages, page_in, out_dir, dpi, stem="page"):
    paths = []
    for n, page in enumerate(pages, start=1):
        img = render_page_png(page, page_in, dpi)
        p = Path(out_dir) / f"{stem}_{n:02d}.png"
        img.save(p, dpi=(dpi, dpi))
        paths.append(str(p))
    return paths


def write_svgs(pages, page_in, out_dir, stem="page"):
    """SVG pages: vector pieces nested as <svg> with their own viewBox,
    raster pieces as embedded PNG <image>."""
    pw, ph = page_in
    u = USER_PX_PER_IN
    paths = []
    for n, page in enumerate(pages, start=1):
        els = []
        for piece, x, y in page:
            if piece.svg:
                parts = svg_parts(piece.svg)
                if parts:
                    inner, (vx, vy, vw, vh), _wi, _hi = parts
                    els.append(
                        f'<svg x="{x * u:.3f}" y="{y * u:.3f}" '
                        f'width="{piece.w_in * u:.3f}" height="{piece.h_in * u:.3f}" '
                        f'viewBox="{vx:.3f} {vy:.3f} {vw:.3f} {vh:.3f}" '
                        f'preserveAspectRatio="none">{inner}</svg>')
                    continue
            if piece.png is not None:
                b64 = base64.b64encode(_png_bytes(piece.png)).decode("ascii")
                els.append(
                    f'<image x="{x * u:.3f}" y="{y * u:.3f}" '
                    f'width="{piece.w_in * u:.3f}" height="{piece.h_in * u:.3f}" '
                    f'href="data:image/png;base64,{b64}"/>')
        svg = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
               f'width="{pw:.4f}in" height="{ph:.4f}in" '
               f'viewBox="0 0 {pw * u:.3f} {ph * u:.3f}">\n'
               + "\n".join(els) + "\n</svg>\n")
        p = Path(out_dir) / f"{stem}_{n:02d}.svg"
        p.write_text(svg, encoding="utf-8")
        paths.append(str(p))
    return paths


WHITE_LAYERS = ("none", "white", "underbase")


def split_white(img, white_min=235, chroma_max=24):
    """A page picture split for printing on clear film with white ink:
    (colour layer: everything but the white ink, white layer: the white ink
    as solid black on clear — the usual way to feed a white channel / a
    second pass). White = opaque, light and colourless."""
    import numpy as np
    a = np.asarray(img.convert("RGBA")).astype(np.int32)
    op = a[..., 3] > 128
    white = op & (a[..., :3].min(2) >= white_min) & \
        ((a[..., :3].max(2) - a[..., :3].min(2)) <= chroma_max)
    col = a.copy()
    col[..., 3] = np.where(white, 0, a[..., 3])
    wl = np.zeros_like(a)
    wl[..., 3] = np.where(white, 255, 0)
    return (Image.fromarray(col.astype(np.uint8), "RGBA"),
            Image.fromarray(wl.astype(np.uint8), "RGBA"))


def underbase(img):
    """White under ALL ink (an underbase: colours stay bright on clear
    film), as solid black on clear."""
    import numpy as np
    a = np.asarray(img.convert("RGBA"))
    out = np.zeros_like(a)
    out[..., 3] = np.where(a[..., 3] > 128, 255, 0)
    return Image.fromarray(out, "RGBA")


def write_white_layers(pages, page_in, folder, dpi, mode="white", progress=None):
    """For a printer with white ink (or a second pass on white): per page a
    COLOUR layer (white ink removed) and a WHITE layer (black = where white
    ink goes: the white areas, or under all ink for mode='underbase'),
    as PNGs and as two PDFs at the true size. Returns the files."""
    files = []
    col_pages, white_pages = [], []
    for n, page in enumerate(pages, start=1):
        if progress:
            progress(f"white-ink layers, page {n} of {len(pages)}…")
        img = render_page_png(page, page_in, dpi)
        col, wl = split_white(img)
        if mode == "underbase":
            wl = underbase(img)
            col = img
        pc = Path(folder) / f"page_{n:02d}_colour.png"
        pw_ = Path(folder) / f"page_{n:02d}_white.png"
        col.save(pc, dpi=(dpi, dpi))
        wl.save(pw_, dpi=(dpi, dpi))
        files += [str(pc), str(pw_)]
        col_pages.append([(Piece("colour", page_in[0], page_in[1], png=col), 0, 0)])
        white_pages.append([(Piece("white", page_in[0], page_in[1], png=wl), 0, 0)])
    files.append(write_pdf(col_pages, page_in, Path(folder) / "decals_colour.pdf",
                           title="Decals — colour layer"))
    files.append(write_pdf(white_pages, page_in, Path(folder) / "decals_white.pdf",
                           title="Decals — white ink layer"))
    return files


def export(entries, out_root, paper="Letter 8.5 × 11 in", landscape=False,
           margin_mm=10.0, gap_mm=3.0, formats=("pdf", "png"), dpi=600,
           progress=None, white_layer="none"):
    """The whole job: gallery entries (params dicts) → pieces → pages →
    files under out_root/print_<stamp>/. Returns a dict with folder,
    pages, pieces, tiles, files."""
    paper_in = PAPER_IN.get(paper) or PAPER_IN[PAPERS[0][0]]
    pieces = []
    for i, prm in enumerate(entries):
        if progress:
            progress(f"cutting result {i + 1} of {len(entries)}…")
        try:
            pieces.extend(pieces_from_entry(prm, gap_in=gap_mm / 25.4))
        except Exception as e:
            raise RuntimeError(f"could not read {prm.get('seed', 'a result')}: {e}")
    if not pieces:
        raise RuntimeError("nothing to print — no decal result could be read")
    pages, page_in = layout(pieces, paper_in, margin_in=margin_mm / 25.4,
                            gap_in=gap_mm / 25.4, landscape=landscape)
    tiles = sum(1 for pg in pages for p, _x, _y in pg if p.tile)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    folder = Path(out_root) / f"print_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    if "pdf" in formats:
        if progress:
            progress(f"writing the PDF ({len(pages)} page(s))…")
        files.append(write_pdf(pages, page_in, folder / "decals.pdf"))
    if "png" in formats:
        if progress:
            progress(f"writing {len(pages)} PNG page(s) at {dpi} dpi…")
        files.extend(write_pngs(pages, page_in, folder, dpi))
    if "svg" in formats:
        if progress:
            progress(f"writing {len(pages)} SVG page(s)…")
        files.extend(write_svgs(pages, page_in, folder))
    if white_layer in ("white", "underbase"):
        files.extend(write_white_layers(pages, page_in, folder, dpi,
                                        mode=white_layer, progress=progress))
    (folder / "README.txt").write_text(
        f"Decals for print — {paper}{' landscape' if landscape else ''}, "
        f"margin {margin_mm:g} mm, gap {gap_mm:g} mm.\n"
        f"{len(pieces)} decal(s) on {len(pages)} page(s)"
        + (f", {tiles} page-tile(s) for decals bigger than the page "
           "(overlap 0.2 in — trim one edge and butt them)" if tiles else "")
        + ".\nEvery decal is at its true size: print at 100% / 'actual "
          "size', never 'fit to page'. The PNG pages are transparent; the "
          "PDF has no background.\n"
        + ("White-ink layers: decals_colour.pdf / page_NN_colour.png hold the "
           "colours, decals_white.pdf / page_NN_white.png the white ink "
           + ("under all the ink (underbase)" if white_layer == "underbase"
              else "where the art is white")
           + " — black marks where white ink goes. Print the white layer "
             "first (white channel or a pass on a white-ink printer), then "
             "the colour layer on top, both at 100%.\n"
           if white_layer in ("white", "underbase") else ""),
        encoding="utf-8")
    return {"folder": str(folder), "pages": len(pages), "pieces": len(pieces),
            "tiles": tiles, "files": files}


# ---------------------------------------------------------------- printing
PRINT_PS1 = r'''param([string]$List, [double]$PageW, [double]$PageH, [string]$Title,
      [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$files = @(Get-Content -LiteralPath $List -Encoding UTF8 | Where-Object { $_ })
$imgs = @()
foreach ($f in $files) { $imgs += [System.Drawing.Image]::FromFile($f) }
if ($CheckOnly) { "ok " + $imgs.Count; foreach ($m in $imgs) { $m.Dispose() }; exit 0 }
$doc = New-Object System.Drawing.Printing.PrintDocument
$doc.DocumentName = $Title
$doc.OriginAtMargins = $false
$script:i = 0
$doc.add_PrintPage({
    param($s, $e)
    $g = $e.Graphics
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.PageUnit = [System.Drawing.GraphicsUnit]::Inch
    # the drawing origin is the printable corner: step back over the
    # printer's hard margin so the picture lands at its true place
    $hx = $e.PageSettings.HardMarginX / 100.0
    $hy = $e.PageSettings.HardMarginY / 100.0
    $g.DrawImage($imgs[$script:i], [single](-$hx), [single](-$hy),
                 [single]$PageW, [single]$PageH)
    $script:i++
    $e.HasMorePages = ($script:i -lt $imgs.Count)
})
$dlg = New-Object System.Windows.Forms.PrintDialog
$dlg.Document = $doc
$dlg.UseEXDialog = $true
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true; $owner.ShowInTaskbar = $false
$owner.StartPosition = 'CenterScreen'; $owner.Width = 1; $owner.Height = 1
$owner.Show(); $owner.Activate()
if ($dlg.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    # the paper the layout was made for, on the printer that was chosen
    $land = $PageW -gt $PageH
    $w = [math]::Round([math]::Min($PageW, $PageH) * 100)
    $h = [math]::Round([math]::Max($PageW, $PageH) * 100)
    foreach ($ps in $doc.PrinterSettings.PaperSizes) {
        if ([math]::Abs($ps.Width - $w) -le 6 -and [math]::Abs($ps.Height - $h) -le 6) {
            $doc.DefaultPageSettings.PaperSize = $ps; break
        }
    }
    $doc.DefaultPageSettings.Landscape = $land
    $doc.Print()
    "printed"
} else { "cancelled" }
$owner.Close()
foreach ($m in $imgs) { $m.Dispose() }
'''


def run_print_script(args, timeout=1800):
    """Run the print script; returns its last output line. Split out so a
    test can stand in for the real printer."""
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(["powershell.exe", "-NoProfile", "-STA",
                        "-ExecutionPolicy", "Bypass", "-File"] + list(args),
                       capture_output=True, text=True, timeout=timeout,
                       creationflags=flags)
    out = (r.stdout or "").strip().splitlines()
    if r.returncode != 0:
        raise RuntimeError(((r.stderr or "").strip().splitlines() or
                            ["the print script failed"])[-1][:300])
    return out[-1] if out else ""


def print_pages(pages, page_in, dpi=600, title="Decals", check_only=False,
                runner=None, work_dir=None):
    """Send layout pages to a printer through the Windows Print dialog:
    each page is rendered at `dpi` (vector pieces from their SVG, so the
    edges are as sharp as the printer can put down) on white, and drawn at
    the page's true size in inches — 100%, never fit-to-page. Returns
    'printed', 'cancelled' or, with check_only, 'ok N'."""
    import tempfile
    d = Path(work_dir or tempfile.mkdtemp(prefix="cbac_print_"))
    files = []
    for n, page in enumerate(pages, start=1):
        img = render_page_png(page, page_in, dpi)
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=img.split()[3])
        f = d / f"print_page_{n:02d}.png"
        flat.save(f, dpi=(dpi, dpi))
        files.append(str(f))
    lst = d / "pages.txt"
    lst.write_text("\n".join(files) + "\n", encoding="utf-8")
    ps1 = d / "print_pages.ps1"
    ps1.write_text(PRINT_PS1, encoding="utf-8-sig")
    args = [str(ps1), "-List", str(lst), "-PageW", f"{page_in[0]:.4f}",
            "-PageH", f"{page_in[1]:.4f}", "-Title", title]
    if check_only:
        args.append("-CheckOnly")
    return (runner or run_print_script)(args)


def print_piece(piece, paper_in, dpi=600, margin_in=0.4, title="Decals",
                runner=None, check_only=False):
    """One picture to the printer at its true size — a decal bigger than
    the printable area is tiled over several pages; the page is turned
    when the picture is wider than tall. Returns (result, pages)."""
    landscape = piece.w_in > piece.h_in
    pages, page_in = layout([piece], paper_in, margin_in=margin_in,
                            landscape=landscape)
    return print_pages(pages, page_in, dpi=dpi, title=title, runner=runner,
                       check_only=check_only), len(pages)
