#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file of this skill.
"""Write a DIN 5008 Form B letter as a print-ready PDF, and prove where every part landed.

Purpose: only the sender and the recipient are required. Every optional part that is given goes
    to its place on DIN 5008:2020-03 Form B (tiefgestelltes Anschriftfeld), with Falzmarken at 105
    and 210 mm and the Lochmarke at 148,5 mm. Input the window or the layout cannot take is refused,
    never shrunk. The PDF is kept only after its layout was measured against the norm.
Usage:
    python3 letter.py --sender "Name\\nStraße 1\\n12345 Ort" --recipient "..." --out brief.pdf [--png brief.png]
    python3 letter.py --json letter.json --out brief.pdf
    python3 letter.py --selftest
    python3 letter.py --help        (every flag, the JSON keys and the exit codes)
Needs Python 3.10+, Typst 0.15.1+ and pdftotext (poppler), which measures the finished PDF.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

TEMPLATE = Path(__file__).resolve().with_name("letter.typ")
FONTS = ("Arial", "Helvetica", "Liberation Sans", "Arimo", "DejaVu Sans")
# --font must name a sans text font, 20.7.1. The default list comes first.
SANS_FONTS = FONTS + ("Noto Sans", "Open Sans", "Roboto", "Source Sans 3", "Inter", "Fira Sans",
                      "IBM Plex Sans", "Lato", "Calibri", "Verdana", "Segoe UI", "Helvetica Neue")
# Fonts with the metrics of Arial; the selftest pagination cases are calibrated for them.
ARIAL_METRICS = ("Arial", "Helvetica", "Liberation Sans", "Arimo")
MIN_TYPST = (0, 15, 1)  # first release with `typst eval` that exits non-zero on a failed expression
MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
          "Oktober", "November", "Dezember")
MAX_INPUT = 200_000     # characters of letter data; a letter, not a book

# The numbers of the norm, written here independently of letter.typ so that the proof can catch
# the template drifting. DIN 5008:2020-03 Anhang A (Tabelle A.1), Anhang B (Tabelle B.1), Bild 9.
PITCH = 25.4 * 12 / 72          # 4,23 mm: one grid line
TOL = 0.3                       # mm
# The top of a pdftotext word box is the ascender of the font, which can reach about 0,6 mm above
# the line top (Helvetica Neue). Word boxes are compared with line tops allowing this much.
ASCENT = 1.0                    # mm
PAGE_H = 297.0
LEFT = 25.0                     # Fluchtlinie
ADDRESS_RIGHT = 105.0           # writing limit of the address field, 25 + 80 mm (A.1)
BODY_RIGHT = 190.0              # text edge of the body, 20 mm from the right edge of the sheet
INFO_X, INFO_RIGHT = 125.0, 200.0
TOP, BOTTOM = 20.0, PAGE_H - 25.0  # the text area of this template, on every page
RETURN_LINE, NOTES_LAST, ADDRESS_FIRST, INFO_FIRST = 13, 15, 16, 13   # grid lines
ADDRESS_LAST = ADDRESS_FIRST + 5
MARKS = {"fold-1": 105.0, "hole": 148.5, "fold-2": 210.0}
MARK_X = 5.0
# Blank lines after each body part, as the norm asks (20.9.2, 20.9.4, 20.10, 20.15) or as this
# template chooses (three for the signature: 20.13 leaves the number to need).
BLANK_AFTER = {"subject": 2, "salutation": 1, "paragraph": 1, "closing": 3, "signer": 1,
               "enclosures": 1}
# The layouts after the plain one keep the last k lines of the text with the Gruß. Only values of k
# whose cut does not fall after a hyphenated line are tried, at most this many.
MAX_KEEP_TRIES = 8
FLOW = "flow: "                 # prefix of a proof error that another layout may avoid

ZERO_WIDTH_SPACE = chr(0x200B)  # a break point the template adds inside long Informationsblock values
SOFT_HYPHEN = chr(0xAD)  # allowed in text as a hyphenation hint; it prints only at a line break
MAX_COMBINING = 2       # Vietnamese needs two on one letter; more is a smear, not a language
RTL = ("R", "AL")       # bidi classes of right-to-left letters

# The PDF syntax missing_glyphs() reads: streams and the text objects inside them. A stream ends
# just before "endstream"; the line break in between is left for zlib to ignore, because a
# compressed stream may itself end in a carriage-return byte.
STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.S)
TEXT_OBJECT = re.compile(rb"\bBT\b(.*?)\bET\b", re.S)
# The first bytes of an embedded font program: TrueType, OpenType, collection, WOFF, bare CFF.
FONT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf", b"wOFF", b"\x01\x00\x04\x02",
              b"\x01\x00\x04\x03", b"\x01\x00\x04\x04")
PDF_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
BACKSLASH = ord("\\")

TEXT = {"sender", "recipient", "your_ref", "your_message", "our_ref", "our_message", "name", "phone",
        "fax", "email", "date", "subject", "salutation", "body", "closing"}
LISTS = {"notes", "enclosures", "cc"}
KEYS = TEXT | LISTS | {"return_line", "signer", "info"}
MULTILINE = {"sender", "recipient", "subject", "body", "closing", "signer"}
INFO_KEYS = ("your_ref", "your_message", "our_ref", "our_message", "name", "phone", "fax", "email",
             "date")
BODY_ORDER = ("subject", "salutation", "body", "closing", "signer", "enclosures", "cc")
EPILOG = """\
JSON keys (--json): sender, recipient (required, lines separated by newlines); return_line (text,
or false to drop it); notes, enclosures, cc (lists of one-line texts); your_ref, your_message,
our_ref, our_message, name, phone, fax, email, date; info (list of [label, value]); subject,
salutation, body (a blank line starts a paragraph), closing; signer (text, or false for none).
In flags, a literal \\n is a line break. --note, --enclosure, --cc and --info repeat.

