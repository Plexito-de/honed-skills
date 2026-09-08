#!/usr/bin/env python3
"""Adaptable template for filling an official comb-box PDF form, one glyph per box.

This is a TEMPLATE to copy and re-coordinate per form, not a generic filler: the geometry
(field rects, which rows have visible boxes, the comb boundaries of a boxed row that has no
AcroForm field) is specific to each form and must be measured from the blank.

The coordinates below were measured from one publicly downloadable German tax-office direct-debit
blank, so that the numbers are real rather than invented. Yours will differ: measure your own.
Method and gotchas: ../SKILL.md.

Deps (throwaway venv): pip install pypdf reportlab pdfplumber
External: qpdf, pdftoppm (Homebrew: `brew install qpdf poppler`).

Usage:
  python fill_example.py BLANK.pdf            # inspect: print field geometry
  python fill_example.py BLANK.pdf OUT.pdf    # fill, flatten, render; THEN READ THE PNGs
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import BooleanObject, NameObject
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

FF_COMB = 1 << 24  # PDF 32000-1 text-field flag 25 (Comb)


# --- inherited-attribute resolution -------------------------------------------------------
# A widget may inherit /FT, /Ff, /MaxLen and /T from ancestors. Resolve by KEY PRESENCE up the
# whole chain, never with `or`: an explicit local /Ff of 0 is falsy, so `a or b` silently
# restores the parent's flags and reports a plain field as a comb field.
def inherited(obj, key):
    node, seen = obj, 0
    while node is not None and seen < 32:  # depth cap: malformed files can build a cycle
        if key in node:
            return node[key]
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
        seen += 1
    return None


def qualified_name(obj):
    """Full dotted field name. A bare /T is only the leaf, so two distinct fields under
    different parents can print identically."""
    parts, node, seen = [], obj, 0
    while node is not None and seen < 32:
        t = node.get("/T")
        if t:
            parts.append(str(t))
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
        seen += 1
    return ".".join(reversed(parts)) or "(unnamed)"


def checkbox_on_state(obj):
    """The name that actually MEANS checked for this widget, read from /AP /N.

    "/On" is NOT universal: plenty of forms use /Yes, /1 or /Ja. pypdf falls back to /Off when
    the requested name is absent from the appearance dictionary, so guessing silently UNchecks
    the box and the wrong result looks like a successful write.
    """
    ap = obj.get("/AP")
    if not ap:
        return None
    normal = ap.get("/N")
    if not normal:
        return None
    states = [str(k) for k in normal.keys()]
    for s in states:
        if s != "/Off":
            return s
    return None


# --- step 1: inspect the blank once (run, read output, then hardcode geometry) -------------
def inspect(blank: str) -> None:
    reader = PdfReader(blank)
    for pageno, page in enumerate(reader.pages, start=1):
        for a in page.get("/Annots") or []:  # a page with no widgets has no /Annots key
            o = a.get_object()
            ft = inherited(o, "/FT")
            if ft not in ("/Tx", "/Btn"):
                continue
            rect = [round(float(x), 1) for x in o.get("/Rect")]
            ff = int(inherited(o, "/Ff") or 0)
            ml = inherited(o, "/MaxLen")
            extra = ""
            if ft == "/Btn":
                extra = f" on_state={checkbox_on_state(o)}"
            print(
                f"p{pageno} {qualified_name(o):44} {ft} Rect={rect} "
                f"MaxLen={ml} comb={'Y' if ff & FF_COMB else 'n'}{extra}"
            )


# --- step 4: overlay helpers --------------------------------------------------------------
def per_cell(c, x0, y0, x1, y1, n_cells, s, fs=11.0):
    """One glyph per box, centred. Use ONLY where the form DRAWS visible dividers and
    len(s) <= n_cells (check the render, not the comb flag)."""
    if len(s) > n_cells:
        raise ValueError(f"{len(s)} chars into {n_cells} cells: use continuous() instead")
    cw = (x1 - x0) / n_cells
    base = y0 + (y1 - y0 - fs * 0.70) / 2  # rough cap-height centring; confirm in the render
    c.setFont("Helvetica", fs)
    for i, ch in enumerate(s):
        cx = x0 + (i + 0.5) * cw
        c.drawString(cx - stringWidth(ch, "Helvetica", fs) / 2, base, ch)


def continuous(c, x0, y0, x1, y1, s, fs=11.0):
    """Plain left-aligned text. Use on lines WITHOUT boxes, or when the value is longer than
    the available cells ("where it fits")."""
    base = y0 + (y1 - y0 - fs * 0.70) / 2
    c.setFont("Helvetica", fs)
    c.drawString(x0 + 2, base, s)


def per_boundaries(c, boundaries, s, baseline, fs=11.0):
    """One glyph per cell where the cells come from detected box boundaries (a boxed row with
    NO AcroForm field). `boundaries` needs len(s)+1 x-positions."""
    if len(boundaries) < len(s) + 1:
        raise ValueError(f"{len(s)} chars needs {len(s) + 1} boundaries, got {len(boundaries)}")
    c.setFont("Helvetica", fs)
    for i, ch in enumerate(s):
        cx = (boundaries[i] + boundaries[i + 1]) / 2
        c.drawString(cx - stringWidth(ch, "Helvetica", fs) / 2, baseline, ch)


def draw_overlay(page, checks_drawn=()):
    """Build the overlay for ONE page, sized from that page rather than assumed A4."""
    box = page.mediabox
    w, h = float(box.width), float(box.height)
    buf = tempfile.SpooledTemporaryFile(max_size=2 << 20)
    c = canvas.Canvas(buf, pagesize=(w, h))

    # BOXES -> per_cell(x0, y0, x1, y1, cells, value). Coordinates measured from the blank.
    per_cell(c, 65.5, 479.9, 547.1, 497.6, 34, "DE89370400440532013000")  # IBAN (placeholder)
    per_cell(c, 65.5, 450.7, 220.7, 468.5, 11, "COBADEFFXXX")  # BIC (placeholder)
    per_cell(c, 65.5, 538.3, 178.7, 556.1, 8, "12345")  # postcode

    # PLAIN LINE (no boxes drawn on this form) -> continuous, full text, no cramming.
    continuous(c, 248.7, 450.9, 547.3, 468.7, "Musterbank")
    continuous(c, 60.1, 684.2, 282.4, 699.7, "Finanzamt Musterstadt")

    # Value longer than its cells -> continuous, smaller font ("where it fits").
    continuous(c, 65.5, 596.6, 547.1, 614.4, "Mustermann, Max und Erika", fs=9.0)

    # Boxed row with no AcroForm field: 14 boundaries -> 13 cells, for 13 characters.
    tax_no_boundaries = [
        163.7, 177.8, 192.0, 206.3, 220.4, 234.6, 248.9,
        263.0, 277.2, 291.5, 305.6, 319.8, 334.0, 348.0,
    ]
    per_boundaries(c, tax_no_boundaries, "12/345/67890", baseline=289.5)

    # On a FLAT pdf there are no checkbox widgets to set, so draw the mark as content instead.
    for x, y in checks_drawn:
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x, y, "X")

    c.save()
    buf.seek(0)
    return buf


def run_qpdf(src: Path, dst: Path) -> None:
    """Flatten + compress, and FAIL LOUDLY.

    `--warning-exit-0` turns qpdf's warning status into 0, so `check=True` then fires only on a
    real error. Without that, `check=False` swallows a fatal exit; and because qpdf leaves an
    existing output file untouched when it errors, the next render happily rasterizes the
    PREVIOUS run's PDF and the mandatory visual check certifies the wrong document. Writing to a
    fresh temp and moving only on success removes that trap entirely.
    """
    proc = subprocess.run(
        [
            "qpdf",
            "--warning-exit-0",
            "--generate-appearances",
            "--flatten-annotations=all",
            "--object-streams=generate",
            "--compress-streams=y",
            "--recompress-flate",
            "--compression-level=9",
            str(src),
            str(dst),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    if proc.stderr.strip():
        print(f"qpdf warnings (output written):\n{proc.stderr.strip()}", file=sys.stderr)


# Declared once, so the AcroForm branch and the flat branch cannot disagree about what should be
# ticked. CHECKBOX_MARKS_FLAT are (x, y) points for a flat PDF, where there is no widget to set.
CHECKBOX_NAMES = ["<exact checkbox field name 1>", "<exact checkbox field name 2>"]
CHECKBOX_MARKS_FLAT: tuple[tuple[float, float], ...] = ()


def fill(blank: str, out: str) -> None:
    out_path = Path(out)
    reader = PdfReader(blank)
    writer = PdfWriter()
    writer.append(reader)

    # This template carries geometry for ONE page. inspect() prints fields for every page, so say
    # so loudly rather than silently filling page 1 of a multi-page Vordruck and rendering the rest
    # unchanged, which looks like a clean run.
    if len(writer.pages) != 1:
        raise SystemExit(
            f"{len(writer.pages)}-page form: this template fills page 1 only. Give draw_overlay() "
            "per-page geometry and loop, or fill the pages you have measured."
        )

    # Only touch form machinery when the document HAS a form. On a flat PDF (a scan, or the
    # no-AcroForm route in SKILL.md step 2) update_page_form_field_values raises, so a template
    # that calls it unconditionally cannot serve that route at all.
    has_acroform = "/AcroForm" in writer._root_object  # noqa: SLF001 - no public accessor
    if has_acroform:
        # Set each checkbox to ITS OWN checked state, never a guessed "/On".
        wanted = CHECKBOX_NAMES
        by_name = {}
        for a in writer.pages[0].get("/Annots") or []:
            o = a.get_object()
            if inherited(o, "/FT") == "/Btn":
                by_name[qualified_name(o)] = o
        updates = {}
        for name in wanted:
            widget = by_name.get(name)
            if widget is None:
                raise KeyError(f"checkbox {name!r} not on page 1; run inspect() and use its names")
            state = checkbox_on_state(widget)
            if state is None:
                raise ValueError(f"checkbox {name!r} has no non-/Off appearance state")
            updates[name] = state
        writer.update_page_form_field_values(writer.pages[0], updates, auto_regenerate=False)
        try:
            writer.set_need_appearances_writer(True)
        except Exception:  # older pypdf: no public setter
            writer._root_object["/AcroForm"][NameObject("/NeedAppearances")] = BooleanObject(True)

    # On a flat PDF there is no widget to tick, so the mark has to be drawn as content. Passing the
    # marks unconditionally keeps the parameter live: an empty tuple on an AcroForm form draws
    # nothing, while a flat form with marks declared gets them.
    overlay = draw_overlay(
        writer.pages[0], checks_drawn=() if has_acroform else CHECKBOX_MARKS_FLAT
    )
    if not has_acroform and CHECKBOX_NAMES and not CHECKBOX_MARKS_FLAT:
        raise SystemExit(
            "flat PDF with checkboxes to tick but no CHECKBOX_MARKS_FLAT coordinates: measure them "
            "off the 150-dpi render and set CHECKBOX_MARKS_FLAT, or clear CHECKBOX_NAMES."
        )
    writer.pages[0].merge_page(PdfReader(overlay).pages[0])

    with tempfile.TemporaryDirectory() as td:  # private dir: no predictable path, no symlink race
        interim = Path(td) / "interim.pdf"
        flattened = Path(td) / "flat.pdf"
        with open(interim, "wb") as f:
            writer.write(f)
        run_qpdf(interim, flattened)
        shutil.move(str(flattened), out_path)  # only now does `out` exist as this run's result

    # VERIFY: render EVERY page and READ them. -f 1 -l 1 would check only page 1 while qpdf
    # rewrote all of them. Never claim done without looking at the pixels.
    subprocess.run(["pdftoppm", "-png", "-r", "150", str(out_path), f"{out_path}.page"], check=True)
    pages = sorted(out_path.parent.glob(f"{out_path.name}.page*.png"))
    print(f"wrote {out_path}")
    print(f"now OPEN and read: {', '.join(p.name for p in pages)}")
    print("check every identifying number box-by-box; flattening removes interactivity, it does")
    print("NOT make the file uneditable, and it does not prove the glyphs are in the right cells")


if __name__ == "__main__":
    if len(sys.argv) == 2:
        inspect(sys.argv[1])
    elif len(sys.argv) == 3:
        fill(sys.argv[1], sys.argv[2])
    else:
        sys.exit(__doc__)
