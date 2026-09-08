---
name: comb-pdf-form-filling
description: Use when filling an official PDF form to print, sign or submit, and each character has to land inside its own printed box (a comb field) - IBAN, tax number, BIC, postcode, dates. Covers German Behörden-, Steuer- and Bank-Vordrucke (SEPA-Lastschriftmandat, Finanzamt and insurer forms) and any AcroForm or flat PDF with printed boxes. Triggers on comb field, Kästchen, one letter per box, Vordruck, Formular ausfüllen, fillable PDF, AcroForm, flatten PDF, and on filled text that crosses the box dividers or will not fit.
license: Apache-2.0
compatibility: Requires Python 3.9+ with pypdf, reportlab and pdfplumber, plus qpdf and poppler's pdftoppm on PATH. Agent-agnostic, no product-specific tooling.
metadata:
  version: "1.1"
---

# Comb-box PDF form filling

## What a comb field is

A **comb field** prints one open box per character, so the form expects one glyph per box. Filling it as an ordinary text field puts a continuous string over the top and the characters land across the dividers, which is what an authority rejects:

![A comb field filled wrongly as one continuous string, and filled correctly with one glyph centred per box](assets/comb-field-wrong-vs-right.png)

Both rows above hold the same value in the same field. The top one is what you get by default; the bottom one is what this skill produces. Regenerate the image with [`scripts/make_example_images.py`](scripts/make_example_images.py).

## Overview

Fill an official form so it looks typed by a clerk: every character sits **inside** its printed box, identifying numbers (IBAN, tax number, BIC) are unambiguous, and the result prints the same everywhere. Two principles carry the whole skill:

1. **Place glyphs by geometry, then VERIFY BY RENDERING.** A filled AcroForm can look right in one viewer and wrong in another, and a value written to the wrong state looks like a successful write. The only proof is to rasterize the finished PDF and read the pixels. Do it every time, on every page, and check the identifying numbers box by box.
2. **The AcroForm "comb" flag does NOT mean the form draws visible boxes.** Decide per-cell vs. continuous from the boxes actually drawn on the page (render the blank and look), never from the field flag. Seen in practice: a bank-name field carried comb=YES with MaxLen 21 and had no dividers printed at all, so per-cell spacing there looked absurd.

**Vocabulary**, because the forms this was built against are German: *Vordruck* is an official blank form, *Behörde* a public authority, *Kästchen* the printed box, *Steuernummer* a German tax number (13 characters, written with slashes, e.g. `12/345/67890`), *SEPA-Lastschriftmandat* a direct-debit authorisation.

## When to use

- Filling a government, tax, bank or insurance PDF that will be printed, signed, and posted or uploaded.
- Any fillable-PDF task where characters must land in individual boxes, especially account, tax or ID numbers.
- When a previously "filled" form shows text straddling the cell dividers, or a value is too long for the boxes.

**Not for:** generating a document from scratch (author it in HTML/LaTeX), or forms the recipient fills online. For general PDF work (extraction, merge and split, OCR, ordinary AcroForm filling) prefer the Anthropic pdf skill; see Prior art.

## Toolchain

| Job | Tool | Note |
|---|---|---|
| Read AcroForm fields (name, /Rect, /MaxLen, comb) | `pypdf` | resolve inherited keys up the whole parent chain |
| Detect drawn boxes on the blank | render + look (primary); `pdfplumber` edges (secondary) | edge-counting is noisy, see gotchas |
| Draw each glyph at a coordinate | `reportlab` canvas overlay | one glyph per cell, centred |
| Merge overlay onto the page | `pypdf` `page.merge_page()` | overlay becomes page content, so it survives flattening |
| Flatten to static, compress | `qpdf --warning-exit-0 --generate-appearances --flatten-annotations=all --object-streams=generate --compress-streams=y --recompress-flate --compression-level=9` | turns form values into ordinary page content. It removes interactivity; it does NOT make the file uneditable and is not an integrity guarantee |
| Render to verify | `pdftoppm -png -r 150 <out.pdf> <prefix>` | every page, not just page 1, then READ the PNGs. MANDATORY |

Set the Python side up in a throwaway environment, so nothing lands in a project env:

```bash
python3 -m venv /tmp/combfill && /tmp/combfill/bin/pip install pypdf reportlab pdfplumber
```