Exit codes: 0 written and proven; 1 refused (the input: the norm, the window or the layout cannot
take it); 2 usage (flags, unreadable or unwritable files, JSON syntax, an existing output, a --font
that is not a listed sans); 3 environment (Typst missing or too old, pdftotext missing, no sans
font installed); 4 the layout proof failed, or the tool itself failed unexpectedly.
"""


class Refused(Exception):
    """Input the norm, the envelope window or this layout cannot take (exit 1)."""


class UsageError(Exception):
    """Wrong flags, unreadable files, malformed JSON, an output that would be overwritten (exit 2)."""


class Environment(Exception):
    """Something to install: Typst, a newer Typst, pdftotext, a sans font (exit 3)."""


class ProofFailed(Exception):
    """The PDF does not sit where the norm puts it, or Typst failed (exit 4)."""


def line_top(n: int) -> float:
    return (n - 1) * PITCH


def lines(s: str) -> list[str]:
    return [l.strip() for l in s.split("\n") if l.strip()]


def german_date(day: dt.date) -> str:
    return "%d. %s %d" % (day.day, MONTHS[day.month - 1], day.year)


def bad_char(s: str) -> str | None:
    """The first character that would print other than it reads: controls, bidi and format marks,
    line separators. A newline is allowed, and a soft hyphen as a hyphenation hint."""
    for ch in s:
        if ch in ("\n", SOFT_HYPHEN):
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            return "U+%04X" % ord(ch)
    return None


def combining_run(s: str) -> int:
    """The longest run of combining marks (categories Mn and Me) on one base letter."""
    longest = run = 0
    for ch in s:
        if ch == SOFT_HYPHEN:
            continue  # invisible, so it does not end a run of marks on one letter
        run = run + 1 if unicodedata.category(ch) in ("Mn", "Me") else 0
        longest = max(longest, run)
    return longest


def check_text(key: str, s: str) -> str:
    # Windows line endings and tabs, as pasted from a word processor, become newlines and spaces.
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ").strip()
    bad = bad_char(s)
    if bad:
        raise Refused("%s contains %s, a control or formatting character that would print other "
                      "than it reads; remove it" % (key, bad))
    if combining_run(s) > MAX_COMBINING:
        raise Refused("%s stacks more than %d combining marks on one letter, which prints as a smear "
                      "over the line above" % (key, MAX_COMBINING))
    if "\n" in s and key not in MULTILINE:
        raise Refused("%s must be one line" % key)
    return s


def postal_lines(sender: str) -> list[str]:
    """The sender up to the postcode line, so the default Rücksendeangabe leaves out a phone number
    or an e-mail address that follows it. A sender with no postcode line is used whole."""
    ls = lines(sender)
    for i, line in enumerate(ls):
        if re.match(r"(?:[A-Z]{1,2}-)?\d{4,5}\s", line):
            return ls[:i + 1]
    return ls


def normalise(data: dict, today: dt.date | None = None) -> dict:
    """Check every key, type and character, fill the defaults, and return what the template reads."""
    if not isinstance(data, dict):
        raise Refused("the letter data must be a JSON object")
    unknown = set(data) - KEYS
    if unknown:
        shown = ", ".join(repr(k)[:40] for k in sorted(map(str, unknown))[:10])
        raise Refused("unknown keys: " + shown + ". Known: " + ", ".join(sorted(KEYS)))
    if len(json.dumps(data, ensure_ascii=False)) > MAX_INPUT:
        raise Refused("the letter data is longer than %d characters" % MAX_INPUT)
    out: dict = {}
    for k, v in data.items():
        if v is None or v == "" or v == []:
            continue
        if k in ("return_line", "signer") and v is False:
            out[k] = False
        elif k in TEXT or k in ("return_line", "signer"):
            if not isinstance(v, str):
                raise Refused("%s must be a text%s" % (k, " or false" if k in ("return_line", "signer") else ""))
            if text := check_text(k, v):
                out[k] = text
        elif k in LISTS:
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise Refused("%s must be a list of texts" % k)
            if items := [check_text(k, x) for x in v if x.strip()]:
                out[k] = items
        elif k == "info":
            if not isinstance(v, list) or not all(
                    isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p) for p in v):
                raise Refused("info must be a list of [label, value] pairs of texts")
            out[k] = [[check_text("info", a), check_text("info", b)] for a, b in v]
    for k in ("sender", "recipient"):
        if not out.get(k):
            raise Refused("%s is missing: sender and recipient are required" % k)
    n = len(lines(out["recipient"]))
    if n > ADDRESS_LAST - ADDRESS_FIRST + 1:
        raise Refused("the Anschriftzone has 6 lines and the address has %d: combine lines, or drop "
                      "the country line on a domestic letter" % n)
    if len(out.get("notes", [])) > NOTES_LAST - RETURN_LINE:
        raise Refused("at most 2 lines of Zusätze und Vermerke fit beside the Rücksendeangabe on the "
                      "12 pt grid, not %d" % len(out["notes"]))
    if out.get("date", "").lower() in ("today", "heute"):
        out["date"] = german_date(today or dt.date.today())
    if "return_line" not in out:
        out["return_line"] = ", ".join(postal_lines(out["sender"]))
        out["return_line_auto"] = True
    if "closing" in out and "signer" not in out:
        out["signer"] = lines(out["sender"])[0]
    return {k: v for k, v in out.items() if v is not False}


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """The one place a program is started: typst or pdftotext, as an argument list, no shell."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              cwd=cwd)
    except OSError as e:
        raise Environment("cannot run %s: %s" % (cmd[0], e)) from None


def which(name: str) -> str | None:
    """A program on PATH. On Windows, never one picked up from the current folder, which Python
    before 3.12 searched first."""
    exe = shutil.which(name)
    if exe and os.name == "nt" and Path(exe).resolve().parent == Path.cwd().resolve():
        return None
    return exe


def tools() -> tuple[str, str]:
    """Typst and pdftotext, or the reason they cannot be used."""
    exe = which("typst")
    if not exe:
        raise Environment("typst is not installed: https://github.com/typst/typst/releases")
    version = run([exe, "--version"]).stdout
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    found = tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    if found < MIN_TYPST:
        raise Environment("Typst %s or later is needed, found: %s"
                          % (".".join(map(str, MIN_TYPST)), version.strip() or None))
    pdftotext = which("pdftotext")
    if not pdftotext:
        raise Environment("pdftotext is not installed: it measures the finished PDF. Install "
                          "poppler (brew install poppler, apt install poppler-utils)")
    return exe, pdftotext


def pick_font(exe: str, wanted: str | None) -> str:
    """The font asked for, or the first installed of FONTS. DIN 5008 20.7.1 asks for a sans below
    10 pt, and the Rücksendeangabe is 8 pt, so --font takes only a known sans text font: a symbol
    font such as Webdings would print the whole letter as pictograms and still pass the proof."""
    installed = set(run([exe, "fonts"]).stdout.splitlines())
    if wanted:
        if wanted not in SANS_FONTS:
            raise UsageError("--font takes one of these sans fonts: " + ", ".join(SANS_FONTS))
        if wanted not in installed:
            raise Environment("font %s is not installed (see typst fonts)" % wanted)
        return wanted
    for f in FONTS:
        if f in installed:
            return f
    raise Environment("no sans font found, install one of: " + ", ".join(FONTS))


def run_typst(exe: str, work: Path, args: list[str], font: str, keep: int) -> str:
    res = run([exe, *args, "--root", str(work), "--input", "font=" + font,
               "--input", "keep=%d" % keep], cwd=work)
    refusal = re.search(r"DIN5008: (.*?)\"?\s*$", res.stderr, re.M)
    if res.returncode != 0 and refusal:
        raise Refused(refusal.group(1))
    if res.returncode != 0:
        raise ProofFailed("typst failed:\n" + res.stderr.strip())
    if "warning:" in res.stderr:
        raise ProofFailed("typst warned, so the layout is not trustworthy:\n" + res.stderr.strip())
    return res.stdout


def anchors(exe: str, work: Path, font: str, keep: int) -> list[dict]:
    out = run_typst(exe, work, ["eval", "query(<din-anchor>).map(it => it.value)", "--in",
                                str(work / "letter.typ")], font, keep)
    try:
        found = json.loads(out)
    except json.JSONDecodeError:
        raise ProofFailed("typst eval returned no anchor list: " + out[:200]) from None
    if not found:
        raise ProofFailed("typst eval returned no anchors")
    return found


def info_rows(data: dict) -> int:
    return sum(1 for k in INFO_KEYS if k in data) + len(data.get("info", []))


def expected_parts(data: dict) -> list[str]:
    names = ["sender"] + ["recipient-%d" % (i + 1) for i in range(len(lines(data["recipient"])))]
    names += ["note-%d" % (i + 1) for i in range(len(data.get("notes", [])))]
    if info_rows(data):
        names.append("info")
    names += [n for n in BODY_ORDER if n in data]
    return names


def body_sequence(by: dict) -> list[tuple[str, dict]]:
    """The body parts in reading order, each with its first anchor."""
    paragraphs = sorted((n for n in by if n.startswith("paragraph-")), key=lambda n: int(n.split("-")[1]))
    # A paragraph cut for 20.10 has its tail right after its head.
    text = [x for n in ["body", *paragraphs] for x in (n, "tail-of-" + n)]
    order = ["subject", "salutation", *text, "closing", "signer", "enclosures", "cc"]
    return [(n, by[n][0]) for n in order if n in by]


