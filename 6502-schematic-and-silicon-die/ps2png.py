#!/usr/bin/env python3
"""Convert a PostScript (.ps/.eps) file to PNG image(s) using Ghostscript.

Two modes:

  default    Render the document as-is, one PNG per page. Good for the
             printable, page-tiled versions (6502_schematic_A3.ps / _A4.ps).

  --full     Reconstruct the entire drawing as ONE big PNG. The 6502.ps in
             this folder is a poster: every page draws the complete schematic
             (objects o0..oN) but a prolog clip path crops each page to a
             single A4 tile, and a y-flip pushes most of the drawing below the
             page origin where the media clips it. So a naive render yields
             only the top-left A4 corner. --full removes that page clip and
             shifts the drawing into view, then crops to the real ink bounds.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile


def find_ghostscript():
    """Return the path to a Ghostscript executable, or None if not found."""
    for name in ("gs", "gswin64c", "gswin32c"):
        path = shutil.which(name)
        if path:
            return path
    return None


GS = find_ghostscript()

# Common anti-aliasing / rendering flags.
COMMON = ["-dBATCH", "-dNOPAUSE", "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4"]

# The fig2dev/pstoedit prolog clip that bounds every page to the printable
# media, e.g. "newpath 0 842 moveto 0 0 lineto 595 0 lineto 595 842 lineto
# closepath clip newpath". Removing it lets the full drawing escape the tile.
PAGE_CLIP_RE = re.compile(
    r"newpath\s+0\s+[\d.]+\s+moveto\s+0\s+0\s+lineto\s+[\d.]+\s+0\s+lineto"
    r"\s+[\d.]+\s+[\d.]+\s+lineto\s+closepath\s+clip\s+newpath"
)

# The first page's coordinate setup, e.g. "-0.0 757.8 tr 1 -1 sc" — a translate
# followed by a vertical flip. We shift this translate to reposition the drawing.
PAGE_XFORM_RE = re.compile(r"^(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+tr 1 -1 sc$", re.M)


def _require_gs():
    if GS is None:
        raise RuntimeError(
            "Ghostscript not found. Install it (e.g. 'brew install ghostscript')."
        )


def measure_bbox(ps_path, width_pts, height_pts, first_page=1):
    """Return (llx, lly, urx, ury) of the ink on a page, using oversized media
    so nothing is clipped. Reads the HiResBoundingBox from the bbox device."""
    out = subprocess.run(
        [GS, "-q", *COMMON,
         f"-dFirstPage={first_page}", f"-dLastPage={first_page}",
         f"-dDEVICEWIDTHPOINTS={width_pts}",
         f"-dDEVICEHEIGHTPOINTS={height_pts}",
         "-dFIXEDMEDIA", "-sDEVICE=bbox", ps_path],
        capture_output=True, text=True, check=True,
    )
    m = re.search(r"%%HiResBoundingBox:\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)",
                  out.stdout + out.stderr)
    if not m:
        raise RuntimeError("Could not determine bounding box from Ghostscript.")
    return tuple(float(g) for g in m.groups())


def ps_to_png(input_path, output_path=None, dpi=150):
    """Render the document as-is, one PNG per page (default mode)."""
    _require_gs()
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    base = os.path.splitext(output_path or input_path)[0]

    # Count pages so single-page docs get a clean name and multi-page docs
    # get a -NNN suffix per page (instead of overwriting one file).
    pages = _count_pages(input_path)
    if pages > 1:
        out_spec = f"{base}-%03d.png"
    else:
        out_spec = f"{base}.png"

    subprocess.run(
        [GS, *COMMON, "-sDEVICE=png16m", f"-r{dpi}",
         f"-sOutputFile={out_spec}", input_path],
        check=True,
    )
    return out_spec if pages > 1 else f"{base}.png"


def ps_to_png_full(input_path, output_path=None, dpi=150):
    """Reconstruct the entire drawing as a single PNG (poster mode)."""
    _require_gs()
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    output_path = output_path or (os.path.splitext(input_path)[0] + "_full.png")

    with open(input_path, "r", errors="replace") as fh:
        text = fh.read()

    # 1. Drop the per-page clip so the drawing is no longer cropped to one tile.
    text, n_clip = PAGE_CLIP_RE.subn("newpath", text, count=1)
    if n_clip == 0:
        raise RuntimeError(
            "No page-clip path found — this file is not a poster-tiled PS. "
            "Use the default mode instead."
        )

    # 2. Find page 1's translate+flip. The flip puts most of the drawing at
    #    negative Y, so add a large offset to bring it into positive space,
    #    then measure where the ink actually lands.
    m = PAGE_XFORM_RE.search(text)
    if not m:
        raise RuntimeError("Could not find the page coordinate transform.")
    tx, ty = float(m.group(1)), float(m.group(2))
    BUMP = 12000.0
    bumped = (text[:m.start()] + f"{tx} {ty + BUMP} tr 1 -1 sc" + text[m.end():])

    with tempfile.NamedTemporaryFile("w", suffix=".ps", delete=False) as tf:
        tf.write(bumped)
        bumped_path = tf.name
    try:
        llx, lly, urx, ury = measure_bbox(bumped_path, 6000, BUMP + 4000)
    finally:
        os.unlink(bumped_path)

    width, height = urx - llx, ury - lly

    # 3. Shift the drawing so its ink lower-left sits at the origin, then render
    #    page 1 onto media sized exactly to the ink — a tight, full crop.
    final_xform = f"{tx - llx} {ty + BUMP - lly} tr 1 -1 sc"
    final = text[:m.start()] + final_xform + text[m.end():]

    with tempfile.NamedTemporaryFile("w", suffix=".ps", delete=False) as tf:
        tf.write(final)
        final_path = tf.name
    try:
        subprocess.run(
            [GS, *COMMON, "-dFirstPage=1", "-dLastPage=1",
             "-sDEVICE=png16m", f"-r{dpi}",
             f"-dDEVICEWIDTHPOINTS={width:.4f}",
             f"-dDEVICEHEIGHTPOINTS={height:.4f}", "-dFIXEDMEDIA",
             f"-sOutputFile={output_path}", final_path],
            check=True,
        )
    finally:
        os.unlink(final_path)

    return output_path


def _count_pages(path):
    """Count %%Page: markers (0 if none declared)."""
    n = 0
    with open(path, "rb") as fh:
        for line in fh:
            if line.startswith(b"%%Page:"):
                n += 1
    return n


def main():
    parser = argparse.ArgumentParser(
        description="Convert a PostScript file to PNG image(s)."
    )
    parser.add_argument("input", help="path to the .ps/.eps file")
    parser.add_argument("-o", "--output", help="output .png path")
    parser.add_argument("-r", "--dpi", type=int, default=150,
                        help="resolution in DPI (default: 150)")
    parser.add_argument("--full", action="store_true",
                        help="reconstruct the whole poster-tiled drawing as one PNG")
    args = parser.parse_args()

    try:
        if args.full:
            out = ps_to_png_full(args.input, args.output, args.dpi)
        else:
            out = ps_to_png(args.input, args.output, args.dpi)
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
