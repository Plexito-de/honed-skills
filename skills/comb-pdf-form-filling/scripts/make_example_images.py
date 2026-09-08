#!/usr/bin/env python3
"""Generate the illustration in ../assets/ from a SYNTHETIC blank.

Deliberately synthetic: the image has to show what a comb field is without shipping any real
authority's form layout or anyone's data. Every box here is drawn by this script and every value is
a documentation placeholder (DE89370400440532013000 is the standard German example IBAN).

Run: python make_example_images.py            # writes ../assets/comb-field-wrong-vs-right.png
Deps: reportlab, pdftoppm (poppler), Pillow.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

ASSETS = Path(__file__).resolve().parent.parent / "assets"
IBAN = "DE89370400440532013000"
CELLS = 34
FS = 11.0


def draw_comb_row(c, x0, y0, x1, y1, cells):
    """The printed artefact: one open box per character, exactly what a Vordruck prints."""
    c.setLineWidth(0.6)
    c.setStrokeColorRGB(0.1, 0.1, 0.1)
    cw = (x1 - x0) / cells
    c.line(x0, y0, x1, y0)
    c.line(x0, y1, x1, y1)
    for i in range(cells + 1):
        c.line(x0 + i * cw, y0, x0 + i * cw, y1)


def label(c, x, y, text, size=8.5):
    c.setFont("Helvetica", size)
    c.setFillColorRGB(0.25, 0.25, 0.25)
    c.drawString(x, y, text)
    c.setFillColorRGB(0, 0, 0)


def build_pdf(path: Path) -> None:
    w, h = 620, 176
    c = canvas.Canvas(str(path), pagesize=(w, h))
    x0, x1 = 40, 580
    cw = (x1 - x0) / CELLS

    # WRONG: one continuous string laid over the comb, the default you get by filling the field.
    y0, y1 = 118, 140
    label(c, x0, y1 + 9, "WRONG  filled as one string: the characters straddle the dividers")
    draw_comb_row(c, x0, y0, x1, y1, CELLS)
    c.setFont("Helvetica", FS)
    c.drawString(x0 + 2, y0 + (y1 - y0 - FS * 0.70) / 2, IBAN)

    # RIGHT: one glyph centred per cell.
    y0, y1 = 58, 80
    label(c, x0, y1 + 9, "RIGHT  one glyph centred per box, so every character is unambiguous")
    draw_comb_row(c, x0, y0, x1, y1, CELLS)
    c.setFont("Helvetica", FS)
    base = y0 + (y1 - y0 - FS * 0.70) / 2
    for i, ch in enumerate(IBAN):
        cx = x0 + (i + 0.5) * cw
        c.drawString(cx - stringWidth(ch, "Helvetica", FS) / 2, base, ch)

    label(c, x0, 30, "Synthetic: the boxes are drawn by this script and the value is the standard", 7.5)
    label(c, x0, 20, "documentation IBAN, not anyone's account.", 7.5)
    c.save()


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    out = ASSETS / "comb-field-wrong-vs-right.png"
    with tempfile.TemporaryDirectory() as td:
        pdf = Path(td) / "demo.pdf"
        build_pdf(pdf)
        prefix = Path(td) / "page"
        subprocess.run(
            ["pdftoppm", "-png", "-r", "150", "-cropbox", str(pdf), str(prefix)], check=True
        )
        png = next(iter(sorted(Path(td).glob("page*.png"))))
        try:
            from PIL import Image

            im = Image.open(png)
            im.save(out)  # page is already sized to the content, so no crop
        except ImportError:
            out.write_bytes(png.read_bytes())
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