def prove(found: list[dict], data: dict) -> list[str]:
    """Every check the finished layout has to pass. Returns the failures, empty when proven. A
    failure prefixed with FLOW concerns the page breaks, which another layout may avoid."""
    errors: list[str] = []
    by: dict[str, list[dict]] = {}
    for a in found:
        by.setdefault(a["name"], []).append(a)
    pages = max(a["page"] for a in found)
    for name in expected_parts(data):
        if name not in by:
            errors.append("%s: given, but not on the page" % name)
    if data.get("return_line") and not ({"return-line", "return-line-omitted"} & set(by)):
        errors.append("return-line: given, but not on the page")
    if len(by.get("info-row", [])) != info_rows(data):
        errors.append("Informationsblock: %d rows given, %d on the page"
                      % (info_rows(data), len(by.get("info-row", []))))

    def at(name: str, x: float, y: float) -> None:
        hits = [a for a in by.get(name, []) if a["page"] == 1]
        if len(hits) > 1:
            errors.append("%s: %d times on page 1" % (name, len(hits)))
        for a in hits:
            if abs(a["x"] - x) > TOL or abs(a["y"] - y) > TOL:
                errors.append("%s: at (%.2f, %.2f) mm, norm says (%.2f, %.2f)" % (name, a["x"], a["y"], x, y))

    for name, y in MARKS.items():
        on = sorted(a["page"] for a in by.get(name, []))
        if on != list(range(1, pages + 1)):
            errors.append("%s: on pages %s, expected every page 1..%d" % (name, on, pages))
        for a in by.get(name, []):
            if abs(a["y"] - y) > TOL or abs(a["x"] - MARK_X) > TOL:
                errors.append("%s: at %.2f mm on page %d, norm says %s" % (name, a["y"], a["page"], y))
    for a in by.get("sender", []):
        if a["page"] != 1 or not 5.0 - TOL <= a["y"] <= 40.0:
            errors.append("sender: starts at %.2f mm on page %d, outside the header" % (a["y"], a["page"]))
    at("return-line", LEFT, line_top(RETURN_LINE))
    notes = data.get("notes", [])
    for i in range(len(notes)):
        at("note-%d" % (i + 1), LEFT, line_top(NOTES_LAST - (len(notes) - 1 - i)))
    for i in range(len(lines(data["recipient"]))):
        at("recipient-%d" % (i + 1), LEFT, line_top(ADDRESS_FIRST + i))
    at("info", INFO_X, line_top(INFO_FIRST))
    for name, items in by.items():
        if name in MARKS or name == "page-number":
            continue
        for a in items:
            if a["y"] > BOTTOM + TOL:
                errors.append("%s: starts at %.2f mm on page %d, below the text area" % (name, a["y"], a["page"]))
            if a["page"] > 1 and a["y"] < TOP - TOL:
                errors.append("%s: at %.2f mm on page %d, above the text area" % (name, a["y"], a["page"]))

    sequence = body_sequence(by)
    if sequence:
        # The body opens two blank lines below the lower of the Anschriftzone and the
        # Informationsblock (20.9.2); the last line of the block comes from its measured line count.
        last = ADDRESS_LAST
        if "info" in by:
            last = max(last, INFO_FIRST + by["info"][0]["lines"] - 1)
        name, a = sequence[0]
        want = line_top(last + 3)
        if a["page"] != 1:
            errors.append(FLOW + "the text starts on page %d, not on page 1" % a["page"])
        elif abs(a["y"] - want) > TOL:
            errors.append("%s: at %.2f mm, expected grid line %d (%.2f mm), two blank lines below line %d"
                          % (name, a["y"], last + 3, want, last))
    for (a_name, a), (b_name, b) in zip(sequence, sequence[1:]):
        kind = "paragraph" if a_name in ("body",) or a_name.startswith(("paragraph-", "tail-of-")) else a_name
        blank = 0 if b_name == "tail-of-" + a_name else BLANK_AFTER.get(kind, 1)
        if a["page"] == b["page"]:
            gap = (b["y"] - a["y"]) / PITCH - a["lines"]
            if abs(gap - blank) > TOL / PITCH:
                errors.append("%s to %s: %.2f blank lines, expected %d" % (a_name, b_name, gap, blank))
    if "closing" in by and "signer" in by and by["closing"][0]["page"] != by["signer"][0]["page"]:
        errors.append("closing and signer are on different pages")

    # The text starts on page 1. The two lines before the Gruß (20.10) are counted in the PDF itself,
    # by gruss_lines(), because a paragraph that continues onto a page has no marker there.
    if "body" in by and by["body"][0]["page"] != 1:
        errors.append(FLOW + "the text starts on page %d, not on page 1" % by["body"][0]["page"])
    numbered = sorted(a["page"] for a in by.get("page-number", []))
    if pages > 1 and numbered != list(range(1, pages + 1)):
        errors.append("page numbers on pages %s, expected 1..%d" % (numbered, pages))
    if pages == 1 and numbered:
        errors.append("a one-page letter carries a page number")
    return errors


def pdf_words(pdftotext: str, pdf: Path) -> list[list[tuple[float, float, float, float, str]]]:
    """Every drawn word, per page, as (x0, y0, x1, y1, text) in mm from the top-left corner. Fails
    the proof when a page is not A4 (DIN EN ISO 216, 19.2)."""
    res = run([pdftotext, "-bbox", str(pdf), "-"])
    if res.returncode != 0:
        raise ProofFailed("pdftotext failed:\n" + res.stderr.strip())
    try:
        doc = ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", res.stdout))
    except ET.ParseError as e:
        raise ProofFailed("pdftotext output did not parse: %s" % e) from None
    mm = 25.4 / 72
    page_nodes = [p for p in doc.iter() if p.tag.endswith("page")]
    for p in page_nodes:
        size = (float(p.get("width")) * mm, float(p.get("height")) * mm)
        if abs(size[0] - 210.0) > 0.5 or abs(size[1] - 297.0) > 0.5:
            raise ProofFailed("a page is %.1f x %.1f mm, not A4" % size)
    return [[(float(w.get("xMin")) * mm, float(w.get("yMin")) * mm, float(w.get("xMax")) * mm,
              float(w.get("yMax")) * mm, w.text or "") for w in p.iter() if w.tag.endswith("word")]
            for p in page_nodes]


def gruss_lines(pages: list, found: list[dict]) -> str | None:
    """The flow failure, if any, for 20.10: on a later page, at least two lines of the text must
    stand above the Gruß. This template reads the rule as two lines; the norm text asks for a
    complete paragraph of two lines, which an ordinary one-paragraph letter often cannot meet."""
    start = next((a for a in found if a["name"] in ("closing", "signer")), None)
    if start is None or start["page"] == 1:
        return None
    words = pages[start["page"] - 1]
    tops = {round(w[1] / PITCH) for w in words if LEFT - 1 <= w[0] < BODY_RIGHT and TOP - 1 <= w[1] < start["y"] - TOL}
    if len(tops) < 2:
        return FLOW + "only %d line(s) of the text stand above the Gruß on page %d (20.10)" % (len(tops), start["page"])
    return None


def flatten(s: str) -> str:
    """Text without spaces, hyphens and zero-width spaces, so a value wrapped or hyphenated on the
    page still matches."""
    return re.sub(r"[\s\-" + SOFT_HYPHEN + ZERO_WIDTH_SPACE + "]", "", s)


def printed_rows(pages: list, found: list[dict]) -> list[tuple[int, float, str]]:
    """The printed lines of the body in reading order, as (page, top in mm, text)."""
    body_top = min((a["y"] for a in found if a["page"] == 1 and a["name"] in BODY_ORDER), default=PAGE_H)
    out = []
    for n, words in enumerate(pages, 1):
        rows: dict[int, list] = {}
        for w in words:
            if (w[1] >= body_top - ASCENT if n == 1 else w[1] >= TOP - 1) and w[3] <= BOTTOM + TOL:
                rows.setdefault(round(w[1] / PITCH * 2), []).append(w)
        for _, row in sorted(rows.items()):
            row.sort(key=lambda w: w[0])
            out.append((n, row[0][1], " ".join(w[4] for w in row)))
    return out


def text_lines(pages: list, found: list[dict]) -> list[str]:
    """The printed lines of the body, page breaks aside: what a reader sees."""
    return [t for _, _, t in printed_rows(pages, found)]


def keep_candidates(pages: list, found: list[dict]) -> list[int]:
    """How many lines to keep with the Gruß, from the plain layout: k lines are cut after printed line
    n - k, and that line must not end in a hyphen, or the cut layout would break the text otherwise."""
    start = next((a for a in found if a["name"] in ("closing", "signer")), None)
    if start is None:
        return []
    before = [t for page, top, t in printed_rows(pages, found) if (page, top) < (start["page"], start["y"] - TOL)]
    good = [k for k in range(2, len(before)) if not before[-k - 1].endswith((SOFT_HYPHEN, "-"))]
    return good[:MAX_KEEP_TRIES]


