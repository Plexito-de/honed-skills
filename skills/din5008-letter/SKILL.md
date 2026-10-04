---
name: din5008-letter
description: Use when a German letter (Brief, Geschäftsbrief) has to be printed and posted in a DIN lang window envelope (Fensterumschlag), laid out to DIN 5008 Form B, such as a Kündigung, Widerspruch, Einspruch or a letter to a Behörde. Needs only sender and recipient; every optional part (Rücksendeangabe, Vermerk, Informationsblock, Datum, Betreff, Anrede, Text, Gruß, Anlagen) goes to its normed place, with Falz- and Lochmarken. Refuses an address the window cannot take and proves the PDF layout. Not for Form A or a logo letterhead.
license: Apache-2.0
compatibility: Requires Python 3.10+ (standard library only), Typst 0.15.1+ (one binary, https://typst.app) and pdftotext from poppler, which measures the finished PDF. No network, no account. Agent-agnostic.
metadata:
  version: "1.0.0"
  changeSummary: First release. Form B letter to PDF from plain data, with Falz- and Lochmarken, refusals and a layout proof.
  isBreaking: "false"
---

# DIN 5008 letter: Form B to a print-ready PDF, proven from the layout

A letter in a window envelope fails in the post room, not on screen. An address one line too low
slides under the window edge, a seventh line is cut off, and a printer set to "fit to page" moves
everything by a few millimetres. So this skill renders the PDF, reads back where each part starts,
compares that with numbers taken from the norm, and keeps the file only when they agree.

You give it two things: the sender and the recipient. That alone is a valid letter. Everything else
is optional, and whatever you give goes to the place DIN 5008:2020-03 Form B reserves for it.

![Page one of a DIN 5008 Form B letter: the sender right-aligned in the header, a one-line return address and the note "Einschreiben" above a five-line recipient address on the left, an information block with Ihr Zeichen, Ihre Nachricht vom, E-Mail and Datum on the right, a bold subject line, salutation, two short paragraphs, the closing, a signature gap, the signer, an enclosure and a distribution note, and three short marks on the left edge for folding and punching](../../docs/assets/din5008-letter-example.png)

The picture lives in the repository's `docs/assets/`, so this folder stays text-only. If you copied
the folder alone, run `scripts/make_example_image.py` and it draws the picture into your current
folder. Every name, address, date and number in it is invented.

## Install

```bash
# macOS
brew install typst poppler
# Debian, Ubuntu: apt install poppler-utils fonts-liberation, and Typst from
#   https://github.com/typst/typst/releases (distribution packages are often older than 0.15.1)
# Windows: Typst from the same releases page, and a poppler build that puts pdftotext on PATH
python3 scripts/letter.py --selftest
```

`scripts/` is relative to the folder that holds this file. An agent runs from the user's project, so
it calls the script by its full path.

The selftest runs 72 cases and must print `selftest: PASS`. It needs one of the sans fonts Arial,
Helvetica, Liberation Sans, Arimo or DejaVu Sans; pass `--font NAME` to `--selftest` to use another
sans that `--help` lists. DIN 5008 20.7.1 asks for a sans below 10 pt, and the Rücksendeangabe is 8
pt. Widths are measured in the font that is used, so a wider font such as DejaVu Sans refuses a long
address line sooner than Arial does, and the page-break and column-width cases of the selftest are
calibrated for Arial metrics, so with another font they report a skip. Every character is set in
that one font: a character it lacks is refused, never borrowed from another font. The selftest
passes with Arial, Helvetica, Helvetica Neue and Verdana on macOS; other fonts are untested.

## Write a letter

The smallest letter. A literal `\n` in a flag is a line break. Most letters want a date too:

```bash
python3 scripts/letter.py \
  --sender 'Erika Mustermann\nBeispielweg 7\n12345 Musterstadt' \
  --recipient 'Beispiel GmbH\nMusterallee 12\n54321 Beispielstadt' \
  --date today --out brief.pdf --png brief.png
```

A full letter is easier as JSON (`--json letter.json --out brief.pdf`). On Windows, use JSON: the
shell quoting above is POSIX. A UTF-8 file with a byte-order mark is accepted.

```json
{
  "sender": "Erika Mustermann\nBeispielweg 7\n12345 Musterstadt",
  "recipient": "Beispiel GmbH\nKundenservice\nHerrn Max Beispiel\nMusterallee 12\n54321 Beispielstadt",
  "notes": ["Einschreiben"],
  "your_ref": "AB-123",
  "your_message": "1. März 2030",
  "email": "erika@example.com",
  "date": "today",
  "subject": "Kündigung meines Vertrags Nr. 0815",
  "salutation": "Sehr geehrter Herr Beispiel,",
  "body": "hiermit kündige ich meinen Vertrag zum nächstmöglichen Termin.\n\nBitte bestätigen Sie mir die Kündigung schriftlich.",
  "closing": "Mit freundlichen Grüßen",
  "enclosures": ["Kopie des Vertrags"],
  "cc": ["Steuerberaterin"]
}
```

The JSON keys are the text flags with underscores. Six flags differ: `--note`, `--enclosure` and
`--cc` repeat and become the lists `notes`, `enclosures` and `cc`; `--info LABEL=VALUE` becomes
`info: [["LABEL", "VALUE"]]`; `--no-return-line` and `--no-signer` become `return_line: false` and
`signer: false`. `--body-file`, `--font`, `--out`, `--png` and `--force` have no key.
`python3 scripts/letter.py --help` lists every key and every exit code.

A letter to a Behörde usually carries its file number (Aktenzeichen, Steuernummer) in the
Informationsblock, where the reader looks for it. A subject may run over two lines:

```json
{
  "sender": "Erika Mustermann\nBeispielweg 7\n12345 Musterstadt",
  "recipient": "Finanzamt Musterstadt\nPostfach 12 34\n12345 Musterstadt",
  "info": [["Aktenzeichen", "AZ-2030-0815"]],
  "date": "today",
  "subject": "Einspruch gegen den Bescheid vom 1. März 2030\nfür das Jahr 2029",
  "salutation": "Sehr geehrte Damen und Herren,",
  "body": "gegen den oben genannten Bescheid lege ich Einspruch ein.",
  "closing": "Mit freundlichen Grüßen"
}
```

An address abroad ends with the place in the form of the destination country and the country in
German, both in capital letters (20.7.3), for example
`"Jane Doe\n10 Example Street\nLONDON SW1A 1AA\nVEREINIGTES KÖNIGREICH"`.

A company name after the Gruß follows it after one blank line (20.12). Write the closing as two
paragraphs: `"closing": "Mit freundlichen Grüßen\n\nBeispiel e. V."`.

Look at the PNG before you print: `--png brief.png` writes `brief.png`, or `brief-1.png`,
`brief-2.png` and so on for a longer letter. Then print at **100 % / Actual size**, never "fit to
page". An existing PDF or preview is never replaced unless you pass `--force`. With `--force`,
previews of an earlier, longer version (`brief-3.png` when the letter now has two pages) are
removed, so no stale page lies next to the new ones.

The tool prints your words as you give them. The conventions for the wording are yours to keep: no
"Betreff:" label and no full stop after the Betreff, no punctuation after the Gruß (20.9, 20.11).
The proof checks the geometry, not the wording.

## Where each part goes

Positions are millimetres from the top-left corner of the A4 sheet, to the top of the line. The
grid is the norm's 4,23 mm line (12 pt), so line n starts at (n - 1) x 4,23 mm. A row marked
*choice* is this template's decision where the norm leaves room.

| Key | Part | Position | Basis |
| --- | --- | --- | --- |
| `sender` (required) | Absender, first line is the name | header, right-aligned to 190 mm, ending 5 mm above the Anschriftfeld (*choice*) | 19.4.2, E.8 |
| `return_line` | Rücksendeangabe, 8 pt, one line | x 25, line 13 (50,8 mm). Default: the sender up to the postcode line, joined with commas, set down to 6 pt or left out when it does not fit. `false` or `--no-return-line` drops it | 19.4.3, B.1 |
| `notes` | Zusätze und Vermerke, up to 2 lines | lines 14 and 15, filled from the bottom (the lower at 59,2 mm) | 20.7.1, B.1 |
| `recipient` (required) | Anschrift, up to 6 lines, 80 mm wide | x 25, from line 16 (63,5 mm) | 20.7, A.1, B.1 |
| `your_ref`, `your_message`, `our_ref`, `our_message` | Bezugszeichen | Informationsblock, x 125 to 200, from line 13 | 19.5, A.1 |
| `name`, `phone`, `fax`, `email`, `info` | contact lines, plus any `[label, value]` pairs | same block, after a blank line | 19.5 |
| `date` | Datum, as given, or `today` for "4. Oktober 2026" | last line of the block, after a blank line | 19.5 |
| (Informationsblock values) | 11 pt; one long token such as an e-mail address down to 8 pt to fit its column, and if even that is too wide it wraps after "@", "." or a hyphen; never hyphenated. Labels up to 40 mm | | 20.8 |
| `subject` | Betreff, bold, may run over lines | two blank lines below the lower of the address zone (line 21) and the Informationsblock: line 24 (97,4 mm) or lower | 20.9.2 |
| `salutation` | Anrede | two blank lines after the Betreff | 20.9.2 |
| `body` | Text, ragged right; a blank line starts a paragraph | one blank line after the Anrede | 20.9.4, 20.10 |
| `closing` | Gruß; a blank line, then a company name, if any | one blank line after the text | 20.11, 20.12 |
| `signer` | Unterzeichner. Default: the sender's name when there is a `closing`; `false` or `--no-signer` for none | three blank lines below the Gruß, room for the signature (*choice*: 20.13 leaves the number to need) | 20.13 |
| `enclosures`, `cc` | Anlage(n), Verteiler | one blank line below the signer, so at least three below the Gruß | 20.15 |
| (always) | Falzmarken and Lochmarke | 105 mm, 210 mm and 148,5 mm, from x 5 mm, on every page | Bild 9, 19.8 |
| (2+ pages) | "Seite x von y" | bottom right, on every page (*choice*: the norm numbers from page 2) | 20.17 |

The line from 63,5 mm is the line box. The ink of a capital letter starts about 0,4 mm lower.

The text starts on page 1, and when the Gruß is on a later page, at least two lines of the text
stand above it (20.10). The norm asks for a complete paragraph of two lines; this template counts
two lines (*choice*), because the strict reading forces an ordinary one-paragraph letter onto an
empty first page. The Gruß and the signer always stay together, and the Anlagen start on their page;
a long list continues on the next. The tool lays the letter out normally first. When the measured
page breaks leave fewer than two lines above the Gruß, it lays it out again with the last few lines
of the text kept with the Gruß, trying only cuts whose line before does not end in a hyphenated
word. The lines are cut from the end of the text at a line break you typed, or between two words
where Typst breaks the line anyway, and never so that a single line stays behind. A kept layout is
used only when its printed lines are exactly those of the normal layout, read back from both PDFs,
so a cut never shows.

The Zusatz- und Vermerkzone is drawn on the 12 pt grid of Tabelle B.1, which gives it three lines:
the Rücksendeangabe and two Vermerke. The norm also allows more lines in a smaller type; this
template does not implement that.

The length and the stroke of the marks are not normed: they are 5 mm and 8 mm long at 0,5 pt, and
they start 5 mm in, because most printers cannot print the outer 4 mm.

## What it refuses (exit 1)

| Input | Why |
| --- | --- |
| No `sender` or no `recipient` | the only two required parts |
| More than 6 address lines | the Anschriftzone has six (20.7.1). Combine lines, or drop the country on a domestic letter |
| An address line wider than 80 mm | the writing limit of the address field (A.1). The message gives the measured width |
| More than 2 Vermerk lines | two fit beside the Rücksendeangabe on the 12 pt grid (see above) |
| A `return_line` you gave that is wider than 80 mm at 8 pt | shorten it, for example to name, street and place |
| A sender taller than 35 mm, or a sender line wider than 165 mm | the header ends 5 mm above the Anschriftfeld |
| An Informationsblock below grid line 40 | it would leave no room for the letter |
| A word wider than its line, such as a long URL, or an Informationsblock value with no break point that is too wide even at 8 pt | Typst does not break it, so it would run off its area. Measured in the finished PDF: right of 190 mm in the body, 200 mm in the Informationsblock, 105 mm in the address field |
| An Informationsblock label wider than 40 mm | it would squeeze the values of every row |
| A Gruß and Unterzeichner of more than 20 lines | they stay together on one page |
| Page breaks that no layout can fit to 20.10 | split a paragraph with a blank line (see above) |
| A character the font has no glyph for | it would print as an empty box |
| A control, bidi or invisible formatting character, or more than 2 combining marks on one letter | it prints other than it reads (a right-to-left override turns `1234` into `4321`). Windows line endings, tabs (printed as a space) and decomposed umlauts are accepted |
| A line break in a one-line field, a value of the wrong type, an unknown key | a typo such as `betreff` would otherwise vanish silently |

Nothing is shrunk or cut to make it fit, with two exceptions the norm allows: the Rücksendeangabe
the tool builds itself from a long sender (down to 6 pt, 19.4.3), and a long Informationsblock
value (down to 8 pt, 20.8). A letter that quietly loses half an address line is worse than one
that is refused.

Other exit codes: 2 for usage (flags, unreadable or unwritable files, JSON syntax, an existing
output, a `--font` that is not a listed sans), 3 for the environment (Typst missing or older than
0.15.1, pdftotext missing, no sans font installed), 4 when the proof fails or the tool itself
fails unexpectedly.

## How the PDF is proven (exit 4 when it fails)

1. The template drops a marker at the top-left of every part and of every Informationsblock row,
   with the number of grid lines the part takes. `typst eval` reads them back as millimetres on the
   sheet.
2. `letter.py` compares them with constants it holds itself, taken from the norm and not from the
   template. Every part you gave must be on the page, every Informationsblock row too. The sender
   sits in the header, the address on lines 16 to 21 at x 25, the Rücksendeangabe on line 13, the
   Informationsblock at x 125 from line 13. The marks are at 105, 148,5 and 210 mm on every page.
   The body opens exactly two blank lines below the lower of the address zone and the
   Informationsblock. Between two parts on one page the blank lines are counted from the last line
   of the first: two after the Betreff, one after the Anrede and after each paragraph, three after
   the Gruß, one after the signer and after the Anlagen. The text starts on page 1, and a letter of
   two or more pages is numbered on every page.
3. The PDF itself is read for placeholders, with no other tool: glyph 0 (the empty box a font
   draws for a character it lacks) or the LastResort font macOS substitutes. Typst warns about
   neither, and the text layer still carries the right character.
4. pdftotext reads every drawn word back with its box: nothing past the right edge of its area,
   nothing in the bottom margin except "Seite n von total", every line of the body starting on the
   Fluchtlinie at 25 mm, the sender right-aligned to 190 mm, the word at the start of every address
   line inside its line, two lines of the text above a Gruß on a later page, every character of the
   input in the text, and every one-line value (an address line, a reference, an Anlage, a
   Rücksendeangabe you gave) found in the text, spaces and hyphens aside.
5. A Typst warning fails the run too, because a warning means the layout did not settle.

The markers give where each part starts and how many lines it takes. Where the text ends, on the
right and at the bottom, is measured by step 4 in the finished PDF.

The letter goes to Typst as a file in a private temporary folder, not on the command line. The PDF
and the previews are moved into place only after all of this passes, each through a new file in the
target folder: renamed over the target with `--force`, and linked to it without, so a file that
appeared meanwhile is never overwritten. A volume without hard links (FAT, exFAT, many network
shares) gets the file created exclusively instead: still never an overwrite, though not atomic. Output files get the usual permissions of your umask. The PDF's own metadata says
only "Brief": no name, no subject.

## Print, fold, post

- Print at 100 % on plain A4. The proof cannot see the printer: measure the first printout once.
  The first address line starts 63,5 mm from the top edge and 25 mm from the left edge.
- Fold the bottom part up along the lower mark (210 mm). Then fold the top part backwards along the
  upper mark (105 mm), so the address stays on the outside. The folded sheet is 105 mm high, the
  size of a DIN lang envelope.
- The Lochmarke at 148,5 mm is the middle of the sheet, for a two-hole punch.
- "Einschreiben" in `notes` prints the word. The Post still needs its own label or stamp.

## Lessons from building it

Each of these cost a wrong layout first.

- **Secondary summaries of DIN 5008 disagree, and the Anschriftfeld is where it shows.** Some give
  the Form B field's top as 50 mm. It is 45 mm: the 45 mm header plus the 17,7 mm Vermerkzone is
  62,7 mm, which puts the first address line at 63,5 mm (Tabelle B.1). Check a number against a
  second source before you build on it.
- **Typst's `leading` is the gap between lines.** To hold the 4,23 mm grid, set the text's
  `top-edge` and `bottom-edge` so every line box is exactly 12 pt, and set `leading` to 0. Then a
  blank line is one grid line, for any font size.
- **A marker inside a paragraph reports the baseline; at the start of a block it reports the line
  top.** The difference is the top edge, 9 pt here. Every measured part is therefore its own block.
- **A bare `context` in markup opens a paragraph and pushes the flow down by one line box.** Spacing
  computed in a context belongs in a `block`.
- **`v()` adds to the block spacing.** "Three blank lines" is the block's one plus `v(2 lines)`.
- **An unbreakable block loses text, and a sticky block moves whole.** An unbreakable last
  paragraph ran past the paper edge with no error. `block(sticky: true)` lets it break, but Typst
  moves a sticky block to the next page whole when it does not fit, which emptied page 1. Several
  rounds of rules for what to make sticky each failed on a new case. What held: cut the text so
  that only its last few lines stick to the Gruß, and judge the result by measuring it.
- **A cut between words is not invisible on its own.** Ragged-right text breaks greedily, so a
  prefix usually breaks where the whole paragraph does. It does not when the line ends in a
  hyphenated word, and a cut that ignores typed line breaks tore an IBAN across two pages. The
  fix is not a smarter cut: compare the printed lines of the cut layout with those of the plain
  layout, both read back from the PDF, and use the cut only when they are identical.
- **A right-aligned line can end past its edge.** Typst lets a closing full stop hang into the
  margin by default (`overhang`), so a sender named "e.V." ended at 191 mm. Turn it off where the
  edge is measured.
- **Guessing where Typst breaks a line is a losing game.** A word-width check that split on
  whitespace missed a narrow no-break space and refused compounds that Typst hyphenates. Measuring
  the right edge of every drawn word in the finished PDF has neither problem.
- **Text extraction cannot see a missing glyph.** The text layer of the PDF carries the character
  even when the page shows an empty box, so the check reads the glyph codes.
- **Only `\n` is safe to count lines by.** A carriage return, a vertical tab or a Unicode line
  separator also starts a new line in the output, so a nine-line address split by carriage returns
  passed a "six lines" check. Convert Windows line endings, and refuse the rest.
- **The proof must hold its own constants.** A proof that reads its expected values from the
  template only proves the template agrees with itself.

## Limits

- Form B only. Form A (27 mm header) differs in every vertical position and is not implemented.
- The header is plain text. For a logo or a designed letterhead, use a Typst library such as
  letter-pro and write the letter in markup.
- The body is plain text: paragraphs and line breaks, no bold or lists. The norm's Teilbetreff and
  tables are not supported.
- Folgeseiten repeat no header. The Datum always goes in the Informationsblock, even alone, which
  19.5 allows.
- Nothing stops a print dialog from scaling the page. Measure the first printout.
- What the proof does not cover: that each value is printed is checked across the whole document,
  not at its own place; the fold and hole marks are proven by their markers, not found in the PDF;
  the rule that the last page carries two text lines looks at the page with the Gruß.
- This skill is not affiliated with DIN. "DIN 5008" names the standard the layout follows.

## Prior art (as of October 2026)

- [letter-pro](https://github.com/Sematre/typst-letter-pro) (Typst, MIT): a library you write a
  letter in by hand, Form A and B, with fold and hole marks. The address and Informationsblock sit in
  a fixed 45 mm row and the Betreff follows at a fixed offset, so a tall Informationsblock and the
  two-blank-line rule are left to you, and it does not measure the result. Use it when you want to
  write Typst yourself, need Form A, or want a logo in the header.
- [typst-din-5008-letter](https://github.com/ludwig-austermann/typst-din-5008-letter) (Typst, "DIN
  5008 inspired") and KOMA-Script `scrlttr2` with the `DIN` letter class option (LaTeX): templates
  in the same category, for people who write markup.
- What this skill adds: plain data in, two required fields, refusals at the window's limits, and a
  PDF that is kept only when its measured layout matches the norm and the template choices marked
  above.