The two binaries come from your package manager: `brew install qpdf poppler` on macOS, `apt install qpdf poppler-utils` on Debian or Ubuntu. On Windows use WSL, or fetch qpdf and poppler builds and put both on PATH.

## The method

1. **Get the current official blank** from the authority's own site. Note the form number and version printed on it, because they change and the coordinates change with them. Keep that file; it is the fill source.
2. **Inspect the form.** For each widget print the **fully qualified** field name, `/Rect` `[x0,y0,x1,y1]` (PDF coords, origin bottom-left), `/MaxLen`, the comb flag, and for a checkbox its real checked state. Resolve `/FT`, `/Ff`, `/MaxLen` and `/T` by **key presence up the full parent chain**, never with `or`: an explicit local `/Ff` of `0` is falsy, so `local or parent` silently reports a plain field as a comb field. Note the exact spelling of each value you must enter, since an agency or bank often stores names in ASCII without diacritics.
   **If there is no AcroForm at all** (a flat scan), there are no `/Rect`s: measure the boxes off a 150-dpi render (pixels to points) and go straight to step 4. Skip every form-field call, including `update_page_form_field_values`, which raises without an `/AcroForm`.
3. **Map which fields have VISIBLE boxes.** Render the blank at 150 dpi and read it; crop uncertain rows. This map, not the comb flag, drives placement.
4. **Place text** on an overlay canvas sized from the page's own mediabox, not assumed A4:
   - **Box field, value fits (`len ≤ cells`):** one glyph per cell, centred. `cell_w = (x1-x0)/cells`; for char `i`, `center_x = x0 + (i+0.5)*cell_w`, drawn at `center_x - stringWidth(ch)/2`, with `baseline = y0 + ((y1-y0) - fontsize*0.70)/2`. The `0.70` approximates cap height as a fraction of font size, so confirm the vertical position in the render rather than trusting it.
   - **Plain line (no boxes), OR value longer than the cells:** write continuously, left-aligned at `x0+2`; drop the font size for a long value. Long-value-in-boxes is the "where it fits" case: continuous is correct, do not shrink to cram.
   - **Drawn boxes with NO AcroForm field** (a tax-number comb, for instance): detect the cell boundaries from the page's vertical vector edges in that row and place per detected cell. `n` characters need `n+1` boundaries; assert it.
   - **Pre-fill alignment check (cheap iteration):** before filling for real, render the blank with your computed cell centres drawn as ticks or thin rectangles and compare them against the printed boxes. A mis-measured `/Rect` is one pass to fix here, instead of a full fill, flatten and render cycle to discover. (The same idea appears as a validation-image step in the `anthropics/skills` pdf skill; the implementation here is independent and no code or text was copied.)
5. **Checkboxes: read each box's own checked state; never assume `/On`.** Take the non-`/Off` key from the widget's `/AP` `/N` dictionary, because forms use `/Yes`, `/1`, `/Ja` and others. pypdf substitutes `/Off` when the name you ask for is not in that dictionary, so a guessed `/On` silently leaves the box UNTICKED while the call reports success. On a mandate the tick is the authorisation, so this is the difference between granting something and granting nothing. Verify the tick in the render.
6. **Leave signature and date boxes empty**, since a wet signature is the point. Prefill a known place if asked.
7. **Flatten + compress** with the qpdf line above, writing to a fresh temporary file and moving it into place only after success. `--warning-exit-0` makes a warning exit 0 so `check=True` fires only on a real error. Without that discipline a fatal exit is swallowed, and because qpdf leaves an existing output file untouched when it errors, the next step renders the PREVIOUS run's PDF and the visual check certifies the wrong document.
8. **Verify by rendering EVERY page** (`pdftoppm -png -r 150`), read the PNGs, and confirm each identifying number sits one-per-box. `-f 1 -l 1` checks page 1 only while qpdf rewrote them all. Fix coordinates and re-render until right.
9. **Hand over** the flattened PDF and say plainly what is left for the pen.

## Gotchas