def prove_glyphs(pages: list, data: dict, found: list[dict]) -> tuple[str, list[str], list[str]]:
    """Independent of Typst: check where every drawn word is. Returns a summary, the words that
    run off their area (input to refuse), and the failures of the layout itself."""
    if not pages or not pages[0]:
        raise ProofFailed("pdftotext found no words on page 1")
    body_top = min((a["y"] for a in found if a["page"] == 1 and a["name"] in BODY_ORDER), default=PAGE_H)
    overflow, errors = [], []
    for n, words in enumerate(pages, 1):
        # Below the text area only the page number may stand, exactly "Seite n von total".
        low = [w for w in words if w[3] > BOTTOM + TOL]
        footer = " ".join(w[4] for w in sorted(low, key=lambda w: w[0]))
        if low and not (len(pages) > 1 and footer == "Seite %d von %d" % (n, len(pages))):
            overflow.append("page %d has text in the bottom margin: %s" % (n, footer[:80]))
        for x0, y0, x1, y1, t in words:
            in_info = n == 1 and x0 >= INFO_X - TOL and y0 < body_top
            in_address = n == 1 and line_top(RETURN_LINE) - TOL <= y0 < line_top(ADDRESS_LAST + 1) and x0 < INFO_X
            right = INFO_RIGHT if in_info else ADDRESS_RIGHT if in_address else BODY_RIGHT
            if x1 > right + TOL:
                overflow.append("%s on page %d runs to %.1f mm, past the edge at %.0f mm"
                                % (t[:40], n, x1, right))
    # Every line of the body starts on the Fluchtlinie: the leftmost word of each line, measured.
    for n, words in enumerate(pages, 1):
        body = [w for w in words if (w[1] >= body_top - ASCENT if n == 1 else w[1] >= TOP - 1) and w[3] <= BOTTOM + TOL]
        starts = {}
        for w in body:
            key = round(w[1] / PITCH * 2)
            starts[key] = min(starts.get(key, PAGE_H), w[0])
        for x in starts.values():
            if abs(x - LEFT) > 0.5:
                errors.append("a line on page %d starts at x %.1f mm, not on the Fluchtlinie at %s mm" % (n, x, LEFT))
                break
    # Nothing stands between the Anschriftfeld and the text, left of the Informationsblock.
    for x0, y0, x1, y1, t in pages[0]:
        if line_top(ADDRESS_LAST + 1) + TOL < y0 < body_top - ASCENT and x0 < INFO_X - TOL:
            errors.append("%s stands at %.1f mm, between the Anschriftfeld and the text" % (t[:40], y0))
            break
    # The sender: every header line ends at the right text edge.
    header_lines: dict[int, float] = {}
    for w in pages[0]:
        if w[3] < 45.0:
            key = round(w[3] * 2)
            header_lines[key] = max(header_lines.get(key, 0.0), w[2])
    for end in header_lines.values():
        if abs(end - BODY_RIGHT) > 0.6:
            errors.append("a sender line ends at %.1f mm, not right-aligned to %.0f mm" % (end, BODY_RIGHT))
            break
    # Every address line: a word at the Fluchtlinie inside its own grid line. The top of a word box
    # is the ascender of the font, which can reach about 0,5 mm above the line top.
    for i in range(len(lines(data["recipient"]))):
        top = line_top(ADDRESS_FIRST + i)
        hits = [w for w in pages[0] if abs(w[0] - LEFT) < 1.0 and top - 1.0 <= w[1] < top + PITCH - 1.0]
        if not hits:
            errors.append("address line %d is not drawn at x %s mm in its line from %.2f mm" % (i + 1, LEFT, top))
        elif hits[0][3] > top + PITCH + TOL:
            errors.append("%s reaches %.2f mm, below its address line" % (hits[0][4], hits[0][3]))
    drawn = {c for words in pages for w in words for c in w[4]}
    texts = [v for k, v in data.items() if isinstance(v, str) and k != "return_line"]
    texts += [x for k in LISTS for x in data.get(k, [])] + [x for p in data.get("info", []) for x in p]
    missing = sorted({c for t in texts for c in t if not c.isspace() and c != SOFT_HYPHEN} - drawn)
    if missing:
        errors.append("characters missing from the text layer of the PDF: " + " ".join(missing))
    # Every one-line value must be on the page as given. Right-to-left values are skipped, because
    # pdftotext returns their words in drawing order.
    page_text = flatten("".join(w[4] for words in pages for w in words))
    singles = [data[k] for k in INFO_KEYS if k in data]
    singles += [x for k in LISTS for x in data.get(k, [])] + [v for _, v in data.get("info", [])]
    singles += lines(data["recipient"])
    if data.get("return_line") and not data.get("return_line_auto"):
        singles.append(data["return_line"])
    for value in singles:
        if any(unicodedata.bidirectional(c) in RTL for c in value):
            continue
        if flatten(value) not in page_text:
            errors.append("not found in the text layer of the PDF: %s" % value[:60])
    words = sum(len(w) for w in pages)
    summary = "glyph check: %d words on %d page(s), all inside their areas" % (words, len(pages))
    return summary, overflow, errors


def pdf_strings(content: bytes) -> list[bytes]:
    """The decoded bytes of every string in a content stream: literal strings with nested
    parentheses and escapes, and hex strings. A small tokenizer, because a glyph code may itself be
    a parenthesis or a bracket byte, which a regular expression mistakes for syntax."""
    out, i, n = [], 0, len(content)
    while i < n:
        c = content[i]
        if c == ord("%"):  # a comment runs to the end of the line
            while i < n and content[i] not in b"\r\n":
                i += 1
        elif c == ord("("):
            buf, depth, i = bytearray(), 1, i + 1
            while i < n and depth:
                c = content[i]
                if c == BACKSLASH and i + 1 < n:
                    octal = re.match(rb"[0-7]{1,3}", content[i + 1:i + 4])
                    if octal:
                        buf.append(int(octal.group(), 8) & 0xFF)
                        i += 1 + len(octal.group())
                        continue
                    buf += PDF_ESCAPES.get(content[i + 1:i + 2], content[i + 1:i + 2])
                    i += 2
                    continue
                depth += (c == ord("(")) - (c == ord(")"))
                if depth:
                    buf.append(c)
                i += 1
            out.append(bytes(buf))
            continue
        elif c == ord("<"):  # a hex string; a dictionary bracket is not hex and is skipped below
            end = content.find(b">", i)
            digits = re.sub(rb"\s", b"", content[i + 1:end if end != -1 else n])
            if re.fullmatch(rb"[0-9A-Fa-f]*", digits):
                out.append(bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode("ascii")))
            i = n if end == -1 else end
        i += 1
    return out


def missing_glyphs(pdf: bytes) -> list[str]:
    """Signs, read from the PDF with no other tool, that a character was drawn as a placeholder:
    glyph 0 (.notdef, the empty box) in a text object, or the LastResort font, which macOS
    substitutes for a character no installed font has. Typst warns about neither, and the text
    layer still carries the right character, so text extraction cannot see it. The template sets
    fallback: false, so every glyph comes from one CID font with two-byte codes."""
    found = set()
    if b"LastResort" in pdf:
        found.add("a character no installed font has, drawn with the LastResort font")
    for m in STREAM.finditer(pdf):
        raw = m.group(1)
        try:
            data = zlib.decompressobj().decompress(raw)
        except zlib.error:
            data = raw  # an uncompressed stream; read it as it stands
        if b"LastResort" in data:
            found.add("a character no installed font has, drawn with the LastResort font")
        if data[:4] in FONT_MAGIC or b" Tf" not in data:
            continue  # a font program or an image, not page text
        for text_object in TEXT_OBJECT.findall(data):
            for codes in pdf_strings(text_object):
                if any(codes[i:i + 2] == b"\x00\x00" for i in range(0, len(codes) - 1, 2)):
                    found.add("a character the font has no glyph for, drawn as an empty box")
    return sorted(found)


def flow_advice(flow: list[str]) -> str:
    if any("starts on page" in e for e in flow):
        return ("the text cannot start on page 1 and still keep two of its lines with the Gruß on "
                "the last page (20.10): split the last paragraph with a blank line")
    return ("no layout keeps two lines of the text with the Gruß on the last page (20.10): split "
            "the last paragraph with a blank line, or shorten the text or the Anlagen")


