#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file of this skill.
"""The selftest of overlays.py, on invented scenes, plus the scene builder the illustration uses.

Purpose: prove align, list and merge on a dark and a light scene, and prove every refusal, with
    fixtures drawn here so that nothing real goes into them.
Usage:
    python3 overlays.py --selftest      (or: python3 selftest.py)
Exit codes: 0 all checks pass, 4 a check failed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import random
import sys
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from overlays import (  # noqa: E402 - sibling module, path set above
    BIG_DIFF,
    MIN_SOLID,
    SHEET_LONG,
    WARN_RESIDUAL,
    Alignment,
    Refused,
    Unreadable,
    _box_period,
    _changed,
    _edges,
    _grow,
    _join_offset,
    _near,
    _on_board,
    _window,
    align,
    at_anchor,
    at_offset,
    components,
    contact_sheet,
    load,
    main,
    merge,
    merge_boxes,
    proof_sheet,
    repeat_steps,
    save_verified,
)

import numpy as np  # noqa: E402 - overlays has already checked the dependency
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError  # noqa: E402


@dataclass(frozen=True)
class Theme:
    bg: tuple[int, int, int]
    name: tuple[int, int, int]
    text: tuple[int, int, int]
    muted: tuple[int, int, int]
    tip_fill: tuple[int, int, int]
    tip_outline: tuple[int, int, int] | None
    tip_text: tuple[int, int, int]


# The dark outline is faint on purpose: 20 above the background, between a hover tint and the
# threshold of 24 that leaked at an arrow tip, so a regression to it fails the selftest.
DARK = Theme((22, 25, 31), (238, 238, 238), (205, 208, 212), (148, 150, 154), (31, 34, 41), (42, 45, 51), (236, 236, 236))
# Light theme with the common solid, borderless dark bubble.
LIGHT = Theme((250, 250, 250), (20, 20, 20), (60, 60, 64), (120, 120, 124), (44, 44, 48), None, (250, 250, 250))


def scene(seed: int, theme: Theme = DARK, size=(980, 780)) -> Image.Image:
    """A chat-like screen with invented rows; nothing in it comes from a real application."""
    rnd = random.Random(seed)
    im = Image.new("RGB", size, theme.bg)
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=18)
    words = ["lorem", "ipsum", "dolor", "sit", "amet", "tempor", "magna", "aliqua", "veniam", "nostrud"]
    y = 80
    while y < size[1] - 60:
        hue = rnd.randrange(60, 220)
        d.ellipse((24, y, 64, y + 40), fill=(hue, 90, 255 - hue))
        d.text((80, y), rnd.choice(["Row one", "Row two", "Row three"]), fill=theme.name, font=font)
        d.text((200, y + 2), "a moment ago", fill=theme.muted, font=font)
        for line in range(rnd.randrange(1, 3)):
            text = " ".join(rnd.choice(words) for _ in range(rnd.randrange(5, 11)))
            d.text((80, y + 26 + 24 * line), text, fill=theme.text, font=font)
        y += 110
    return im


def tooltip(im: Image.Image, theme: Theme, x: int, y: int, lines: list[str]) -> tuple[int, int, int, int]:
    """Draw a tooltip with a downward arrow whose tip is at (x, y); return its box.

    The text sits 24 pixels inside the outline, more than GROW, so the text and the outline label
    apart and the box merge has work to do.
    """
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=18)
    w, h = 240, 44 + 26 * len(lines)
    x0, y0 = x - w // 2, y - 12 - h
    width = 2 if theme.tip_outline else 0
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=12, fill=theme.tip_fill, outline=theme.tip_outline, width=width)
    d.polygon([(x - 12, y0 + h - 1), (x + 12, y0 + h - 1), (x, y)], fill=theme.tip_fill, outline=theme.tip_outline)
    d.line((x - 10, y0 + h - 1, x + 10, y0 + h - 1), fill=theme.tip_fill, width=2)
    for i, text in enumerate(lines):
        d.text((x0 + 24, y0 + 22 + 26 * i), text, fill=theme.tip_text, font=font)
    return x0, y0, x0 + w + 1, y + 1


def pair(seed: int = 7, theme: Theme = DARK, shift=(-7, 11), tint: int = 10):
    """BASE and DONOR crops of one invented screen, each with its own tooltip.

    Returns base, donor, the base with BOTH tooltips (the expected merge), and the donor tooltip's
    box in base coordinates. The donor also carries a hover tint on one band of rows.
    """
    origin = (40, 40)
    world = scene(seed, theme)
    base_w, donor_w, want_w = world.copy(), world.copy(), world.copy()
    for target in (base_w, want_w):
        tooltip(target, theme, 330, 420, ["Open in thread", "Today at 09:15"])
    sign = 1 if sum(theme.bg) < 384 else -1
    tinted = np.asarray(donor_w, dtype=np.int16).copy()
    tinted[180:260] = np.clip(tinted[180:260] + sign * tint, 0, 255)
    donor_w = Image.fromarray(tinted.astype(np.uint8))
    for target in (donor_w, want_w):
        box_w = tooltip(target, theme, 300, 200, ["Copy link", "Edited 2 min ago"])
    crop = (origin[0], origin[1], origin[0] + 900, origin[1] + 700)
    # BASE(x, y) == DONOR(x + dx, y + dy), so the donor crop starts `shift` further along.
    do = (origin[0] - shift[0], origin[1] - shift[1])
    dcrop = (do[0], do[1], do[0] + 880, do[1] + 690)
    box = (box_w[0] - origin[0], box_w[1] - origin[1], box_w[2] - origin[0], box_w[3] - origin[1])

    def as_arr(im: Image.Image, c) -> np.ndarray:
        return np.asarray(im.crop(c).convert("RGBA"), dtype=np.int16)

    return as_arr(base_w, crop), as_arr(donor_w, dcrop), as_arr(want_w, crop), box


def prose(seed: int, size=(1800, 1600)) -> np.ndarray:
    """A page of random words at random indents and sizes: detail that never repeats, for large shifts."""
    rnd = random.Random(seed)
    im = Image.new("RGB", size, DARK.bg)
    d = ImageDraw.Draw(im)
    words = ["lorem", "ipsum", "dolor", "sit", "amet", "tempor", "magna", "aliqua", "veniam", "nostrud", "quis", "ut"]
    y = 10
    while y < size[1] - 30:
        fs = rnd.choice([14, 16, 18, 22])
        x = rnd.randrange(10, 200)
        text = " ".join(rnd.choice(words) for _ in range(rnd.randrange(4, 14)))
        d.text((x, y), text, fill=(rnd.randrange(150, 240),) * 3, font=ImageFont.load_default(size=fs))
        y += fs + rnd.randrange(6, 30)
    return np.asarray(im.convert("RGBA"), dtype=np.int16)


def outside_ring_equal(out: np.ndarray, want: np.ndarray, base: np.ndarray) -> bool:
    """True when `out` equals `want` everywhere except a 1-pixel ring around what the paste changed."""
    footprint = (want != base).any(axis=2)
    allowed = _grow(footprint, 3)
    return bool(np.array_equal(out[~allowed], want[~allowed]))


def selftest() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
        if not ok:
            failures.append(name)

    def refuses(fn, kind=Refused) -> bool:
        try:
            fn()
        except kind:
            return True
        return False

    try:
        for theme, label in ((DARK, "dark"), (LIGHT, "light")):
            base, donor, want, box = pair(theme=theme)
            al = align(base, donor)
            check(f"{label}: align recovers a known shift", (al.dx, al.dy) == (-7, 11), f"got ({al.dx}, {al.dy})")
            check(f"{label}: the residual stays under the warning", al.residual < WARN_RESIDUAL, str(al.residual))
            boxes = components(base, donor, al)
            hit = [b for b in boxes if b.x0 <= box[0] and b.y0 <= box[1] and b.x1 >= box[2] and b.y1 >= box[3]]
            touching = [c for c in boxes if hit and c is not hit[0] and c.x0 < hit[0].x1 and hit[0].x0 < c.x1
                        and c.y0 < hit[0].y1 and hit[0].y0 < c.y1]
            check(f"{label}: the donor tooltip is one component", len(hit) == 1 and not touching, str(boxes))
            if not hit:
                continue
            out, report = merge(base, donor, al, [(hit[0].x0, hit[0].y0, hit[0].x1, hit[0].y1)], False)
            check(f"{label}: the paste is solid, no stroke warning", report[0]["solid"] >= MIN_SOLID, str(report))
            footprint = (want != base).any(axis=2)
            check(f"{label}: merge copies the tooltip", np.array_equal(out[footprint], want[footprint]))
            check(f"{label}: nothing else changes, the tint included", outside_ring_equal(out, want, base))

        base, donor, want, box = pair(theme=LIGHT, tint=22)
        al = align(base, donor)
        wide = [b for b in components(base, donor, al) if b.x1 - b.x0 > 0.8 * base.shape[1]]
        check("a tint above --edge shows as a full-width component", len(wide) == 1, str(wide))
        al = align(base, donor, edge=30)
        hit = [b for b in components(base, donor, al, edge=30) if b.x0 <= box[0] and b.x1 >= box[2]]
        out, _ = merge(base, donor, al, [(b.x0, b.y0, b.x1, b.y1) for b in hit], False, edge=30)
        check("a higher --edge leaves that tint behind", len(hit) == 1 and outside_ring_equal(out, want, base))

        nested = merge_boxes([(0, 0, 100, 50), (10, 10, 40, 30), (90, 40, 120, 70), (200, 0, 210, 10)])
        check("merge_boxes joins nested and overlapping boxes only", sorted(nested) == [(0, 0, 120, 70), (200, 0, 210, 10)], str(nested))

        base, donor, _, _ = pair()
        big = Image.fromarray(donor.astype(np.uint8))
        big = np.asarray(big.resize((big.width * 5 // 4, big.height * 5 // 4), Image.LANCZOS), dtype=np.int16)
        check("align refuses a capture at another scale", refuses(lambda: align(base, big)))
        a1 = np.asarray(scene(1).convert("RGBA"), dtype=np.int16)
        a2 = np.asarray(scene(2).convert("RGBA"), dtype=np.int16)
        try:
            flagged = align(a1, a2).residual > WARN_RESIDUAL
        except Refused:
            flagged = True
        check("the same layout with other text is flagged or refused", flagged)
        flat = np.zeros((300, 300, 4), dtype=np.int16) + 200
        check("align refuses an overlap with no detail", refuses(lambda: align(flat, flat)))
        small = align(base[480:580, 60:260], donor[491:591, 53:253])
        check("a small crop aligns at full size", (small.dx, small.dy) == (0, 0), str(small))
        # Text only in the middle: a far shift overlaps flat margins alone and scores a perfect 0.
        tiny = Image.new("RGB", (300, 120), DARK.bg)
        ImageDraw.Draw(tiny).text((40, 50), "lorem ipsum dolor", fill=DARK.text, font=ImageFont.load_default(size=18))
        tiny_a = np.asarray(tiny.convert("RGBA"), dtype=np.int16)
        check("flat margins cannot win the search", refuses(lambda: align(tiny_a, tiny_a)) is False and align(tiny_a, tiny_a).dx == 0)
        # A strip under 256 px tall still takes the coarse pass; at full size it took about 10 s.
        strip = np.concatenate([np.asarray(scene(3).convert("RGBA"), dtype=np.int16)[100:350]] * 2, axis=1)
        start = time.monotonic()
        moved = align(strip[:, :-3], strip[:, 3:])
        check("a thin strip aligns fast", (moved.dx, moved.dy) == (-3, 0) and time.monotonic() - start < 2.0, str(moved))
        for th, label in ((DARK, "dark"), (LIGHT, "light")):
            b2, d2, _, _ = pair(theme=th)
            menu = Image.fromarray(d2.astype(np.uint8))
            draw = ImageDraw.Draw(menu)
            draw.rectangle((150, 120, 870, 600), fill=th.tip_fill, outline=th.tip_outline or (0, 0, 0))
            for i in range(16):
                draw.text((170, 130 + 28 * i), f"Menu item {i}", fill=th.tip_text, font=ImageFont.load_default(size=18))
            got = align(b2, np.asarray(menu, dtype=np.int16))
            check(f"{label}: a menu over half the capture still aligns, and warns",
                  (got.dx, got.dy) == (-7, 11) and got.residual > WARN_RESIDUAL, str(got))
        text = Image.fromarray(donor.astype(np.uint8))
        ImageDraw.Draw(text).text((500, 600), "brand new text here", fill=DARK.name, font=ImageFont.load_default(size=18))
        text_a = np.asarray(text, dtype=np.int16)
        tal = align(base, text_a)
        low = [c for c in components(base, text_a, tal) if c.y0 > 550]
        solid = merge(base, text_a, tal, [(c.x0, c.y0, c.x1, c.y1) for c in low], False)[1]
        check("text with no outline scores as thin strokes", len(low) == 1 and solid[0]["solid"] < MIN_SOLID, str(solid))
        # Every shift of a sweep, both signs, both axes. The round-3 penalty got 102 of 224 of these wrong.
        sweep = np.asarray(scene(2, size=(1100, 900)).convert("RGBA"), dtype=np.int16)
        wrong = []
        for sx in range(-38, 39, 19):
            for sy in (-30, -5, 8, 25):
                got = align(sweep[100:800, 100:1000], sweep[100 + sy : 800 + sy, 100 + sx : 1000 + sx])
                if (got.dx, got.dy) != (-sx, -sy):
                    wrong.append((sx, sy, got))
        check("a sweep of shifts all align exactly", not wrong, str(wrong))
        wide = np.asarray(scene(11, size=(2000, 1400)).convert("RGBA"), dtype=np.int16)
        for k in (70, 71, 90):
            got = align(wide[100:1300, 100:1800], wide[100:1300, 100 + k : 1800 + k])
            check(f"a true shift of {k} px is found on a sparse screen", (got.dx, got.dy) == (-k, 0), str(got))
        pane = donor.copy()
        pane[:, 380:] = np.asarray(scene(9).convert("RGBA"), dtype=np.int16)[:690, 380:880]
        check("a pane of other content warns", align(base, pane).residual > WARN_RESIDUAL)
        word = Image.new("RGB", (400, 300), DARK.bg)
        for x, y in ((10, 12), (346, 12), (10, 252), (346, 252)):
            ImageDraw.Draw(word).text((x, y), "ab", fill=DARK.text, font=ImageFont.load_default(size=18))
        word_a = np.asarray(word.convert("RGBA"), dtype=np.int16)
        check("four words in the corners are too little detail to trust", refuses(lambda: align(word_a, word_a)))
        small = Image.fromarray(donor.astype(np.uint8))
        small = np.asarray(small.resize((small.width * 101 // 100, small.height * 101 // 100), Image.LANCZOS), dtype=np.int16)
        check("align refuses a capture rescaled by 1 percent", refuses(lambda: align(base, small)))
        # A table repeats with its row pitch. Every shift up to 96 px must align exactly or be refused
        # as repeating: an aligned row alias is the one wrong answer.
        table = Image.new("RGB", (1000, 1300), LIGHT.bg)
        tdraw = ImageDraw.Draw(table)
        tfont = ImageFont.load_default(size=18)
        for i in range(44):
            y = 10 + 28 * i
            tdraw.line((0, y, 1000, y), fill=(200, 200, 200))
            tdraw.text((20, y + 5), f"2026-10-{i % 28 + 1:02d}", fill=(30, 30, 30), font=tfont)
            tdraw.text((300, y + 5), "Payment received", fill=(30, 30, 30), font=tfont)
            tdraw.text((700, y + 5), f"{(i * 37) % 900 + 100}.00", fill=(30, 30, 30), font=tfont)
        table_a = np.asarray(table.convert("RGBA"), dtype=np.int16)
        aliased = []
        for sy in range(-96, 97, 8):
            try:
                got = align(table_a[150:1150, 50:950], table_a[150 + sy : 1150 + sy, 50:950])
            except Refused:
                continue
            if (got.dx, got.dy) != (0, -sy):
                aliased.append((sy, got))
        check("a repeating table never aligns on a row alias", not aliased, str(aliased))
        # Crops of only two rows of that table: every alias fits, so align must refuse, never guess.
        for band in ((50, 950, 48), (660, 724, 44)):
            x0, x1, sy = band
            short = (table_a[150:214, x0:x1], table_a[150 + sy : 214 + sy, x0:x1])
            check(f"two rows of the table, {x1 - x0} px wide, are refused", refuses(lambda: align(*short)))
        # A unique heading over identical rows decides, even when a tooltip moves by one row.
        listing = Image.new("RGB", (900, 700), DARK.bg)
        ldraw = ImageDraw.Draw(listing)
        for ly in range(80, 700, 64):
            ldraw.text((20, ly), "a repeated row of text", fill=DARK.text, font=tfont)
        ldraw.text((20, 2), "HEADING: " + "a line that appears only once on this screen " * 2, fill=DARK.text, font=tfont)
        lb, ld = listing.copy(), listing.copy()
        tooltip(lb, DARK, 300, 350, ["Copy link", "Edited 2 min ago"])
        tooltip(ld, DARK, 300, 414, ["Copy link", "Edited 2 min ago"])
        try:
            lal = align(np.asarray(lb.convert("RGBA"), dtype=np.int16), np.asarray(ld.convert("RGBA"), dtype=np.int16))
            heading_ok = (lal.dx, lal.dy) == (0, 0) or lal.rival is not None
        except Refused:
            heading_ok = True
        check("identical rows under one heading never align silently on a row alias", heading_ok)
        # One block that matches only at a far corner must not beat the offset the whole page supports.
        def page() -> Image.Image:
            im = Image.new("RGB", (200, 200), DARK.bg)
            r = random.Random(1)
            for py in range(4, 200, 16):
                text = "".join(r.choice("abcdefghij") for _ in range(20))
                ImageDraw.Draw(im).text((4, py), text, fill=DARK.text, font=ImageFont.load_default(size=13))
            return im
        pb, pd = page(), page()
        ImageDraw.Draw(pd).rectangle((60, 60, 140, 140), fill=DARK.bg)
        block = scene(9).crop((80, 80, 120, 120))
        pb.paste(block, (0, 0))
        pd.paste(block, (160, 160))
        cal = align(np.asarray(pb.convert("RGBA"), dtype=np.int16), np.asarray(pd.convert("RGBA"), dtype=np.int16), max_shift=180)
        check("a match in a far corner does not beat the whole page", (cal.dx, cal.dy) == (0, 0), str(cal))
        tall = np.asarray(scene(19, size=(2000, 1500)).convert("RGBA"), dtype=np.int16)
        check("a better fit with too little overlap is refused, not passed over",
              refuses(lambda: align(tall[300:450, :1800], tall[380:530, :1800])))
        page = prose(5)
        # Half the height of a 190 px strip: without padding the FFT wraps this shift around.
        got = align(page[300:490, 100:1700], page[396:586, 100:1700])
        check("a 96 px shift on a 190 px strip does not wrap around", (got.dx, got.dy) == (0, -96), str(got))
        got = align(page[100:1100, 100:1700], page[612:1500, 100:1700], max_shift=512)
        check("a shift of 512 px is found with --max-shift 512", (got.dx, got.dy) == (0, -512), str(got))
        rows = Image.new("RGB", (900, 700), DARK.bg)
        for ry in range(0, 700, 64):
            ImageDraw.Draw(rows).text((20, ry + 10), "the same row, again and again", fill=DARK.text, font=tfont)
        rows_a = np.asarray(rows.convert("RGBA"), dtype=np.int16)
        check("rows that repeat exactly are refused as ambiguous", refuses(lambda: align(rows_a, rows_a)))
        island = Image.new("RGB", (900, 700), DARK.bg)
        for iy in range(5):
            ImageDraw.Draw(island).text((20, 20 + 26 * iy), "a small island of text", fill=DARK.text, font=tfont)
        island_a = np.asarray(island.convert("RGBA"), dtype=np.int16)
        check("detail in one small corner is too narrow to trust", refuses(lambda: align(island_a, island_a)))
        # Red on green of the same brightness: a brightness-only edge map sees nothing here.
        hue = Image.new("RGB", (900, 700), (40, 200, 40))
        for hy in range(0, 700, 30):
            ImageDraw.Draw(hue).text((20 + (hy * 7) % 300, hy + 4), f"line {hy} of colored text, row {hy // 30}", fill=(200, 40, 40), font=tfont)
        hue_a = np.asarray(hue.convert("RGBA"), dtype=np.int16)
        hue_al = align(hue_a[20:, 10:], hue_a[:-20, :-10])
        check("colors of equal brightness still align", (hue_al.dx, hue_al.dy) == (10, 20), str(hue_al))
        # A true shift just past --max-shift must be refused, never answered with an in-range alias.
        for sh in (97, 104, 112, 127):
            out_b, out_d, _, _ = pair(shift=(0, sh))
            check(f"a true shift of {sh} px is refused at --max-shift 96", refuses(lambda: align(out_b, out_d)))
        # Numbered rows with the same hover toolbar on different rows: the row numbers decide.
        def numbered(tool_row: int) -> np.ndarray:
            im = Image.new("RGB", (1000, 1400), LIGHT.bg)
            nd = ImageDraw.Draw(im)
            for i in range(48):
                ny = 10 + 32 * i
                nd.line((0, ny, 1000, ny), fill=(210, 210, 210))
                for nx, text in ((20, f"{i + 1}"), (90, "12.50"), (250, "Open"), (400, "Alice"), (600, f"#{4000 + i * 7}")):
                    nd.text((nx, ny + 7), text, fill=(30, 30, 30), font=tfont)
            if tool_row >= 0:
                ny = 10 + 32 * tool_row
                nd.rounded_rectangle((760, ny + 2, 960, ny + 30), radius=6, fill=(60, 60, 66))
            return np.asarray(im.convert("RGBA"), dtype=np.int16)
        nb, nd_ = numbered(10), numbered(12)
        try:
            got = align(nb[150:1150, 100:950], nd_[119:1119, 110:960])
            toolbar_ok = (got.dx, got.dy) == (-10, 31) or got.rival is not None
        except Refused:
            toolbar_ok = True
        check("a toolbar on another numbered row never pulls the offset silently onto a row alias", toolbar_ok)
        # The doubt flag is the contract: over repeating rows with a moved overlay, an offset that is
        # wrong must carry it. Heading over identical rows, the tooltip one or two rows lower.
        silent = []
        for heading in ("Inbox", "Settings"):
            for k in (1, -1, 2):
                for mx, my in ((0, 0), (5, 9), (-7, 20)):
                    hb, hd = Image.new("RGB", (900, 700), DARK.bg), None
                    for hy in range(80, 700, 64):
                        ImageDraw.Draw(hb).text((20, hy), "a repeated row of text", fill=DARK.text, font=tfont)
                    ImageDraw.Draw(hb).text((20, 2), heading, fill=DARK.text, font=tfont)
                    hd = hb.copy()
                    tooltip(hb, DARK, 300, 350, ["Copy link", "Edited 2 min ago"])
                    tooltip(hd, DARK, 300, 350 + 64 * k, ["Copy link", "Edited 2 min ago"])
                    ha, hda = np.asarray(hb.convert("RGBA"), dtype=np.int16), np.asarray(hd.convert("RGBA"), dtype=np.int16)
                    pb_, pd_ = ha[30:670, 30:870], hda[30 + my : 670 + my, 30 + mx : 870 + mx]
                    try:
                        hal = align(pb_, pd_)
                    except Refused:
                        continue
                    if (hal.dx, hal.dy) != (-mx, -my) and hal.rival is None:
                        silent.append((heading, k, (mx, my), hal))
        # Identical rows with one small badge, and dated rows that share every amount: the evidence for
        # the true offset is one tile, or smaller than a tile. These broke an earlier rule.
        for badge_row, tip_row, step, (mx, my) in ((7, 9, 1, (5, 9)), (12, 6, -1, (-7, 20)), (4, 10, 2, (3, -11))):
            br = Image.new("RGB", (900, 1100), DARK.bg)
            bdraw = ImageDraw.Draw(br)
            for ry in range(10, 1100, 56):
                bdraw.ellipse((16, ry + 4, 44, ry + 32), fill=(120, 90, 200))
                bdraw.text((60, ry + 6), "Alex Example  +1  sounds good", fill=DARK.text, font=tfont)
            bdraw.ellipse((600, 10 + 56 * badge_row + 10, 610, 10 + 56 * badge_row + 20), fill=(220, 60, 60))
            bd = br.copy()
            tooltip(br, DARK, 300, 10 + 56 * tip_row + 30, ["Copy link", "Edited 2 min ago"])
            tooltip(bd, DARK, 300, 10 + 56 * (tip_row + step) + 30, ["Copy link", "Edited 2 min ago"])
            ba, bda = np.asarray(br.convert("RGBA"), dtype=np.int16), np.asarray(bd.convert("RGBA"), dtype=np.int16)
            try:
                bal = align(ba[60:960, 30:870], bda[60 + my : 960 + my, 30 + mx : 870 + mx])
            except Refused:
                continue
            if (bal.dx, bal.dy) != (-mx, -my) and bal.rival is None:
                silent.append(("badge", badge_row, tip_row, step, (mx, my), bal))
        for tip_row, step, (mx, my) in ((14, 1, (17, 27)), (20, 1, (-8, -41)), (10, 2, (5, 30))):
            dt = Image.new("RGB", (1000, 1300), LIGHT.bg)
            ddraw = ImageDraw.Draw(dt)
            for i in range(44):
                ry = 10 + 28 * i
                ddraw.line((0, ry, 1000, ry), fill=(200, 200, 200))
                ddraw.text((20, ry + 5), f"2026-10-{i % 28 + 1:02d}", fill=(30, 30, 30), font=tfont)
                ddraw.text((300, ry + 5), "Payment received", fill=(30, 30, 30), font=tfont)
                ddraw.text((700, ry + 5), "250.00", fill=(30, 30, 30), font=tfont)
            dd = dt.copy()
            tooltip(dt, LIGHT, 500, 10 + 28 * tip_row, ["Copy link", "Edited 2 min ago"])
            tooltip(dd, LIGHT, 500, 10 + 28 * (tip_row + step), ["Copy link", "Edited 2 min ago"])
            ta, tda = np.asarray(dt.convert("RGBA"), dtype=np.int16), np.asarray(dd.convert("RGBA"), dtype=np.int16)
            try:
                tal = align(ta[140:640, 90:920], tda[140 + my : 640 + my, 90 + mx : 920 + mx])
            except Refused:
                continue
            if (tal.dx, tal.dy) != (-mx, -my) and tal.rival is None:
                silent.append(("dated", tip_row, step, (mx, my), tal))
        check("no wrong offset on repeating rows comes without the doubt flag", not silent, str(silent))
        # The repeat flag is structural: a screen that repeats within the search range has an alias of
        # every offset, so even an exact offset carries a rival; a page that does not repeat carries none.
        settings_page = Image.new("RGB", (900, 900), DARK.bg)
        for sy in range(10, 900, 48):
            ImageDraw.Draw(settings_page).text((30, sy + 12), "Enable notifications", fill=DARK.text, font=tfont)
            ImageDraw.Draw(settings_page).rounded_rectangle((780, sy + 10, 830, sy + 34), radius=12, fill=(80, 160, 90))
        sp = np.asarray(settings_page.convert("RGBA"), dtype=np.int16)
        try:
            rep = align(sp[20:820, 20:880], sp[29:829, 25:885])
            repeat_flagged = rep.rival is not None
        except Refused:
            repeat_flagged = True
        check("a screen that repeats is flagged even when no overlay moved", repeat_flagged)
        # Chat with real text: avatars and timestamps repeat, the messages do not. No flag.
        chat = Image.new("RGB", (900, 1000), DARK.bg)
        cr = random.Random(4)
        cwords = ["sounds", "good", "ship", "it", "tomorrow", "review", "the", "plan", "thanks", "on", "it", "later"]
        for cy in range(10, 1000, 56):
            ImageDraw.Draw(chat).ellipse((16, cy + 4, 44, cy + 32), fill=(120, 90, 200))
            ImageDraw.Draw(chat).text((60, cy + 4), "Alex Example", fill=DARK.name, font=tfont)
            ImageDraw.Draw(chat).text((60, cy + 26), " ".join(cr.choice(cwords) for _ in range(cr.randrange(3, 9))), fill=DARK.text, font=tfont)
        ca = np.asarray(chat.convert("RGBA"), dtype=np.int16)
        got = align(ca[40:900, 20:880], ca[49:909, 25:885])
        check("chat with real text aligns without a flag", (got.dx, got.dy, got.rival) == (-5, -9, None), str(got))
        # Short replies under timestamps that change: most tiles agree with the screen one row on,
        # yet few are clean, so only the clean share keeps this from a flag.
        replies = Image.new("RGB", (900, 1000), DARK.bg)
        rr, sfont = random.Random(2), ImageFont.load_default(size=14)
        for i in range(18):
            ry = 10 + 56 * i
            ImageDraw.Draw(replies).ellipse((16, ry + 6, 48, ry + 38), fill=(120, 90, 200))
            ImageDraw.Draw(replies).text((60, ry + 4), "Alex Example", fill=DARK.name, font=tfont)
            ImageDraw.Draw(replies).text((200, ry + 6), f"{9 + i // 6}:{(i * 7) % 60:02d}", fill=DARK.muted, font=sfont)
            ImageDraw.Draw(replies).text((60, ry + 28), rr.choice(["sounds good", "on it", "thanks!", "+1", "will check"]), fill=DARK.text, font=tfont)
        ra = np.asarray(replies.convert("RGBA"), dtype=np.int16)
        got = align(ra[40:900, 20:880], ra[49:909, 25:885])
        check("chat with short replies aligns without a flag", (got.dx, got.dy, got.rival) == (-5, -9, None), str(got))
        # A popover over half the page, with more text than the page behind it, moved 64 px: the
        # popover can decide the offset, and then the page behind it must come back as the rival.
        pages = []
        for top, title in ((85, "def resolve(path, strict=False)"), (149, "def resolve_all(paths, *, follow=True)")):
            pg = Image.new("RGB", (900, 700), DARK.bg)
            pr, br = random.Random(1), random.Random(99)
            for i in range(30):
                ImageDraw.Draw(pg).text((20, 10 + 23 * i), " ".join(pr.choice(cwords) for _ in range(pr.randrange(5, 13))), fill=DARK.text, font=sfont)
            ImageDraw.Draw(pg).rounded_rectangle((100, top, 700, top + 500), radius=10, fill=(44, 47, 56), outline=(90, 94, 104), width=2)
            ImageDraw.Draw(pg).text((120, top + 14), title, fill=DARK.name, font=tfont)
            for i in range(19):
                ImageDraw.Draw(pg).text((120, top + 50 + 23 * i), " ".join(br.choice(cwords) for _ in range(br.randrange(5, 11))), fill=DARK.text, font=sfont)
            pages.append(np.asarray(pg.convert("RGBA"), dtype=np.int16))
        got = align(*pages)
        check("a big popover that moved never decides the offset silently", (got.dx, got.dy) == (0, 0) or got.rival == (0, 0), str(got))
        check("a big popover that decides the offset leaves much of the screen changed", (got.dx, got.dy) == (0, 0) or got.changed >= BIG_DIFF, str(got))
        got = at_anchor(pages[0], pages[1], (10, 5, 100, 700))
        check("an anchor on the page beside a big popover finds the page's offset", (got.dx, got.dy) == (0, 0), str(got))
        check("an anchored offset carries no rival and the whole screen's residual",
              got.rival is None and got.residual == at_offset(pages[0], pages[1], 0, 0).residual, str(got))
        try:
            at_anchor(pages[0], pages[1], (750, 600, 890, 690))
            blank_refused = ""
        except Refused as exc:
            blank_refused = str(exc)
        check("an anchor on empty space is refused in the anchor's own words", "does not line the captures up on its own" in blank_refused, blank_refused[:80])
        # Rows that differ only in a time and an id, with a big profile card two rows away in DONOR,
        # above and below: the alias that lays one card onto the other wins, and only the column of
        # changed ids, two row steps from it, points back to the true offset.
        cards = []
        for card_row, ids in ((3, True), (1, True), (5, True), (3, False), (1, False)):
            cr_im = Image.new("RGB", (900, 1000), LIGHT.bg)
            for i, ry in enumerate(range(10, 952, 48)):
                ImageDraw.Draw(cr_im).ellipse((16, ry + 4, 44, ry + 32), fill=(120, 90, 200))
                ImageDraw.Draw(cr_im).text((60, ry + 6), "Alex Example", fill=LIGHT.name, font=tfont)
                ImageDraw.Draw(cr_im).text((200, ry + 8), f"{9 + i // 6}:{(i * 7) % 60:02d}", fill=LIGHT.muted, font=sfont)
                if ids:
                    ImageDraw.Draw(cr_im).text((60, ry + 28), f"#{4000 + i * 7}", fill=LIGHT.muted, font=sfont)
            cx, cy = 380, 30 + 48 * card_row
            ImageDraw.Draw(cr_im).rounded_rectangle((cx, cy, cx + 360, cy + 220), radius=10, fill=LIGHT.tip_fill, outline=(0, 0, 0), width=2)
            ImageDraw.Draw(cr_im).ellipse((cx + 16, cy + 16, cx + 76, cy + 76), fill=(200, 120, 60))
            ImageDraw.Draw(cr_im).text((cx + 92, cy + 18), "Profile card title", fill=LIGHT.tip_text, font=tfont)
            ImageDraw.Draw(cr_im).text((cx + 92, cy + 44), "Staff engineer, platform", fill=LIGHT.tip_text, font=sfont)
            for k in range(5):
                ImageDraw.Draw(cr_im).text((cx + 16, cy + 92 + 24 * k), f"detail line of the popover number {k}", fill=LIGHT.tip_text, font=sfont)
            cards.append(np.asarray(cr_im.convert("RGBA"), dtype=np.int16))
        got = align(cards[0][80:720, 30:890], cards[1][70:710, 31:891])
        check("a big card two rows higher names the true offset as rival", (got.dx, got.dy) == (-1, 10) or got.rival == (-1, 10), str(got))
        got = align(cards[0][80:720, 30:890], cards[2][83:723, 37:897])
        check("a big card two rows lower names the true offset as rival", (got.dx, got.dy) == (-7, -3) or got.rival == (-7, -3), str(got))
        # At an alias that align chose by itself, the full-width proof band shows the ids at the left
        # edge, which differ, while the card itself lands on the card.
        alias = align(cards[0][80:720, 30:890], cards[2][83:723, 37:897])
        pf = proof_sheet(cards[0][80:720, 30:890], cards[2][83:723, 37:897], alias, cards[0][80:720, 30:890], [(350, 94, 710, 314)])
        band = (pf.shape[0] - 16) // 3 - 28
        ids_top, ids_bottom = pf[28 : 28 + band, :120, :3], pf[56 + band : 56 + 2 * band, :120, :3]
        check("the proof of a real alias shows other ids beside the card", (alias.dx, alias.dy) != (-7, -3) and bool((np.abs(ids_top - ids_bottom) > 16).any()), str(alias))
        got = at_anchor(cards[0][80:720, 30:890], cards[2][83:723, 37:897], (0, 0, 140, 640))
        check("an anchor on the column of ids corrects the card alias, with no rival from the ids", (got.dx, got.dy, got.rival) == (-7, -3, None), str(got))
        # A ruled table: shifts that overlap by less than a tile meet only on the rules, with no
        # mismatch at all, and must not crowd the true offset out of an anchor's search.
        ruled_rows = []
        for hover in (8, 10):
            rt = Image.new("RGB", (1000, 1580), LIGHT.bg)
            for i in range(48):
                ry = 10 + 32 * i
                ImageDraw.Draw(rt).line((0, ry, 1000, ry), fill=(210, 210, 210))
                for rx, text in ((20, f"{i + 1}"), (90, "12.50"), (250, "Open"), (400, "Alice"), (600, f"#{4000 + i * 7}")):
                    ImageDraw.Draw(rt).text((rx, ry + 7), text, fill=(30, 30, 30), font=tfont)
            ImageDraw.Draw(rt).rounded_rectangle((760, 12 + 32 * hover, 960, 40 + 32 * hover), radius=6, fill=(60, 60, 66))
            tooltip(rt, LIGHT, 500, 10 + 32 * hover, ["Copy link", "Edited 2 min ago"])
            ruled_rows.append(np.asarray(rt.convert("RGBA"), dtype=np.int16))
        got = at_anchor(ruled_rows[0][60:760, 40:940], ruled_rows[1][67:767, 45:945], (557, 220, 625, 700))
        check("an anchor on the ids of a ruled table finds the true offset", (got.dx, got.dy) == (-5, -7), str(got))
        try:
            proof_sheet(np.zeros((1000, 4000, 4), dtype=np.int16), np.zeros((1000, 4000, 4), dtype=np.int16), Alignment(0, 0, 0.0),
                        np.zeros((1000, 4000, 4), dtype=np.int16), [(10, 60 * k, 50, 60 * k + 90) for k in range(15)])
            proof_refused = False
        except Refused:
            proof_refused = True
        check("a proof larger than the input limit is refused before it is drawn", proof_refused)
        check("a capture thinner than one window cell does not crash the window", _window(np.ones((20, 5000), dtype=bool))[0].size > 0)
        got = align(cards[3][80:720, 30:890], cards[4][70:710, 31:891])
        check("a big card two rows away on rows without ids names the true offset", (got.dx, got.dy) == (-1, 10) or got.rival == (-1, 10), str(got))
        # An icon strip with the tooltip one icon further along: the best repeat step is 4 icons
        # long, so only the 1-icon step reaches the true offset inside --max-shift.
        strips = []
        for tip in (4, 5):
            st_im = Image.new("RGB", (1000, 110), DARK.bg)
            for i in range(24):
                ImageDraw.Draw(st_im).rounded_rectangle((20 + 40 * i, 70, 46 + 40 * i, 96), radius=5, outline=(200, 200, 200), width=2)
            tx = 20 + 40 * tip
            ImageDraw.Draw(st_im).rounded_rectangle((tx - 30, 8, tx + 60, 52), radius=6, fill=(60, 60, 66))
            ImageDraw.Draw(st_im).text((tx - 20, 18), "Bold", fill=(240, 240, 240), font=tfont)
            strips.append(np.asarray(st_im.convert("RGBA"), dtype=np.int16))
        got = align(strips[0][2:106, 10:900], strips[1][0:104, 6:896])
        check("an icon strip names the true offset as rival", (got.dx, got.dy) == (4, 2) or got.rival == (4, 2), str(got))
        # A table with a full-width rule under every row, and different text in each row, does not
        # repeat: a tile that holds only a rule stays clean when moved along it.
        ruled = Image.new("RGB", (1000, 900), LIGHT.bg)
        tr = random.Random(3)
        for i in range(27):
            ImageDraw.Draw(ruled).line((20, 20 + 32 * i, 980, 20 + 32 * i), fill=(210, 210, 214))
            for x in (30, 260, 520, 760):
                ImageDraw.Draw(ruled).text((x, 28 + 32 * i), tr.choice(cwords) + str(tr.randrange(10, 9999)), fill=LIGHT.text, font=sfont)
        ra = np.asarray(ruled.convert("RGBA"), dtype=np.int16)
        got = align(ra[30:870, 30:970], ra[37:877, 33:973])
        check("a ruled table with varied rows aligns without a flag", (got.dx, got.dy, got.rival) == (-3, -7, None), str(got))
        # Two small dots, one per capture, 50 px apart in one column: two changes are not a column.
        dots = []
        for dy_dot in (300, 350):
            dot_im = scene(3, DARK, size=(1000, 800))
            ImageDraw.Draw(dot_im).ellipse((800, dy_dot, 816, dy_dot + 16), fill=(230, 60, 60))
            dots.append(np.asarray(dot_im.convert("RGBA"), dtype=np.int16))
        got = align(dots[0][20:780, 20:980], dots[1][29:789, 25:985])
        check("two dots in one column do not make a rival", (got.dx, got.dy, got.rival) == (-5, -9, None), str(got))
        # Three changes evenly spaced make a column; two equal gaps that do not touch do not.
        marks = np.zeros((300, 100), dtype=np.int32)
        for n, top in enumerate((10, 42, 90, 122), start=1):
            marks[top : top + 8, 20:40] = n
        check("two equal gaps apart are not a column", _box_period(marks, marks.size) == [], str(_box_period(marks, marks.size)))
        marks[154:162, 20:40] = 5
        check("three evenly spaced changes are a column", (0, 32) in _box_period(marks, marks.size), str(_box_period(marks, marks.size)))
        # A repeat step is never longer than the search reaches, so it cannot hide the other tests.
        far = Image.new("RGB", (1800, 1300), DARK.bg)
        for ry in range(10, 1290, 193):
            ImageDraw.Draw(far).text((76, ry + 4), "Alex Example  posted the weekly numbers", fill=DARK.name, font=tfont)
        far_steps = repeat_steps(_edges(np.asarray(far.convert("RGBA"), dtype=np.int16), 16), 192, 48)
        check("repeat steps stay inside the reach", all(max(abs(a), abs(b)) <= 192 for _, (a, b) in far_steps), str(far_steps[:3]))
        try:
            at_offset(np.asarray(scene(1, DARK).convert("RGBA"), dtype=np.int16), np.asarray(scene(2, DARK).convert("RGBA"), dtype=np.int16), 0, 37)
            named_refused = False
        except Refused:
            named_refused = True
        check("a named offset on two different screens is refused", named_refused)
        # Over 8 megapixels the neighbours are refined on a window of the page's text, which on a
        # sparse page is not in the middle.
        big = prose(9, size=(3880, 2200))
        got = align(big[10:2170, 10:3850], big[17:2177, 15:3855])
        check("a sparse 8 megapixel capture aligns exactly", (got.dx, got.dy) == (-5, -7), str(got))
        corner = np.zeros((3000, 3000), dtype=bool)
        corner[100:400, 2600:2900] = True
        check("the refinement window sits on the text, not the middle", bool(_window(corner)[0].any()))
        check("with no clean tile anywhere only the candidate's centre is scored", _near(corner, np.zeros((3000, 3000), dtype=bool), 7, 9, 2, 48) == [(7, 9)])
        rules = np.zeros((3000, 3000), dtype=bool)
        rules[::9, :] = True
        tied = _near(rules, rules, 0, 0, 2, 48)
        check("neighbours that tie in the windows all go to the full-size score", (0, 0) in tied and len(tied) > 1, str(tied))
        # A changed panel over the middle cannot blind the refinement: the quarters see past it.
        textured = np.zeros((3000, 3000), dtype=bool)
        textured[::7, :] = True
        textured[:, ::11] = True
        panel = textured.copy()
        panel[700:2300, 700:2300] = ~panel[700:2300, 700:2300]
        check("a changed panel in the middle does not hide the true neighbour", _near(textured, panel, 1, 1, 2, 48) == [(0, 0)], str(_near(textured, panel, 1, 1, 2, 48)))
        corner_panel = textured.copy()
        corner_panel[:1500, :1500] = ~corner_panel[:1500, :1500]
        check("a changed quarter does not hide the true neighbour", _near(textured, corner_panel, 1, 1, 2, 48) == [(0, 0)], str(_near(textured, corner_panel, 1, 1, 2, 48)))
        clear = np.zeros((200, 200, 4), dtype=np.int16)
        clear_proof = proof_sheet(clear, clear, Alignment(0, 0, 0.0), clear, [(50, 50, 60, 60)])
        check("the proof shows transparency on a checkerboard", int(clear_proof[40:100, :, :3].min()) >= 150, str(int(clear_proof[40:100, :, :3].min())))
        # On a capture over 2 megapixels the step is found on a reduced copy and refined at full size.
        rows = Image.new("RGB", (1800, 1300), DARK.bg)
        for ry in range(10, 1290, 111):
            ImageDraw.Draw(rows).ellipse((20, ry + 6, 60, ry + 46), fill=(120, 90, 200))
            ImageDraw.Draw(rows).text((76, ry + 4), "Alex Example  posted the weekly numbers", fill=DARK.name, font=tfont)
        rowe = _edges(np.asarray(rows.convert("RGBA"), dtype=np.int16), 16)
        share, step = repeat_steps(rowe, 192, 48)[0]
        check("a row step on a large capture is found exactly", step in ((0, 111), (0, -111)) and share > 0.9, f"{step} {share:.2f}")
        quiet = prose(8, size=(1200, 1000))
        got = align(quiet[50:850, 50:1150], quiet[61:861, 43:1143])
        check("a page that does not repeat aligns without a flag", (got.dx, got.dy, got.rival) == (7, -11, None), str(got))
        check("a page that aligns leaves little of the screen changed", got.changed < BIG_DIFF, str(got))
        # The merge proof puts the DONOR as captured beside the result: at the true offset the two
        # agree around the box, at a row alias the surroundings differ.
        qb, qd = quiet[50:850, 50:1150], quiet[61:861, 43:1143]
        for label, al_try, low in (("true", got, True), ("one row off", Alignment(got.dx, got.dy + 24, 0.0), False)):
            pf = proof_sheet(qb, qd, al_try, qb, [(500, 300, 640, 360)])
            band = 360 + 60 - (300 - 60)
            top, bottom = pf[28 : 28 + band, :, :3], pf[56 + band : 56 + 2 * band, :, :3]
            differs = float((np.abs(top - bottom).max(axis=2) > 16).mean())
            check(f"the merge proof at the {label} offset {'agrees' if low else 'differs'} around the box", (differs < 0.02) == low, f"{differs:.3f}")
        check("the merge proof spans the full width, so a label at the edge is in it", pf.shape[1] == qb.shape[1], str(pf.shape))
        pasted = qb.copy()
        pasted[250:410, :] = 0
        pf = proof_sheet(qb, qd, got, pasted, [(500, 300, 640, 360)])
        check("the proof shows BASE as captured, untouched by the paste", bool((pf[236:416, :, :3] == _on_board(qb[240:420])).all()))
        ghost = np.zeros((40, 40, 4), dtype=np.int16)
        ghost[10:30, 10:30, 3] = 255
        check("the proof shows a difference only in transparency", bool((_on_board(ghost) != _on_board(np.zeros_like(ghost))).any()))
        grown, inside = _changed(qb, qd, got.dx, got.dy + 700, 16)
        check("the changed area never reaches outside the overlap", not (grown & ~inside).any())
        plain = numbered(-1)
        got = align(plain[150:1150, 100:950], plain[174:1174, 100:950])
        check("a numbered table without overlays aligns exactly", (got.dx, got.dy) == (0, -24), str(got))
        wide_page = prose(6, size=(1300, 1600))
        got = align(wide_page[50:650, 50:1250], wide_page[450:1050, 50:1250], max_shift=512)
        check("a 400 px shift is found with --max-shift 512", (got.dx, got.dy) == (0, -400), str(got))
        got = align(wide_page[50:650, 50:1250], wide_page[530:1130, 50:1250], max_shift=512)
        check("a 480 px shift with a thin overlap is found when the page does not repeat", (got.dx, got.dy) == (0, -480), str(got))
        far_b, far_d, _, _ = pair(shift=(0, 96))
        far = align(far_b, far_d)
        check("a true shift of 96 pixels beats a closer wrong one", (far.dx, far.dy) == (0, 96), str(far))
        thin = np.concatenate([np.asarray(scene(5).convert("RGBA"), dtype=np.int16)[300:372]] * 2, axis=1)
        thin_al = align(thin[8:], thin[:-8])
        check("a 64-pixel-high strip finds a vertical shift", (thin_al.dx, thin_al.dy) == (0, 8), str(thin_al))
        clear = Image.new("RGBA", (400, 160), (0, 0, 0, 0))
        ImageDraw.Draw(clear).text((30, 60), "black text on transparency", fill=(0, 0, 0, 255), font=ImageFont.load_default(size=22))
        clear_a = np.asarray(clear, dtype=np.int16)
        clear_al = align(clear_a[:, 5:], clear_a[:, :-5])
        check("dark content on transparency aligns", (clear_al.dx, clear_al.dy) == (5, 0), str(clear_al))
        al = align(base, donor)
        out, _ = merge(base, donor, al, [(120, 40, 260, 170)], False)
        outside = np.ones(base.shape[:2], dtype=bool)
        outside[40:170, 120:260] = False
        check("a paste never leaves the box it was given", np.array_equal(out[outside], base[outside]))
        _, report_dup = merge(base, donor, al, [(120, 40, 260, 170), (120, 40, 260, 170)], False)
        check("merge deduplicates identical boxes", len(report_dup) == 1)
        check("--rect refuses a box the DONOR does not fully cover", refuses(lambda: merge(base, donor, al, [(880, 10, 900, 40)], True)))
        ghost = donor.copy()
        ghost[600:690, 0:300, 3] = 0
        ghost_b = base.copy()
        ghost_b[589:679, 7:307, 3] = 0  # BASE(x, y) == DONOR(x - 7, y + 11)
        ghost[600:690, 0:300, :3] = np.random.default_rng(1).integers(0, 256, (90, 300, 3))
        gal = align(ghost_b, ghost)
        check("colors under full transparency are ignored", (gal.dx, gal.dy) == (-7, 11)
              and not [c for c in components(ghost_b, ghost, gal) if c.y0 > 560], str(gal))
        check("a reversed region is refused", refuses(lambda: merge(base, donor, al, [(300, 200, 100, 50)], False), Unreadable))
        check("an empty region is refused", refuses(lambda: merge(base, donor, al, [(800, 600, 840, 640)], False)))

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "ps.png").write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n")
            try:
                load(t / "ps.png")
                cause = None
            except Unreadable as exc:
                cause = exc.__cause__
            # Not merely refused: refused because no allowed decoder knows it, with or without Ghostscript.
            check("a PostScript file named .png is refused before any decoder runs", isinstance(cause, UnidentifiedImageError), repr(cause))
            Image.new("RGB", (100, 100)).save(t / "big.png")
            check("an image above the pixel cap is refused", refuses(lambda: load(t / "big.png", max_pixels=5000), Unreadable))
            rgba = base.copy()
            rgba[:40, :40, 3] = 0
            save_verified(rgba, t / "a.png", comment="composite")
            back = load(t / "a.png").rgba
            check("transparency survives the write", np.array_equal(back, rgba))
            with Image.open(t / "a.png") as im:
                check("the output says it is a composite", im.info.get("Comment") == "composite")
            Image.fromarray(base[..., :3].astype(np.uint8)).save(t / "b.png")
            Image.fromarray(donor[..., :3].astype(np.uint8)).save(t / "d.png")
            with contextlib.redirect_stderr(io.StringIO()):
                rc = main(["merge", str(t / "b.png"), str(t / "d.png"), "--pick", "1", "--out", str(t / "o.jpg")])
            check("a non-PNG output is refused before writing", rc == 2 and not (t / "o.jpg").exists())
            with contextlib.redirect_stderr(io.StringIO()):
                rc = main(["merge", str(t / "b.png"), str(t / "d.png"), "--pick", "1", "--out", str(t / "b.png")])
            check("an output that overwrites an input is refused", rc == 2)
            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(io.StringIO()):
                rc_align = main(["align", str(t / "b.png"), str(t / "d.png")])
                rc_list = main(["list", str(t / "b.png"), str(t / "d.png"), "--sheet", str(t / "s.png")])
            lines = [json.loads(x) for x in out_buf.getvalue().splitlines()]
            Image.fromarray(pane[..., :3].astype(np.uint8)).save(t / "p.png")
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                main(["align", str(t / "b.png"), str(t / "p.png")])
            check("the command line warns when much of the screen differs", "much of the screen differs" in err.getvalue())
            Image.fromarray(numbered(-1)[150:1150, 100:950].astype(np.uint8)).save(t / "n1.png")
            Image.fromarray(numbered(-1)[174:1174, 100:950].astype(np.uint8)).save(t / "n2.png")
            err = io.StringIO()
            out_doubt = io.StringIO()
            with contextlib.redirect_stdout(out_doubt), contextlib.redirect_stderr(err):
                main(["align", str(t / "n1.png"), str(t / "n2.png")])
            doubt_json = json.loads(out_doubt.getvalue().splitlines()[0]) if out_doubt.getvalue() else {}
            check("a doubtful offset is flagged on stderr and in the JSON",
                  "may be the true one" in err.getvalue() and doubt_json.get("rival") is not None, err.getvalue()[:120])
            check("--offset takes a negative DX on every Python version",
                  _join_offset(["list", "a", "b", "--offset", "-5,-9"]) == ["list", "a", "b", "--offset=-5,-9"])
            Image.fromarray(pages[0].astype(np.uint8)).save(t / "p1.png")
            Image.fromarray(pages[1].astype(np.uint8)).save(t / "p2.png")
            err_big = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err_big):
                main(["align", str(t / "p1.png"), str(t / "p2.png")])
            check("the command line warns when much of the screen still differs", "of the screen differs after alignment" in err_big.getvalue(), err_big.getvalue()[:120])
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                out_anchor = io.StringIO()
                with contextlib.redirect_stdout(out_anchor):
                    rc_anchor = main(["align", str(t / "p1.png"), str(t / "p2.png"), "--anchor", "10,5,100,700"])
                rc_both = main(["list", str(t / "p1.png"), str(t / "p2.png"), "--sheet", str(t / "x.png"), "--anchor", "0,0,100,700", "--offset", "0,0"])
            anchored = json.loads(out_anchor.getvalue() or "{}")
            check("--anchor works on the command line, and not together with --offset",
                  (rc_anchor, rc_both, anchored.get("dx"), anchored.get("dy")) == (0, 2, 0, 0), f"{rc_anchor} {rc_both} {anchored}")
            # A named offset gets the same trust rule and warnings as a found one.
            for offset, want_rc, warns in (("0,-24", 0, False), ("40,-24", 0, True), ("2000,0", 1, False)):
                err_off = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err_off):
                    rc = main(["list", str(t / "n1.png"), str(t / "n2.png"), "--sheet", str(t / "off.png"), "--offset", offset])
                check(f"--offset {offset} is checked like a found offset",
                      (rc, "much of the screen differs" in err_off.getvalue()) == (want_rc, warns), f"rc {rc} {err_off.getvalue()[:80]}")
            wide_w = scene(8, DARK, size=(1500, 800))
            wide_tip = wide_w.copy()
            tooltip(wide_tip, DARK, 900, 300, ["Copy link", "Edited 2 min ago"])
            wide_b = np.asarray(wide_w.crop((40, 40, 1440, 740)).convert("RGBA"), dtype=np.int16)
            wide_d = np.asarray(wide_tip.crop((47, 29, 1447, 729)).convert("RGBA"), dtype=np.int16)
            wal = align(wide_b, wide_d)
            sheet = contact_sheet(wide_b, wide_d, wal, components(wide_b, wide_d, wal))
            k = SHEET_LONG / (2 * wide_b.shape[1] + 16)
            first = components(wide_b, wide_d, wal)[0]
            corners = [sheet[round(first.y0 * k) + 1, round((first.x0 + off) * k) + 1, :3].tolist()
                       for off in (0, wide_b.shape[1] + 16)]
            check("a wide contact sheet is scaled, and its boxes land on both panels",
                  sheet.shape[1] == SHEET_LONG and all(c == [255, 40, 40] for c in corners), str(corners))
            tall_b, tall_d = np.concatenate([base] * 3, axis=0), np.concatenate([donor] * 3, axis=0)
            tall_sheet = contact_sheet(tall_b, tall_d, Alignment(-7, 11, 0.0), [])
            check("a tall contact sheet is scaled on its long edge", max(tall_sheet.shape[:2]) == SHEET_LONG, str(tall_sheet.shape))
            check("align and list run from the command line", rc_align == 0 and rc_list == 0
                  and (lines[0]["dx"], lines[0]["dy"]) == (-7, 11) and lines[1]["components"] and (t / "s.png").exists())
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                rc = main(["merge", str(t / "b.png"), str(t / "d.png"), "--region", "800,600,840,640", "--out", str(t / "r.png")])
            check("a refused merge leaves no file", rc == 1 and not (t / "r.png").exists() and not list(t.glob("tmp*")))
            profile = b"invented-profile-bytes"
            Image.fromarray(base[..., :3].astype(np.uint8)).save(t / "bi.png", icc_profile=profile)
            Image.fromarray(donor[..., :3].astype(np.uint8)).save(t / "di.png", icc_profile=profile)
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                rc = main(["merge", str(t / "bi.png"), str(t / "di.png"), "--pick", "1", "--out", str(t / "mi.png"), "--proof", str(t / "pf.png")])
            with Image.open(t / "mi.png") as im:
                check("the merge keeps the BASE color profile", rc == 0 and im.info.get("icc_profile") == profile)
                check("the merge records its offset in the PNG comment", "offset (-7, 11) searched" in im.info.get("Comment", ""), im.info.get("Comment", "")[:120])
            with Image.open(t / "pf.png") as im:
                check("--proof writes the DONOR above the result", im.width > 200 and im.height > 60, str(im.size))
            out_dup = io.StringIO()
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out_dup):
                rc_dup = main(["merge", str(t / "bi.png"), str(t / "di.png"), "--pick", "1,1", "--out", str(t / "mdup.png"), "--proof", str(t / "pfdup.png")])
            dup_report = json.loads(out_dup.getvalue().splitlines()[0]) if out_dup.getvalue() else {}
            check("--pick deduplicates component numbers", rc_dup == 0 and len(dup_report.get("pasted", [])) == 1)
            with Image.open(t / "pf.png") as im_single, Image.open(t / "pfdup.png") as im_dup:
                check("deduplicated pick produces identical proof sheet height", im_dup.size == im_single.size)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = main(["merge", str(t / "bi.png"), str(t / "di.png"), "--pick", "1", "--out", str(t / "same.png"), "--proof", str(t / "same.png")])
            check("--proof and --out may not name the same file", rc == 2 and not (t / "same.png").exists(), str(rc))
            try:
                (t / "alias").symlink_to(t)
                other = t / "alias" / "x.png"
            except OSError:  # Windows without the right to make links
                (t / "sub").mkdir()
                other = t / "sub" / ".." / "x.png"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = main(["merge", str(t / "bi.png"), str(t / "di.png"), "--pick", "1", "--out", str(t / "x.png"), "--proof", str(other)])
            check("--proof and --out may not reach the same file through a link", rc == 2, str(rc))
            (t / "probe.png").write_bytes(b"")
            folds_case = (t / "PROBE.png").exists()
            (t / "case.png").write_bytes(b"kept")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = main(["merge", str(t / "bi.png"), str(t / "di.png"), "--pick", "1", "--out", str(t / "case.png"), "--proof", str(t / "CASE.png")])
            kept = (t / "case.png").read_bytes() == b"kept" if folds_case else True
            check("--proof and --out may not differ only in case on a disk that ignores case, and nothing is lost",
                  rc == (2 if folds_case else 0) and kept, f"{rc} {folds_case} {kept}")
            mode = (t / "mi.png").stat().st_mode & 0o777
            umask = os.umask(0)
            os.umask(umask)
            check("the output gets ordinary file permissions", mode == 0o666 & ~umask, oct(mode))
    except Exception as exc:  # a crash is a failed selftest, never a pass
        check("selftest ran to the end", False, f"{type(exc).__name__}: {exc}")

    print(f"selftest: {len(failures)} failed" if failures else "selftest: all checks pass")
    return 4 if failures else 0


if __name__ == "__main__":
    sys.exit(selftest())