| Symptom | Cause / fix |
|---|---|
| Text crosses the cell dividers | You filled the AcroForm field directly; its appearance is continuous. Draw glyphs per-cell as an overlay and leave the field empty. |
| A field is spaced into boxes that don't exist on paper | Trusted the comb flag. Go by the drawn boxes in the render. |
| The glyphs are in the file (`pdftotext` finds them) but the render shows an empty shaded box | The widget appearance has a background (`/MK` `/BG`) and paints over your page content. Flatten the blank widget appearances first, keeping their borders, then merge the text overlay. |
| A plain field is reported as comb, or two fields print with the same name | `/Ff` resolved with `or`, so an explicit `0` fell through to the parent; and a bare `/T` is only the leaf name. Resolve inherited keys by presence up the full chain and build the qualified name. |
| A checkbox stays empty although the call succeeded | The form's checked state is not `/On`; pypdf fell back to `/Off`. Read the non-`/Off` key from `/AP` `/N`. |
| Divider auto-detection reports absurd counts (80 "dividers" where there are none) | `pdfplumber` decomposes each box into several edges and adjacent rows bleed into the band. Don't threshold on raw counts: render the blank and look, or match the count to `cells-1` per field. |
| `Font dictionary for /HeBo not found; defaulting to Helvetica` | pypdf appearance-font fallback. Harmless. |
| Value doesn't fit the boxes (e.g. a two-holder account name longer than the printed cells) | Write it continuously ("where it fits"); don't shrink to cram. |
| One viewer shows blank fields, another shows them | Stale appearance streams: set NeedAppearances, or flatten so the values become page content. |
| The form is XFA and filling it changes nothing visible | Dynamic XFA keeps the real form in XML, and the PDF pages may be fallback content only. Flattening those pages does not render the XFA layout or data, so detect `/XFA` first and route it through an XFA-capable renderer when the XML is authoritative. |
| Umlauts or accents look wrong, or the agency can't match the name | Use the exact registered spelling, often ASCII (`Nunez` not `Núñez`, `Strasse` not `Straße`). Confirm with the account holder. |

## Example

[`scripts/fill_example.py`](scripts/fill_example.py) is a working template: inherited-key resolution, per-cell and continuous placement, checkbox state detection, a flat-PDF branch, private temp handling, strict qpdf exit handling, and an all-page render. Run it with no output path to inspect a blank first:

```bash
/tmp/combfill/bin/python scripts/fill_example.py BLANK.pdf              # print field geometry
/tmp/combfill/bin/python scripts/fill_example.py BLANK.pdf OUT.pdf      # fill, then READ the PNGs
```

It is a template to copy and re-coordinate, not a generic filler: **every coordinate in it was measured from one specific blank and will be wrong for yours.** Start by running the inspect mode against your own form and replacing the numbers and the checkbox names with what it prints.

## Delivery

- The flattened PDF is the deliverable. State explicitly what is left for the pen: date, signatures, and where they go.
- If you attach it to a message, attach it **by file path**. Never hand-transcribe a PDF as inline base64 into a tool call: a 100 KB file cannot be reproduced byte-perfect by hand, and a corrupted attachment on an official document is unacceptable.
- Keep filled forms out of version control. A filled form carries personal data, and a bank mandate carries an account number.

## Prior art (checked 2026-09)

Complements, does not replace, the **pdf** skill in [`anthropics/skills`](https://github.com/anthropics/skills/tree/main/skills/pdf) (`SKILL.md`, `forms.md`, `reference.md`, `scripts/`, `LICENSE.txt`). That is the better tool for general PDF work and for ordinary form filling: it extracts per-field metadata including each checkbox's own `checked_value` and `unchecked_value`, handles radio groups and choice fields, covers multi-page rects, and has a non-fillable coordinate-overlay route with a validation image. **Read its `LICENSE.txt` before reusing anything from it:** those materials are all-rights-reserved and separately forbid derivative works, a stricter grant than this repo's Apache-2.0. Nothing here is derived from them; where the two skills agree, they agree because the PDF format forces the same answer.

The other public option, [`claude-office-skills/pdf-form-filler`](https://github.com/claude-office-skills/skills), is a guidance document rather than a method: it lists candidate libraries (pdf-lib, PyPDF2, iText, PDFBox) and its own Limitations section states that it "cannot execute actual form filling".

## Status

Technique-and-reference skill, not a rule set, so there is nothing here to pressure-test. It was worked out and verified against a real government comb-box Vordruck, with the result checked in the raster rather than assumed, and the gotchas table is the residue of the attempts that failed. Retest it by applying it to a new form and reading the render.