def build(data: dict, out: Path, png: Path | None = None, font: str | None = None,
          force: bool = False, quiet: bool = False) -> dict:
    out = out.resolve()
    if out.is_dir():
        raise UsageError("--out %s is a folder" % out)
    if out.exists() and not force:
        raise UsageError("%s exists; pass --force to replace it" % out)
    exe, pdftotext = tools()
    data = normalise(data)
    font = pick_font(exe, font)
    out.parent.mkdir(parents=True, exist_ok=True)
    # The letter goes to Typst as a file in a private folder, never on the command line, where
    # other local processes could read it and where the OS limits its length.
    with tempfile.TemporaryDirectory(prefix="din5008-") as tmp:
        work = Path(tmp)
        try:
            shutil.copyfile(TEMPLATE, work / "letter.typ")
        except OSError as e:
            raise Environment("cannot read the template letter.typ: %s" % e) from None
        (work / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        pdf = work / "letter.pdf"
        # The plain layout, then cut layouts, the first whose page breaks obey 20.10, start the text
        # on page 1 and print the same lines as the plain layout.
        plain_lines = None
        layouts = [0]
        for keep in layouts:
            run_typst(exe, work, ["compile", "letter.typ", str(pdf)], font, keep)
            found = anchors(exe, work, font, keep)
            words = pdf_words(pdftotext, pdf)
            errors = prove(found, data)
            alone = gruss_lines(words, found)
            if alone:
                errors.append(alone)
            if plain_lines is None:
                plain_lines = text_lines(words, found)
                layouts += keep_candidates(words, found)
            elif text_lines(words, found) != plain_lines:
                errors.append(FLOW + "keeping %d lines with the Gruß changes how the text breaks" % keep)
            flow = [e for e in errors if e.startswith(FLOW)]
            if not flow:
                break
            if keep == 0:
                plain_errors = errors
        if flow and (len(flow) == len(errors) or all(e.startswith(FLOW) for e in plain_errors)):
            raise Refused(flow_advice(flow if len(flow) == len(errors) else plain_errors))
        if errors:
            raise ProofFailed("layout proof failed:\n  " + "\n  ".join(errors))
        if not pdf.read_bytes().startswith(b"%PDF-"):
            raise ProofFailed("the output is not a PDF")
        placeholders = missing_glyphs(pdf.read_bytes())
        if placeholders:
            raise Refused("the font %s cannot draw every character: %s. Remove the character, or pass "
                          "--font with a sans font that has it" % (font, "; ".join(placeholders)))
        glyphs, overflow, broken = prove_glyphs(words, data, found)
        if overflow:
            raise Refused("text runs out of its area, because a word is longer than the line: "
                          + "; ".join(overflow[:5]) + ". Break it with a space or a hyphen")
        if broken:
            raise ProofFailed("glyph check failed:\n  " + "\n  ".join(broken))
        pages = max(a["page"] for a in found)
        previews = []
        if png:
            for n in range(1, pages + 1):
                target = (png if pages == 1 else png.with_name("%s-%d%s" % (png.stem, n, png.suffix))).resolve()
                # Compared without case too: macOS and Windows file systems usually ignore it.
                if str(target).casefold() == str(out).casefold():
                    raise UsageError("the preview %s would replace the PDF --out" % target.name)
                if target.is_dir() or (target.exists() and not force):
                    raise UsageError("the preview %s exists; pass --force to replace it" % target)
                previews.append((work / ("page-%d.png" % n), target))
            run_typst(exe, work, ["compile", "letter.typ", str(work / "page-{p}.png"), "--format",
                                  "png", "--ppi", "110"], font, keep)
        # Previews first, then the PDF: a failed preview leaves any earlier PDF untouched.
        written = []
        try:
            for src, target in previews:
                target.parent.mkdir(parents=True, exist_ok=True)
                publish(src, target, force)
                written.append(target)
            publish(pdf, out, force)
        except (OSError, UsageError):
            if not force:  # previews this run created must not stand beside a missing PDF
                for target in written:
                    target.unlink(missing_ok=True)
            raise
        if png and force:
            # An earlier letter of another length left previews of the same name: remove them.
            stale = [png] if pages > 1 else []
            n = 1 if pages == 1 else pages + 1
            while (candidate := png.with_name("%s-%d%s" % (png.stem, n, png.suffix))).is_file():
                stale.append(candidate)
                n += 1
            for f in stale:
                if f.is_file() and str(f.resolve()).casefold() != str(out).casefold():
                    f.unlink()
    if not quiet:
        print("wrote %s (%d page(s), font %s)" % (out, pages, font))
        print("layout proof: %d anchors on the DIN 5008 Form B grid" % len(found))
        print(glyphs)
        if "return-line-omitted" in {a["name"] for a in found}:
            print("note: the Rücksendeangabe built from the sender does not fit 80 mm even at 6 pt and "
                  "was left out; pass a shorter --return-line")
        for _, target in previews:
            print("preview %s" % target)
        print("Print at 100 % (Actual size), never fit to page, or the address leaves the window.")
    return {"pages": pages, "anchors": found, "font": font, "pdf": out}


def publish(src: Path, target: Path, force: bool) -> None:
    """Put a finished file in place atomically: copy it next to the target, then rename it over the
    target, so an interrupted copy to another volume never leaves a half-written file behind."""
    # A new, exclusively created file in the target folder: never a name another run or a link holds.
    fd, name = tempfile.mkstemp(prefix=".din5008-", suffix=target.suffix, dir=target.parent)
    staging = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh, open(src, "rb") as data:
            shutil.copyfileobj(data, fh)
        umask = os.umask(0)
        os.umask(umask)
        staging.chmod(0o666 & ~umask)  # mkstemp creates 0600; a letter is an ordinary file
        if force:
            os.replace(staging, target)
            return
        try:
            os.link(staging, target)  # fails when the target appeared meanwhile: never overwrite
            return
        except FileExistsError:
            raise UsageError("%s exists; pass --force to replace it" % target) from None
        except OSError:
            pass  # FAT, exFAT and many network shares have no hard links
        # Create the target exclusively instead: still never an overwrite, though not atomic.
        try:
            out_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            raise UsageError("%s exists; pass --force to replace it" % target) from None
        with os.fdopen(out_fd, "wb") as fh, open(staging, "rb") as data:
            shutil.copyfileobj(data, fh)
    finally:
        staging.unlink(missing_ok=True)


def unescape(s: str | None) -> str | None:
    return None if s is None else s.replace("\\n", "\n")


def from_args(a: argparse.Namespace) -> dict:
    data: dict = {}
    if a.json:
        try:
            # utf-8-sig accepts the byte-order mark Windows PowerShell 5 writes into UTF-8 files.
            data = json.loads(Path(a.json).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError) as e:
            raise UsageError("cannot read --json as UTF-8: %s" % e) from None
        except json.JSONDecodeError as e:
            raise UsageError("--json is not valid JSON: %s" % e) from None
        if not isinstance(data, dict):
            raise UsageError("--json must hold one JSON object")
    for k in ("sender", "recipient", "return_line", "your_ref", "your_message", "our_ref",
              "our_message", "name", "phone", "fax", "email", "date", "subject", "salutation",
              "body", "closing", "signer"):
        if vars(a)[k] is not None:
            data[k] = unescape(vars(a)[k])
    if a.body_file:
        try:
            data["body"] = sys.stdin.read() if a.body_file == "-" else Path(a.body_file).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            raise UsageError("cannot read --body-file as UTF-8: %s" % e) from None
    for key, vals in (("notes", a.note), ("enclosures", a.enclosure), ("cc", a.cc)):
        if vals:
            data[key] = [unescape(v) for v in vals]
    if a.info:
        bad = [i for i in a.info if "=" not in i]
        if bad:
            raise UsageError("--info needs LABEL=VALUE: " + ", ".join(bad))
        data["info"] = [[unescape(x) for x in i.split("=", 1)] for i in a.info]
    if a.no_return_line:
        data["return_line"] = False
    if a.no_signer:
        data["signer"] = False
    return data


# ---- selftest -------------------------------------------------------------------------------

SENDER = "Erika Mustermann\nBeispielweg 7\n12345 Musterstadt"
RECIPIENT = "Beispiel GmbH\nKundenservice\nHerrn Max Beispiel\nMusterallee 12\n54321 Beispielstadt"
FULL = {"sender": SENDER, "recipient": RECIPIENT, "notes": ["Einschreiben"],
        "your_ref": "AB-123", "your_message": "1. März 2030", "email": "erika@example.com",
        "date": "15. März 2030", "subject": "Kündigung meines Vertrags Nr. 0815",
        "salutation": "Sehr geehrter Herr Beispiel,",
        "body": "hiermit kündige ich meinen Vertrag zum nächstmöglichen Termin.\n\nBitte bestätigen "
                "Sie mir die Kündigung schriftlich.",
        "closing": "Mit freundlichen Grüßen", "enclosures": ["Kopie des Vertrags"],
        "cc": ["Steuerberaterin"]}
SENTENCE = "Dieser Satz steht in einem Absatz und füllt die Zeile mit Text. "


def report(name: str, why: str | None) -> None:
    """One selftest line: ok, or FAIL with the reason."""
    print("ok   " + name if not why else "FAIL " + name + ": " + why)


def selftest(font: str | None = None) -> int:
    failures: list[str] = []
    try:
        exe, _ = tools()
        chosen = pick_font(exe, font)
    except (Environment, UsageError) as e:
        print("selftest cannot run: %s" % e)
        return 2 if isinstance(e, UsageError) else 3
    calibrated = chosen in ARIAL_METRICS
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)

        def case(name: str, data: dict, check, calibrated_only: bool = False) -> None:
            if calibrated_only and not calibrated:
                print("skip %s: its widths and page breaks are calibrated for Arial metrics, not %s" % (name, chosen))
                return
            try:
                why = check(build(data, d / (name + ".pdf"), font=font, quiet=True, force=True))
            except (Refused, UsageError) as e:
                why = check(e)
            except (Environment, ProofFailed, OSError) as e:
                why = "%s: %s" % (type(e).__name__, e)
            report(name, why)
            if why:
                failures.append(name)

        def names(r) -> set:
            return {a["name"] for a in r["anchors"]} if isinstance(r, dict) else set()

        def on_line(name, n):
            def check(r):
                if not isinstance(r, dict):
                    return "not built: %s" % r
                y = next(a["y"] for a in r["anchors"] if a["name"] == name)
                return None if abs(y - line_top(n)) < TOL else \
                    "%s at %.2f mm, expected grid line %d (%.2f mm)" % (name, y, n, line_top(n))
            return check

        def refused(fragment):
            return lambda r: None if isinstance(r, (Refused, UsageError)) and fragment in str(r) else \
                ("expected a refusal mentioning %s, got %r" % (fragment, r))[:300]

        def built(r):
            return None if isinstance(r, dict) else "not built: %s" % r

        def flow_ok(r):
            """Built with the text on page 1 and two lines of it above the Gruß, measured again in the
            PDF. An empty page 1, a lone Gruß or a refusal is never correct for these letters."""
            if not isinstance(r, dict):
                return "not built: %s" % r
            on = {a["name"]: a for a in r["anchors"]}
            if on["body"]["page"] != 1:
                return "the text starts on page %d" % on["body"]["page"]
            return gruss_lines(pdf_words(tools()[1], r["pdf"]), r["anchors"])

        case("full", FULL, on_line("subject", 24))
        case("minimal", {"sender": SENDER, "recipient": RECIPIENT},
             lambda r: None if isinstance(r, dict) and r["pages"] == 1 and names(r) >= set(MARKS)
             and not {"subject", "info", "body"} & names(r) else "expected one page with marks only")
        tall = dict(FULL, our_ref="X-1", our_message="2. März 2030", name="Erika Mustermann",
                    info=[["Kundennr.", "4711"], ["Vertrag", "0815"], ["Zähler", "Z-42"], ["Tarif", "Basis"]])
        # 4 + blank + 6 + blank + 1 rows from line 13 end on line 25, so the Betreff is on 28.
        case("tall_infoblock", tall, on_line("subject", 28))
        case("two_line_subject", dict(FULL, subject="Einspruch gegen den Bescheid vom 1. März 2030\n"
                                      "Aktenzeichen AZ-2030-0815"), on_line("salutation", 28))
        case("two_line_closing", dict(FULL, closing="Mit freundlichen Grüßen\nBeispiel e. V."), built)

        def closing_lines(n):
            return lambda r: None if isinstance(r, dict) and next(
                a["lines"] for a in r["anchors"] if a["name"] == "closing") == n else ("not %d lines: %s" % (n, r))[:200]
        # 20.12: the company name follows the Gruß after one blank line, which must survive.
        case("company_after_closing", dict(FULL, closing="Mit freundlichen Grüßen\n\nBeispiel e. V."),
             closing_lines(3))
        case("signer_false", dict(FULL, signer=False),
             lambda r: None if "enclosures" in names(r) and "signer" not in names(r) else "signer printed")
        case("crlf_address", dict(FULL, recipient=RECIPIENT.replace("\n", "\r\n")), on_line("subject", 24))
        case("decomposed_umlaut", dict(FULL, recipient=RECIPIENT.replace("Musterallee", "Mu" + chr(0x308) + "llerallee")),
             on_line("subject", 24))
        case("long_email_in_infoblock", dict(FULL, email="erika.mustermann@beispiel-gmbh.de"), built,
             calibrated_only=True)
        case("refuse_unbreakable_info_value", dict(FULL, email="x" * 70 + "@example.com"), refused("runs out"))
        case("hyphen_wrapping_email", dict(FULL, email="kundenservice@stadtwerke-musterstadt-beispiel.de"), built,
             calibrated_only=True)
        case("wrapping_info_value", dict(FULL, info=[["Betreff der Akte", "Antrag auf Erstattung der "
                                                     "Grundversorgungskosten aus dem Jahr 2029"]]), built,
             calibrated_only=True)
        long_body = "\n\n".join([SENTENCE * 3] * 14)
        case("multipage", dict(FULL, body=long_body),
             lambda r: None if isinstance(r, dict) and r["pages"] >= 2 else "expected 2 or more pages: %s" % r)
        # Page-break cases. Each must end in a correct outcome, whichever layout wins.
        case("one_long_paragraph", dict(FULL, body=SENTENCE * 90), flow_ok)
        case("page_long_paragraph", dict(FULL, body=SENTENCE * 120), flow_ok)
        for n in (30, 40, 50):
            case("single_paragraph_%d" % n, dict(FULL, body=SENTENCE * n), flow_ok)
            case("short_then_long_%d" % n, dict(FULL, body="Vorweg ein kurzer Absatz. Er steht auf Seite "
                                                     "eins.\n\n" + SENTENCE * n), flow_ok)
        case("many_enclosures", dict(FULL, body=SENTENCE * 25,
                                     enclosures=["Anlage %d" % i for i in range(1, 9)], cc=["A", "B", "C"]), flow_ok)
        five = "\n\n".join([SENTENCE * 3, "Kurz.", SENTENCE * 8, SENTENCE * 40, "Danke."])
        case("trailing_thanks", dict(FULL, body=five), flow_ok)
        case("trailing_thanks_no_closing", {k: v for k, v in dict(FULL, body=five).items()
                                            if k not in ("closing", "enclosures", "cc")}, built)
        # Ordinary letters that an over-strict reading of 20.10 used to refuse.
        case("sender_ending_in_full_stop", dict(FULL, sender="Musterverein e.V.\nBeispielweg 7\n12345 Musterstadt"), built)
        case("tab_in_body", dict(FULL, body="Betrag:\t120 EUR\n\nBitte überweisen Sie ihn."), built)
        case("common_long_label", dict(FULL, info=[["Rentenversicherungsnummer", "12 345678 A 901"]]), built,
             calibrated_only=True)
        case("one_line_paragraphs", dict(FULL, body="\n\n".join("Position %d ist erledigt." % i for i in range(40))), flow_ok)
        case("long_list_of_anlagen", dict(FULL, body=SENTENCE * 20, enclosures=["Beleg %d" % i for i in range(1, 21)],
                                          cc=["Herr A", "Frau B"]), built)
        no_gruss = {k: v for k, v in FULL.items() if k != "closing"}
        case("no_gruss_anlagen_spill", dict(no_gruss, signer=False, body=SENTENCE * 6 + "\n\n" + SENTENCE * 43,
                                            enclosures=["Beleg %d" % i for i in range(1, 9)]), built)
        # A letter that only the keep layout gets right must be found, not refused.
        # Paragraph 9 has two lines: cutting it would leave one line behind, so it moves whole with
        # "Danke." to the Gruß on page 2.
        case("keeps_two_lines_with_gruss", dict(FULL, body="\n\n".join([SENTENCE * 3] * 9 + ["Danke."])),
             lambda r: flow_ok(r) or (None if {a["name"]: a["page"] for a in r["anchors"]}.get("paragraph-9") == 2
                                      else "paragraph 9 did not move"), calibrated_only=True)

        def cut_with_lines(*must):
            """Built, a paragraph was cut, and each given text stands whole on one printed line."""
            def check(r):
                why = flow_ok(r)
                if why:
                    return why
                if not any(a["name"].startswith("tail-of-") for a in r["anchors"]):
                    return "no paragraph was cut"
                printed = text_lines(pdf_words(tools()[1], r["pdf"]), r["anchors"])
                torn = [m for m in must if not any(m in line for line in printed)]
                return "torn across lines: %s" % torn if torn else None
            return check
        case("cut_single_paragraph", dict(FULL, body=SENTENCE * 50), cut_with_lines(), calibrated_only=True)
        iban = "Bitte überweisen Sie den Betrag auf mein Konto:\nIBAN DE12 3456 7890 1234 5678 90\nBIC ABCDEFGH\nBeispielbank Musterstadt"
        # The four typed lines are cut at a typed break, so BIC and bank move and the IBAN stays whole.
        case("cut_keeps_typed_lines", dict(FULL, body=SENTENCE * 40 + "\n\n" + iban),
             lambda r: cut_with_lines("IBAN DE12 3456 7890 1234 5678 90")(r), calibrated_only=True)
        def render_lines(data: dict, keep: int) -> list[str]:
            """The printed body lines of one layout, rendered directly."""
            exe, pdftotext = tools()
            data = normalise(data)
            work = Path(tempfile.mkdtemp(dir=d))
            shutil.copyfile(TEMPLATE, work / "letter.typ")
            (work / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            run_typst(exe, work, ["compile", "letter.typ", str(work / "l.pdf")], chosen, keep)
            return text_lines(pdf_words(pdftotext, work / "l.pdf"), anchors(exe, work, chosen, keep))
        hyphen_text = ("Die Nebenkostenabrechnung enthält Positionen zur Grundsteuer, zur Gebäudeversicherung "
                       "und zur Verteilung der Heizkosten nach Verbrauchsanteilen. ") * 21
        hyphen_letter = dict(FULL, body=hyphen_text)

        def same_lines_as_plain(r):
            why = flow_ok(r)
            if why:
                return why
            printed = text_lines(pdf_words(tools()[1], r["pdf"]), r["anchors"])
            return None if printed == render_lines(hyphen_letter, 0) else "the kept layout breaks the text differently"
        case("cut_matches_plain_lines", hyphen_letter, same_lines_as_plain, calibrated_only=True)
        # A Widerspruch whose short cuts all fall after a hyphenated line: only a cut after a clean
        # line end keeps the printed lines, and it must be found, not refused.
        def cycle(text: str, start: str, count: int) -> str:
            words = text.split()
            i = words.index(start)
            return " ".join(words[(i + j) % len(words)] for j in range(count))
        thanks = ("vielen Dank für Ihr Schreiben vom 1. März 2030. Zu der darin enthaltenen Abrechnung nehme ich "
                  "wie folgt Stellung, und ich bitte Sie, meine Einwände sorgfältig zu prüfen.")
        objection = ("hiermit widerspreche ich dem Bescheid vom 12. März 2030 über die Festsetzung der Grundsteuer für "
                     "das Grundstück in der Beispielstraße, weil die Berechnung nicht nachvollziehbar ist und die "
                     "Wohnfläche falsch angesetzt wurde. Außerdem bitte ich um Aussetzung der Vollziehung bis zur "
                     "Entscheidung über meinen Einspruch sowie um Übersendung einer vollständigen Kopie der Akte. Die "
                     "Nebenkostenabrechnung enthält Positionen zur Gebäudeversicherung, zur Verteilung der Heizkosten "
                     "nach Verbrauchsanteilen und zur Hausmeistertätigkeit, deren Umlagefähigkeit ich bestreite.")
        case("cut_after_clean_line_end", dict(FULL, body=cycle(thanks, "vielen", 260) + "\n\n"
                                              + cycle(objection, "Aussetzung", 130)), flow_ok, calibrated_only=True)
        case("info_email_with_long_label", dict(FULL, email="maximilian.mustermann@example.com",
                                                info=[["Krankenversicherungsnummer", "A123456789"]]), built,
             calibrated_only=True)
        case("cut_paragraph_without_spaces", dict(FULL, body=SENTENCE * 40 + "\n\nIBAN:\nDE12345678901234567890\nBIC:\nABCDEFGHXXX"),
             flow_ok, calibrated_only=True)
        long_sender = ("Dr. Erika Mustermann-Beispielfrau\nLange Beispielstraße 123a, Hinterhaus\n"
                       "12345 Musterstadt-Nord")
        case("long_default_return_line", {"sender": long_sender, "recipient": RECIPIENT},
             lambda r: None if {"return-line", "return-line-omitted"} & names(r) else "no decision")
        refusals = {
            "refuse_7_address_lines": (dict(FULL, recipient=RECIPIENT + "\nDEUTSCHLAND\nZusatz"), "6 lines"),
            "refuse_wide_address_line": (dict(FULL, recipient="Ein Empfängername, der für die "
                                              "Anschriftzone eindeutig viel zu lang ist\n12345 Ort"), "mm wide"),
            "refuse_3_notes": (dict(FULL, notes=["Einschreiben", "Rückschein", "Eigenhändig"]), "at most 2"),
            "refuse_missing_recipient": ({"sender": SENDER}, "required"),
            "refuse_unknown_key": (dict(FULL, betreff="typo"), "unknown keys"),
            "refuse_vertical_tab": (dict(FULL, recipient="Beispiel GmbH\nHerrn Max\vBeispiel\n54321 Ort"), "U+000B"),
            "refuse_bidi_override": (dict(FULL, recipient="Beispiel GmbH\n" + chr(0x202E) + "1234\n54321 Ort"), "U+202E"),
            "refuse_multiline_note": (dict(FULL, notes=["Einschreiben\nRückschein"]), "one line"),
            "refuse_date_not_text": (dict(FULL, date=5), "must be a text"),
            "refuse_wide_return_line": (dict(FULL, return_line="Erika Mustermann, Beispielweg 7, 12345 "
                                             "Musterstadt, Deutschland, Europa, Erde, Sonnensystem"), "Rücksendeangabe"),
            "refuse_combining_stack": (dict(FULL, recipient="Beispiel GmbH\nZ" + chr(0x301) * 5 + "\n54321 Ort"), "combining"),
            "refuse_cyrillic_combining_stack": (dict(FULL, recipient="Beispiel GmbH\nZ" + chr(0x483) * 5 + "\n54321 Ort"), "combining"),
            "refuse_long_word": (dict(FULL, body="Siehe https://example.com/" + "a" * 150), "runs out"),
            "refuse_long_umlaut_word": (dict(FULL, body="a" + "ä" * 100), "runs out"),
            "refuse_narrow_nbsp_run": (dict(FULL, body=chr(0x202F).join(["Nummer"] * 40)), "runs out"),
            "refuse_missing_glyph": (dict(FULL, subject="Kündigung " + chr(0x10000)), "cannot draw"),
            "refuse_marks_across_soft_hyphens": (dict(FULL, recipient="Beispiel GmbH\nZ" + (chr(0x301) + SOFT_HYPHEN) * 5
                                                      + "\n54321 Ort"), "combining"),
            "refuse_long_info_label": (dict(FULL, info=[["Kassenzeichen der Landeshauptkasse Nordrhein-Westfalen", "1"]]), "label"),
            "refuse_oversized_closing": (dict(FULL, closing="\n".join(["Mit freundlichen Grüßen"] * 25)), "20"),
        }
        for name, (data, fragment) in refusals.items():
            case(name, data, refused(fragment))
        for name, call, fragment in (
                ("refuse_symbol_font", lambda: build(FULL, d / "sym.pdf", font="Webdings", quiet=True), "sans"),
                ("refuse_preview_over_pdf", lambda: build(dict(FULL, body=long_body), d / "x-1.png",
                                                          png=d / "x.png", font=font, quiet=True, force=True), "replace the PDF")):
            try:
                call()
                why = "was not refused"
            except (Refused, UsageError) as e:
                why = None if fragment in str(e) else "refused for another reason: %s" % e
            except (Environment, ProofFailed) as e:
                why = "%s: %s" % (type(e).__name__, e)
            report(name, why)
            if why:
                failures.append(name)
        contact_sender = SENDER + "\nE-Mail erika@example.com"
        why = None if "E-Mail" not in ", ".join(postal_lines(contact_sender)) else "the default carries the contact line"
        report("return_line_skips_contact", why)
        if why:
            failures.append("return_line_skips_contact")
        # The 20.10 line count on synthetic words, so it runs the same on every machine.
        gruss = [{"name": "closing", "page": 2, "y": 60.0}]
        one = [[], [(25.0, 20.5, 60.0, 24.0, "Danke.")]]
        two = [[], [(25.0, 20.5, 60.0, 24.0, "Zeile"), (25.0, 24.7, 60.0, 28.2, "Danke.")]]
        for name, words, want in (("gruss_one_line_fails", one, True), ("gruss_two_lines_pass", two, False)):
            ok = bool(gruss_lines(words, gruss)) == want
            report(name, None if ok else "wrong verdict")
            if not ok:
                failures.append(name)
        # publish() on a volume without hard links (FAT, exFAT): it still writes, and still never
        # overwrites. os.link is made to fail the way such a volume fails.
        real_link = os.link

        def no_link(*_args, **_kwargs):
            raise OSError(45, "Operation not supported")
        os.link = no_link
        try:
            src = d / "src.bin"
            src.write_bytes(b"letter")
            publish(src, d / "fat.pdf", False)
            try:
                publish(src, d / "fat.pdf", False)
                why = "an existing file was overwritten without --force"
            except UsageError:
                why = None if (d / "fat.pdf").read_bytes() == b"letter" else "the written file is wrong"
        except (OSError, UsageError) as e:
            why = "%s: %s" % (type(e).__name__, e)
        finally:
            os.link = real_link
        report("publish_without_hard_links", why)
        if why:
            failures.append("publish_without_hard_links")
        # The cut candidates on synthetic lines: a cut after a hyphenated line is never tried.
        rows = [(25.0, 100.0 + 4.23 * i, 60.0, 103.0 + 4.23 * i, "Zeile%d%s" % (i, SOFT_HYPHEN if i in (5, 3) else ""))
                for i in range(8)]
        got = keep_candidates([rows], [{"name": "subject", "page": 1, "y": 100.0},
                                       {"name": "closing", "page": 1, "y": 140.0}])
        why = None if got == [3, 5, 6, 7] else "candidates %s, expected [3, 5, 6, 7]" % got
        report("cut_candidates_skip_hyphens", why)
        if why:
            failures.append("cut_candidates_skip_hyphens")
        # The placeholder detector on synthetic PDF bytes, so it runs the same on every machine.
        for name, pdf, want in (
                ("glyph_box_detected", b"BT /f0 11 Tf (\\000\\001)Tj ET BT (\\000\\000)Tj ET", True),
                ("glyph_box_in_TJ_detected", b"BT [(\\000\\002) -20 (\\000\\000)]TJ ET", True),
                ("glyph_lastresort_detected", b"/BaseFont /ABCDEF+LastResort-Identity-H", True),
                ("glyph_box_after_bracket_byte", b"BT [(\x00]\x00\x00) 5]TJ ET /f0 1 Tf", True),
                ("glyph_box_in_hex", b"BT /f0 11 Tf <0001 0000> Tj ET", True),
                ("glyph_clean_passes", b"BT (\\000\\001\\000\\002)Tj ET BT (\\001\\000)Tj ET", False)):
            ok = bool(missing_glyphs(b"stream\n" + pdf + b" /f0 1 Tf\nendstream")) == want
            report(name, None if ok else "wrong verdict")
            if not ok:
                failures.append(name)
        try:
            build(FULL, d / "full.pdf", font=font, quiet=True)
            why = "an existing PDF was replaced without --force"
        except UsageError:
            why = None
        except (Refused, Environment, ProofFailed) as e:
            why = "%s: %s" % (type(e).__name__, e)
        report("refuse_overwrite", why)
        if why:
            failures.append("refuse_overwrite")
    failed = ", ".join(failures)
    print("selftest:", "FAIL (%s)" % failed if failures else "PASS")
    return 0 if not failures else 4


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", help="a JSON file with the letter (keys below)")
    p.add_argument("--sender", help="required: name, then address lines")
    p.add_argument("--recipient", help="required: up to 6 address lines")
    p.add_argument("--return-line", dest="return_line", help="Rücksendeangabe (default: the sender up to the postcode, one line)")
    p.add_argument("--no-return-line", action="store_true", help="print no Rücksendeangabe")
    p.add_argument("--note", action="append", help="a Zusatz or Vermerk such as Einschreiben, up to 2")
    for f, h in (("your-ref", "Ihr Zeichen"), ("your-message", "Ihre Nachricht vom"),
                 ("our-ref", "Unser Zeichen"), ("our-message", "Unsere Nachricht vom"),
                 ("name", "Name"), ("phone", "Telefon"), ("fax", "Fax"), ("email", "E-Mail"),
                 ("subject", "Betreff, may span lines"), ("salutation", "Anrede"),
                 ("body", "the text; a blank line starts a paragraph"),
                 ("closing", "Gruß; a blank line, then a company name, if any"),
                 ("signer", "Unterzeichner (default: the name of the sender when there is a closing)")):
        p.add_argument("--" + f, dest=f.replace("-", "_"), help=h)
    p.add_argument("--no-signer", action="store_true", help="print no Unterzeichner")
    p.add_argument("--date", help="Datum as printed, or today for the form 4. Oktober 2026")
    p.add_argument("--info", action="append", help="an extra Informationsblock line, LABEL=VALUE")
    p.add_argument("--body-file", help="the text from a file, or - for stdin")
    p.add_argument("--enclosure", action="append", help="an Anlage, repeatable")
    p.add_argument("--cc", action="append", help="a Verteiler entry, repeatable")
    p.add_argument("--font", help="a sans font installed for Typst, one of: " + ", ".join(SANS_FONTS)
                   + " (default: the first installed of " + ", ".join(FONTS) + ")")
    p.add_argument("--out", help="the PDF to write")
    p.add_argument("--force", action="store_true", help="replace an existing --out and its previews")
    p.add_argument("--png", help="also write PNG previews: FILE.png, or FILE-1.png, FILE-2.png ... for 2+ pages")
    p.add_argument("--selftest", action="store_true", help="render the fixtures and check every one")
    a = p.parse_args(argv)
    try:
        if a.selftest:
            return selftest(a.font)
        if not a.out:
            p.error("--out is required")
        build(from_args(a), Path(a.out), Path(a.png) if a.png else None, a.font, a.force)
        return 0
    except Refused as e:
        print("refused: %s" % e, file=sys.stderr)
        return 1
    except UsageError as e:
        print("usage: %s" % e, file=sys.stderr)
        return 2
    except Environment as e:
        print("error: %s" % e, file=sys.stderr)
        return 3
    except ProofFailed as e:
        print("error: %s" % e, file=sys.stderr)
        return 4
    except OSError as e:
        print("usage: cannot write: %s" % e, file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001  any other failure is a defect, never a refusal (exit 1)
        print("error: internal failure: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
