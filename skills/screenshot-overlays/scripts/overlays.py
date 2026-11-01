#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file of this skill.
"""Merge the transient overlays of two screenshots of one screen into a single image.

Purpose: two captures of the same UI, each showing a different hover tooltip, popover or menu, are
    aligned on the pixels they share. The overlay you pick is cut out of one capture along its own
    outline and pasted into the other. Refuses when the two captures do not line up.
Usage:
    python3 overlays.py align BASE.png DONOR.png [--anchor X0,Y0,X1,Y1]
    python3 overlays.py list  BASE.png DONOR.png --sheet SHEET.png [--offset=DX,DY | --anchor X0,Y0,X1,Y1]
    python3 overlays.py merge BASE.png DONOR.png (--pick 2[,5] | --region X0,Y0,X1,Y1) [--rect]
                              [--offset=DX,DY | --anchor X0,Y0,X1,Y1] [--proof PROOF.png] --out OUT.png
    python3 overlays.py --selftest (or selftest)
    Every command also takes --edge N (default 16) and --max-shift N (default 96).

Coordinates are BASE pixels. The offset (dx, dy) means BASE(x, y) shows what DONOR(x + dx, y + dy)
shows. Every result is one JSON line on stdout. Exit codes: 0 ok, 1 refused (nothing written),
2 usage or unreadable input, 3 dependency missing, 4 selftest failed, 5 internal error.
Needs Python 3.11+ with numpy==2.3.4 and pillow==12.3.0. Reads PNG and JPEG only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, PngImagePlugin
except ImportError as exc:
    print(
        f"overlays: missing dependency {exc.name!r}; install numpy==2.3.4 and pillow==12.3.0",
        file=sys.stderr,
    )
    sys.exit(3)

# Only decoders that need no helper program. Pillow otherwise picks the decoder from the file's
# content, whatever its name, and its EPS decoder runs Ghostscript.
FORMATS = ["PNG", "JPEG"]
# An 8K display is 33 megapixels. Everything is held as int16 RGBA: a 40 MP pair peaked at 3.7 GB.
MAX_PIXELS = 40_000_000
MAX_SHIFT = 96
SHIFT_CEILING = 512
# The mismatch of every shift is computed on a copy reduced to about this many pixels; the best
# CANDIDATES local minima are then measured exactly at full size, within the reduction factor.
CORRELATE_PIXELS = 2_000_000
CANDIDATES = 16
# The offset is checked tile by tile. A tile is INFORMATIVE when it holds edges (text, outlines,
# icons) in either capture, and it AGREES when at most TILE_XOR of those edges are in one capture
# only. A hover tint moves no edge, so it never makes a tile disagree.
TILE = 48
TILE_MIN_EDGES = 24
TILE_XOR = 0.3
TILE_CLEAN = 0.05
# A BASE that stays clean against itself moved by some step, on at least this share of its textured
# tiles, repeats: every offset then has an alias at that step, and Alignment.rival names the alias
# that fits the pair best. A hint, not a guarantee: SKILL.md lists the measured rates and misses.
REPEAT = 0.45
# Share of the overlap that still differs after alignment, grown by GROW, above which the offset may
# follow one big overlay that moved instead of the screen behind it, and the command line warns.
# Measured on invented scenes: correct pairs with tooltip-sized overlays 0.074 or less, a correct
# pair with a menu over half the screen 0.19 to 0.61, and a 600 by 500 px popover that decided a
# wrong offset over a 900 by 700 px page 0.18 to 0.21.
BIG_DIFF = 0.12
# The offset is trusted only when at least MIN_AGREE tiles agree, and the agreeing tiles span at
# least MIN_SPREAD of the overlap's width or height. A capture at another scale agrees only in a
# narrow band around one point, and two unrelated screens agree almost nowhere.
MIN_AGREE = 6
MIN_SPREAD = 0.33
# Two offsets more than 4 px apart whose edge mismatch differs by less than this are ambiguous: the
# content repeats, and the pair is refused rather than aligned on a guess.
AMBIGUOUS = 0.05
# Per-channel difference above which a pixel belongs to an overlay. Measured on a dark UI: a hover
# row tint differs by 7 to 12, a tooltip outline by about 30, and the antialiased tip of its arrow
# by 18. 16 keeps such a tint out of every mask and still closes the outline at the tip. A tint
# stronger than 16 needs a higher --edge.
EDGE = 16
# Pixels that join nearby differences into one component, so the letters of one tooltip join.
GROW = 6
MIN_PIXELS = 8
MAX_COMPONENTS = 400
# A model's vision input is scaled so its long edge is about this size, so the sheet is drawn at it.
SHEET_LONG = 1568
# The residual is the share of informative tiles that disagree at the chosen offset. An overlay, a
# changed timestamp or another screen state all raise it, and only the contact sheet tells them
# apart, so a high residual warns and never refuses. Measured on invented scenes: a matching pair 0.24,
# a pane of other content over 57 percent 0.47, the same layout with other text 0.72 to 0.78.
WARN_RESIDUAL = 0.4
# Solidity is the share of the paste's bounding box that either differs or is enclosed by an outline
# without differing; only the one-pixel seal around a stroke counts as neither. Measured: text with
# no outline 0.34 to 0.38, invented tooltips 0.73 to 0.84. Below MIN_SOLID the paste is thin strokes.
MIN_SOLID = 0.6


class Refused(Exception):
    """The inputs cannot give a trustworthy result; nothing is written."""


class Unreadable(Exception):
    """An input or an option is not usable at all."""


@dataclass(frozen=True)
class Alignment:
    dx: int
    dy: int
    residual: float
    # A second offset that may be the true one, or None: an alias of a BASE that repeats, or the
    # offset of what is left when a big overlay explains the chosen one (see align).
    rival: tuple[int, int] | None = None
    # Share of the overlap that still differs after alignment, grown as components are.
    changed: float = 0.0


@dataclass(frozen=True)
class Box:
    n: int
    x0: int
    y0: int
    x1: int
    y1: int
    pixels: int


@dataclass(frozen=True)
class Capture:
    rgba: np.ndarray
    icc: bytes | None
    lossy: bool


def load(path: Path, max_pixels: int = MAX_PIXELS) -> Capture:
    try:
        with Image.open(path, formats=FORMATS) as im:
            if im.width * im.height > max_pixels:
                raise Unreadable(f"{path} is {im.width}x{im.height}, above {max_pixels:,} pixels")
            if im.mode in ("I", "I;16", "I;16B", "I;16L", "I;16N", "F"):
                raise Unreadable(f"{path} is a {im.mode} image; save it as an 8-bit PNG")
            return Capture(
                np.asarray(im.convert("RGBA"), dtype=np.int16),
                im.info.get("icc_profile"),
                im.format == "JPEG",
            )
    except Image.DecompressionBombError as exc:
        raise Unreadable(f"{path}: {exc}") from exc
    except MemoryError as exc:
        raise Unreadable(f"{path} does not fit in memory") from exc
    except (OSError, ValueError) as exc:
        raise Unreadable(f"{path} is not a readable PNG or JPEG ({exc})") from exc


def _visible(rgba: np.ndarray) -> np.ndarray:
    """RGB premultiplied by alpha, plus alpha: what a viewer sees, so a value under alpha 0 is ignored."""
    a = rgba[..., 3:4].astype(np.int32)
    return np.concatenate(((rgba[..., :3].astype(np.int32) * a // 255), a), axis=2).astype(np.int16)


def _edges(rgba: np.ndarray, edge: int) -> np.ndarray:
    """Pixels where any color channel steps by more than `edge`: text, outlines, icons, not a tint.

    Colors are taken as shown over a mid-gray backdrop, so dark content on transparency has edges,
    and per channel, so two colors of equal brightness still differ."""
    v = _visible(rgba).astype(np.float32)
    shown = v[..., :3] + (255 - v[..., 3:4]) * 0.5
    out = np.zeros(rgba.shape[:2], dtype=bool)
    out[:, :-1] |= (np.abs(np.diff(shown, axis=1)) > edge).any(axis=2)
    out[:-1, :] |= (np.abs(np.diff(shown, axis=0)) > edge).any(axis=2)
    return out


def _shrink(gray: np.ndarray, f: int) -> np.ndarray:
    if f == 1:
        return gray
    h, w = (gray.shape[0] // f) * f, (gray.shape[1] // f) * f
    return gray[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3))


def _overlap(base: np.ndarray, donor: np.ndarray, dx: int, dy: int, min_side: int = 16):
    """The base window and the donor window that show the same content under (dx, dy)."""
    h, w = base.shape[:2]
    hd, wd = donor.shape[:2]
    y0, y1 = max(0, -dy), min(h, hd - dy)
    x0, x1 = max(0, -dx), min(w, wd - dx)
    if y1 - y0 < min_side or x1 - x0 < min_side:
        return None
    return (y0, y1, x0, x1), base[y0:y1, x0:x1], donor[y0 + dy : y1 + dy, x0 + dx : x1 + dx]


def _mismatch_map(a: np.ndarray, b: np.ndarray, max_shift: int):
    """Edge mismatch and overlap area for EVERY shift within max_shift, by three FFT correlations.

    For a shift (dx, dy), mismatch is the share of edge pixels in the overlap that only one capture
    has: 1 - both / either. Returns (mismatch, area) as (2 * max_shift + 1) square arrays indexed
    [dy + max_shift, dx + max_shift]. The canvas is padded by max_shift, so no shift wraps around."""
    n = max(a.shape[0], b.shape[0]) + max_shift
    m = max(a.shape[1], b.shape[1]) + max_shift

    def spectrum(x: np.ndarray) -> np.ndarray:
        out = np.zeros((n, m), dtype=np.float64)
        out[: x.shape[0], : x.shape[1]] = x
        return np.fft.rfft2(out)

    fa, fb = spectrum(a), spectrum(b)
    fa1, fb1 = spectrum(np.ones(a.shape)), spectrum(np.ones(b.shape))
    lags = np.arange(-max_shift, max_shift + 1)
    rows, cols = lags % n, lags % m

    def corr(p: np.ndarray, q: np.ndarray) -> np.ndarray:
        return np.fft.irfft2(p.conj() * q, s=(n, m))[np.ix_(rows, cols)]

    both = corr(fa, fb)
    either = corr(fa, fb1) + corr(fa1, fb) - both
    area = np.rint(corr(fa1, fb1))
    with np.errstate(divide="ignore", invalid="ignore"):
        mismatch = np.where(either > 0.5, 1 - both / either, 1.0)
    return np.clip(mismatch, 0.0, 1.0), area


def _tiles(ea: np.ndarray, eb: np.ndarray, dx: int, dy: int, tile: int):
    """(agreeing, clean, informative, spread) tiles of the edge maps at (dx, dy), or None.

    A tile AGREES when at most TILE_XOR of its edges are in one capture only, and is CLEAN when at
    most TILE_CLEAN are. An overlay spoils a few tiles completely; a wrong offset on repeating
    content spoils many tiles a little, so the clean share tells the two apart."""
    o = _overlap(ea, eb, dx, dy, max(tile, 16))
    if o is None:
        return None
    a, b = o[1], o[2]
    h, w = (a.shape[0] // tile) * tile, (a.shape[1] // tile) * tile
    shape = (h // tile, tile, w // tile, tile)
    either = (a[:h, :w] | b[:h, :w]).reshape(shape).sum(axis=(1, 3))
    once = (a[:h, :w] ^ b[:h, :w]).reshape(shape).sum(axis=(1, 3))
    informative = either >= TILE_MIN_EDGES * (tile / TILE) ** 2
    agree = informative & (once <= TILE_XOR * either)
    clean = informative & (once <= TILE_CLEAN * either)
    rows, cols = np.nonzero(agree)
    spread = 0.0
    if len(rows):
        spread = max((rows.max() - rows.min() + 1) / shape[0], (cols.max() - cols.min() + 1) / shape[2])
    return int(agree.sum()), int(clean.sum()), int(informative.sum()), float(spread)


def _clean_grid(ea: np.ndarray, eb: np.ndarray, dx: int, dy: int, tile: int) -> tuple[np.ndarray, np.ndarray]:
    """(clean, detailed) tiles at (dx, dy) on a grid fixed to BASE, so that two offsets can be
    compared tile by tile. A tile not wholly inside the overlap is neither."""
    gh, gw = ea.shape[0] // tile, ea.shape[1] // tile
    moved = np.zeros((gh * tile, gw * tile), dtype=bool)
    inside = np.zeros_like(moved)
    y0, y1 = max(0, -dy), min(gh * tile, eb.shape[0] - dy)
    x0, x1 = max(0, -dx), min(gw * tile, eb.shape[1] - dx)
    if y1 > y0 and x1 > x0:
        moved[y0:y1, x0:x1] = eb[y0 + dy : y1 + dy, x0 + dx : x1 + dx]
        inside[y0:y1, x0:x1] = True
    a = ea[: gh * tile, : gw * tile]
    shape = (gh, tile, gw, tile)
    either = (a | moved).reshape(shape).sum(axis=(1, 3))
    once = (a ^ moved).reshape(shape).sum(axis=(1, 3))
    detailed = inside.reshape(shape).all(axis=(1, 3)) & (either >= TILE_MIN_EDGES * (tile / TILE) ** 2)
    return detailed & (once <= TILE_CLEAN * either), detailed


def _textured(e: np.ndarray, tile: int) -> np.ndarray:
    """Tiles whose edges span at least 4 rows and 4 columns. A tile that holds only a ruled line, an
    input border or a grid line stays clean when moved along it, which is not repetition."""
    gh, gw = e.shape[0] // tile, e.shape[1] // tile
    t = e[: gh * tile, : gw * tile].reshape(gh, tile, gw, tile)
    return (t.any(axis=3).sum(axis=1) >= 4) & (t.any(axis=1).sum(axis=2) >= 4)


def second_motion(e1: np.ndarray, e2: np.ndarray, dx: int, dy: int, reach: int, tile: int) -> tuple[int, int] | None:
    """The offset of what (dx, dy) leaves unexplained: the candidate that makes the most tiles clean
    that are not clean at (dx, dy). When a big overlay that moved decides the offset, this is the
    offset of the screen behind it."""
    clean = _clean_grid(e1, e2, dx, dy, tile)[0]
    f = max(1, int(np.ceil(np.sqrt(max(e1.size, e2.size) / CORRELATE_PIXELS))))
    r = max(1, reach // f)
    mismatch, area = _mismatch_map(_shrink(e1.astype(np.float32), f), _shrink(e2.astype(np.float32), f), r)
    lags = np.arange(-r, r + 1)
    best = (0, None)
    for row, col in _minima(mismatch, area > 0, CANDIDATES):
        cx, cy = int(lags[col]) * f, int(lags[row]) * f
        for ox, oy in _near(e1, e2, cx, cy, max(f - 1, 1), tile):
            if max(abs(ox - dx), abs(oy - dy)) <= 4 or max(abs(ox), abs(oy)) > reach:
                continue
            gain = int((_clean_grid(e1, e2, ox, oy, tile)[0] & ~clean).sum())
            if gain > best[0]:
                best = (gain, (ox, oy))
    return best[1]


def _windows(e: np.ndarray) -> list[tuple[np.ndarray, int, int]]:
    """In each quarter of an edge map, the part that holds the most edges, with its top-left corner;
    CORRELATE_PIXELS in all. A sparse page can have nothing in its middle, and one changed region
    cannot cover all four."""
    h, w = e.shape
    out = []
    for qy in (0, h // 2):
        for qx in (0, w // 2):
            a, x0, y0 = _window(e[qy : qy + (h + 1) // 2, qx : qx + (w + 1) // 2], CORRELATE_PIXELS // 4)
            out.append((a, x0 + qx, y0 + qy))
    return out


def _window(e: np.ndarray, pixels: int = CORRELATE_PIXELS) -> tuple[np.ndarray, int, int]:
    """The part of an edge map with at most `pixels` that holds the most edges, and its top-left
    corner."""
    h, w = e.shape
    k = min(1.0, float(np.sqrt(pixels / (h * w))))
    wh, ww = max(1, int(h * k)), max(1, int(w * k))
    # The window position with the most edges, on a grid of 32 px cells.
    c = max(1, min(32, h, w))
    cells = e[: h // c * c, : w // c * c].reshape(h // c, c, w // c, c).sum(axis=(1, 3))
    sat = np.pad(cells.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    kh, kw = max(1, min(cells.shape[0], wh // c)), max(1, min(cells.shape[1], ww // c))
    sums = sat[kh:, kw:] - sat[:-kh, kw:] - sat[kh:, :-kw] + sat[:-kh, :-kw]
    gy, gx = np.unravel_index(int(np.argmax(sums)), sums.shape)
    y0, x0 = min(gy * c, h - wh), min(gx * c, w - ww)
    return e[y0 : y0 + wh, x0 : x0 + ww], x0, y0


def _near(ea: np.ndarray, eb: np.ndarray, cx: int, cy: int, near: int, tile: int) -> list[tuple[int, int]]:
    """The offsets around (cx, cy) to score at full size. Within 1 px, all of them. Further out,
    which a capture over 8 megapixels needs, only the one with the most clean tiles in the densest
    part of each quarter of BASE: scoring every neighbour at full size grows with the square of
    the pixel count (117 s at 40 MP). With no clean tile anywhere the candidate is wrong, and its
    centre is enough."""
    grid = [(cx + i, cy + j) for j in range(-near, near + 1) for i in range(-near, near + 1)]
    if near <= 1:
        return grid
    parts = _windows(ea)
    best, pick = 0, [(cx, cy)]
    for dx, dy in grid:
        clean = sum((got or (0, 0))[1] for got in (_tiles(a, eb, dx + x0, dy + y0, tile) for a, x0, y0 in parts))
        if clean > best:
            best, pick = clean, [(dx, dy)]
        elif clean == best and best:
            pick.append((dx, dy))  # a tie goes to the full-size score, not to the loop order
    return pick


def _box_period(labels: np.ndarray, area: int) -> list[tuple[int, int]]:
    """Steps at which at least 3 small changed regions repeat in one column or one row, spaced
    evenly: the trace a row alias leaves on per-row digits. Values that update between the
    captures leave it at a correct offset too."""
    try:
        boxes = [b for b in _boxes(labels) if (b[2] - b[0]) * (b[3] - b[1]) < 0.01 * area]
    except Refused:  # too many changes to read a pattern from; `list` refuses such a pair anyway
        return []
    found = set()
    for axis in (1, 0):
        lo, hi = (0, 2) if axis == 1 else (1, 3)
        columns = {
            tuple(sorted({b[axis] for b in boxes if abs(b[lo] - a[lo]) <= 6 and abs(b[hi] - a[hi]) <= 6}))
            for a in boxes
        }
        for starts in columns:
            near = {t + j for t in starts for j in range(-2, 3)}
            for i, p in enumerate(starts):
                for q in starts[i + 1 :]:
                    if q - p > 4 and q + (q - p) in near:
                        found.add((0, q - p) if axis == 1 else (q - p, 0))
    return sorted(found)


def repeat_steps(e: np.ndarray, reach: int, tile: int) -> list[tuple[float, tuple[int, int]]]:
    """How a capture repeats itself: (share, step) for each candidate step more than 4 px long and
    at most `reach` px, best first, where share is the part of its textured tiles that stay clean
    against the capture moved by that step."""
    keep = _textured(e, tile)
    f = max(1, int(np.ceil(np.sqrt(e.size / CORRELATE_PIXELS))))
    small = _shrink(e.astype(np.float32), f)
    r = max(1, reach // f)
    mismatch, area = _mismatch_map(small, small, r)
    lags = np.arange(-r, r + 1)
    steps = []
    for row, col in _minima(mismatch, area > 0, CANDIDATES + 1):
        cx, cy = int(lags[col]) * f, int(lags[row]) * f
        best = (0.0, None)
        # Refined at full size: on a reduced copy a 111 px row step reads as 112, which scores low.
        for sx, sy in _near(e, e, cx, cy, f - 1, tile):
            if max(abs(sx), abs(sy)) <= 4 or max(abs(sx), abs(sy)) > reach:
                continue
            clean, detailed = _clean_grid(e, e, sx, sy, tile)
            n = int((detailed & keep).sum())
            if n >= MIN_AGREE and (clean & keep).sum() / n > best[0]:
                best = (int((clean & keep).sum()) / n, (sx, sy))
        if best[1] is not None:
            steps.append(best)
    return sorted(steps, reverse=True)


def _mismatch(ea: np.ndarray, eb: np.ndarray, dx: int, dy: int) -> float:
    """Share of edge pixels in the overlap at (dx, dy) that only one capture has."""
    o = _overlap(ea, eb, dx, dy, 1)
    if o is None:
        return 1.0
    either = int((o[1] | o[2]).sum())
    return int((o[1] ^ o[2]).sum()) / either if either else 1.0


def _minima(field: np.ndarray, usable: np.ndarray, k: int) -> list[tuple[int, int]]:
    """Up to k (row, col) local minima of field where usable, lowest first."""
    padded = np.pad(np.where(usable, field, np.inf), 1, constant_values=np.inf)
    centre = padded[1:-1, 1:-1]
    lowest = np.ones(centre.shape, dtype=bool)
    for y in range(3):
        for x in range(3):
            if (y, x) != (1, 1):
                lowest &= centre <= padded[y : y + centre.shape[0], x : x + centre.shape[1]]
    rows, cols = np.nonzero(lowest & usable)
    order = np.argsort(centre[rows, cols], kind="stable")[:k]
    return [(int(rows[i]), int(cols[i])) for i in order]


def _tile_size(e1: np.ndarray, e2: np.ndarray) -> int:
    small = min(*e1.shape, *e2.shape)
    return TILE if small >= 4 * TILE else max(8, small // 4)


def at_anchor(base, donor, anchor, max_shift: int = MAX_SHIFT, edge: int = EDGE) -> Alignment:
    """Align on one part of BASE only, such as a heading, a line-number gutter or a column of ids,
    for when the whole screen repeats or a big overlay decides the offset. The part should hold
    text that appears once and no overlay."""
    x0, y0, x1, y1 = check_box(tuple(anchor), base.shape)
    px, py = min(max_shift, x0), min(max_shift, y0)
    try:
        part = align(base[y0:y1, x0:x1], donor[y0 - py : y1 + max_shift, x0 - px : x1 + max_shift], 2 * max_shift, edge)
    except Refused as exc:
        raise Refused(
            f"the anchor {list(anchor)} does not line the captures up on its own. Choose a part with text"
            " that appears once, such as a heading, and nothing that repeats beside it"
        ) from exc
    dx, dy = part.dx - px, part.dy - py
    if max(abs(dx), abs(dy)) > max_shift:
        raise Refused(f"on the anchor the best offset, ({dx}, {dy}), lies beyond --max-shift {max_shift}")
    # The anchor's own rival comes from what repeats inside it, such as a column of ids, so it is
    # dropped; the residual is the whole screen's, as for any other offset.
    found = at_offset(base, donor, dx, dy, edge)
    return Alignment(dx, dy, found.residual, None, found.changed)


def at_offset(base: np.ndarray, donor: np.ndarray, dx: int, dy: int, edge: int = EDGE) -> Alignment:
    """A given offset, held to the same trust rule as a found one: for when the contact sheet shows
    that align chose a row alias and the person names the true offset."""
    e1, e2 = _edges(base, edge), _edges(donor, edge)
    got = _tiles(e1, e2, dx, dy, _tile_size(e1, e2))
    if got is None or got[2] < MIN_AGREE:
        raise Refused(f"at ({dx}, {dy}) the captures share too little text or icons to check the offset")
    agree, _, informative, spread = got
    if agree < MIN_AGREE or spread < MIN_SPREAD:
        raise Refused(
            f"({dx}, {dy}) does not line the captures up: {agree} of {informative} detailed tiles agree,"
            f" spanning {spread:.0%} of the image"
        )
    changed, valid = _changed(base, donor, dx, dy, edge)
    share = float(changed.sum() / max(1, valid.sum()))
    return Alignment(dx, dy, round(1 - agree / informative, 4), None, round(share, 4))


def _changed(base: np.ndarray, donor: np.ndarray, dx: int, dy: int, edge: int) -> tuple[np.ndarray, np.ndarray]:
    """Where the aligned captures still differ, grown as components are, and where DONOR is defined."""
    warped, valid = donor_in_base(base, donor, Alignment(dx, dy, 0.0))
    return _grow(_diff(base, warped, valid, edge), GROW * 2 + 1) & valid, valid


def _best_alias(e1, e2, dx: int, dy: int, steps: list[tuple[int, int]], max_shift: int, tile: int):
    """Of (dx, dy) moved by each step or its double either way, the offset inside max_shift that fits
    the pair best. A step can point either way, the truth can lie two rows away, and the best repeat
    step can be a multiple of the row height."""
    aliases = {(dx + k * st[0], dy + k * st[1]) for st in steps for k in (-2, -1, 1, 2)}
    fits = [(_tiles(e1, e2, *a, tile), a) for a in aliases if max(abs(a[0]), abs(a[1])) <= max_shift]
    fits = [(g[1] / g[2], a) for g, a in fits if g is not None and g[2]]
    return max(fits)[1] if fits else None


def align(base: np.ndarray, donor: np.ndarray, max_shift: int = MAX_SHIFT, edge: int = EDGE) -> Alignment:
    """Find the translation between two captures, and refuse unless it is in range, trusted and unique.

    1. Three FFT correlations of the edge maps give the edge mismatch of every shift up to twice
       max_shift, so no offset is skipped. Its lowest local minima are the candidates.
    2. Each candidate is scored by its share of CLEAN tiles (see _tiles), counting edges outside its
       overlap as unclean, and the highest wins.
    3. A winner beyond max_shift is refused: the true offset may lie further out.
    4. The winner must pass the tile rule (MIN_AGREE tiles spanning MIN_SPREAD).
    5. Another candidate more than 4 px away whose clean share on its own overlap comes within
       AMBIGUOUS of the winner's means the content repeats.
    6. A BASE that repeats, a result where much of the screen still differs, or a column of evenly
       spaced small changes gets a second offset as Alignment.rival: a hint, not a verdict."""
    e1, e2 = _edges(base, edge), _edges(donor, edge)
    f = max(1, int(np.ceil(np.sqrt(max(e1.size, e2.size) / CORRELATE_PIXELS))))
    s1, s2 = _shrink(e1.astype(np.float32), f), _shrink(e2.astype(np.float32), f)
    probe = min(2 * max_shift, SHIFT_CEILING * 2)
    reach = max(1, probe // f)
    mismatch, area = _mismatch_map(s1, s2, reach)
    lags = np.arange(-reach, reach + 1)
    tile = _tile_size(e1, e2)
    # A shift whose overlap is thinner than one tile cannot be scored, and on ruled lines its
    # mismatch is 0, so it would crowd every real candidate out of the minima.
    t = max(1, tile // f)
    tall = (np.minimum(s1.shape[0], s2.shape[0] - lags) - np.maximum(0, -lags)) >= t
    wide = (np.minimum(s1.shape[1], s2.shape[1] - lags) - np.maximum(0, -lags)) >= t
    usable = (area > 0) & tall[:, None] & wide[None, :]
    total = (int(e1.sum()) + int(e2.sum())) / 2
    scored: dict[tuple[int, int], tuple] = {}
    for row, col in _minima(mismatch, usable, CANDIDATES):
        cy, cx = int(lags[row]) * f, int(lags[col]) * f
        for dx, dy in _near(e1, e2, cx, cy, max(f - 1, 1), tile):
            if (dx, dy) in scored or max(abs(dx), abs(dy)) > probe:
                continue
            o = _overlap(e1, e2, dx, dy, 1)
            if o is None:
                continue
            got = _tiles(e1, e2, dx, dy, tile)
            if got is not None and got[2]:
                # Edges outside the overlap count as tiles that did not match, so an offset
                # cannot win by pushing a unique heading out of the overlap.
                inside = (int(o[1].sum()) + int(o[2].sum())) / 2
                lost = (total - inside) / max(inside / got[2], 1.0)
                # Clean share first; the plain mismatch breaks a tie between neighbours.
                scored[(dx, dy)] = (got[1] / (got[2] + lost), -_mismatch(e1, e2, dx, dy), got, got[1] / got[2])
    if not scored:
        raise Refused(
            "the captures share too little text or icons to align them"
            " (crop both to a part with text in it, or check that they show the same screen)"
        )
    ranked = sorted(scored.items(), key=lambda kv: kv[1][:2], reverse=True)
    (dx, dy), (_, _, (agree, clean, informative, spread), raw) = ranked[0]
    if max(abs(dx), abs(dy)) > max_shift:
        raise Refused(
            f"the best offset, ({dx}, {dy}), lies beyond --max-shift {max_shift}. If the window moved that"
            f" far, rerun with --max-shift {min(SHIFT_CEILING, max(abs(dx), abs(dy)) + 16)}; if not, the"
            " captures show different content, or content that repeats"
        )
    if informative < MIN_AGREE:
        raise Refused(
            "the captures share too little text or icons to align them"
            " (crop both to a part with text in it, or check that they show the same screen)"
        )
    if agree < MIN_AGREE or spread < MIN_SPREAD:
        raise Refused(
            f"no offset within {max_shift} px lines the captures up: at the best one, ({dx}, {dy}),"
            f" {agree} of {informative} detailed tiles agree, spanning {spread:.0%} of the image."
            " The captures are at different scales, or show different screens: take both on one"
            " display, with nothing else changed between them"
        )
    # Ambiguity is judged on each offset's own overlap, without the penalty for lost edges: on
    # repeating content every alias matches its own overlap, and the penalty alone would pick
    # whichever alias lies nearest to zero.
    rivals = [o for o, v in ranked[1:] if max(abs(o[0] - dx), abs(o[1] - dy)) > 4 and v[3] > raw - AMBIGUOUS]
    if rivals:
        raise Refused(
            f"offsets ({dx}, {dy}) and {rivals[0]} fit almost equally well: the content repeats, or an"
            " overlay covers most of the capture. Rerun with --anchor X0,Y0,X1,Y1 on a part that appears"
            " once, such as a heading, a line-number gutter or a column of ids, or capture more of the"
            " screen around the overlay"
        )
    changed, valid = _changed(base, donor, dx, dy, edge)
    share = float(changed.sum() / max(1, valid.sum()))
    # BASE that repeats within the search range has an alias of every offset: on repeating rows
    # with an overlay on a different row in each capture, the alias that lays one overlay onto
    # the other can win, and edge statistics cannot always tell. Measured: repeating lists, tables,
    # grids and editors 0.55 and more, varied screens and chat with real text 0.47 and less.
    doubt = _best_alias(e1, e2, dx, dy, [st for r, st in repeat_steps(e1, probe, tile) if r >= REPEAT], max_shift, tile)
    if doubt is None and share >= BIG_DIFF:
        # A big overlay that moved, with more detail than the screen behind it, can decide the
        # offset on its own. Then most of the screen still differs, and the screen behind it
        # aligns at a second offset.
        doubt = second_motion(e1, e2, dx, dy, max_shift, tile)
    if doubt is None:
        # A row alias leaves a column of small changes, one per row, on the digits that differ
        # from row to row: their spacing is the row step.
        doubt = _best_alias(e1, e2, dx, dy, _box_period(_label(changed), int(valid.sum())), max_shift, tile)
    return Alignment(dx, dy, round(1 - agree / informative, 4), doubt, round(share, 4))


def donor_in_base(base: np.ndarray, donor: np.ndarray, al: Alignment):
    """The donor resampled into base coordinates, and where it is defined."""
    out = np.zeros_like(base)
    valid = np.zeros(base.shape[:2], dtype=bool)
    (y0, y1, x0, x1), _, d = _overlap(base, donor, al.dx, al.dy)
    out[y0:y1, x0:x1] = d
    valid[y0:y1, x0:x1] = True
    return out, valid


def _diff(base: np.ndarray, warped: np.ndarray, valid: np.ndarray, edge: int) -> np.ndarray:
    return (np.abs(_visible(base) - _visible(warped)).max(axis=2) > edge) & valid


def _grow(mask: np.ndarray, size: int) -> np.ndarray:
    return np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(size))) > 0


def _label(mask: np.ndarray) -> np.ndarray:
    """4-connected component labels by row runs and union-find; 0 is background."""
    parent: list[int] = [0]

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    runs: list[tuple[int, int, int, int]] = []
    prev: list[tuple[int, int, int]] = []
    for y in range(mask.shape[0]):
        row = mask[y]
        if not row.any():
            prev = []
            continue
        edges = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        cur = []
        j = 0
        for s, e in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
            rid = len(parent)
            parent.append(rid)
            while j < len(prev) and prev[j][1] <= s:
                j += 1
            k = j
            while k < len(prev) and prev[k][0] < e:
                a, b = find(rid), find(prev[k][2])
                parent[max(a, b)] = min(a, b)
                k += 1
            cur.append((int(s), int(e), rid))
            runs.append((y, int(s), int(e), rid))
        prev = cur
    labels = np.zeros(mask.shape, dtype=np.int32)
    for y, s, e, rid in runs:
        labels[y, s:e] = find(rid)
    return labels


def _boxes(labels: np.ndarray) -> list[tuple[int, int, int, int]]:
    """One (x0, y0, x1, y1) box per label, end-exclusive, in a single pass over the pixels."""
    ys, xs = np.nonzero(labels)
    if len(ys) == 0:
        return []
    ids, inverse = np.unique(labels[ys, xs], return_inverse=True)
    if len(ids) > MAX_COMPONENTS:
        raise Refused(
            f"{len(ids)} separate differences; the captures differ everywhere (a lossy, rescaled or"
            " scrolled pair). For a lossy pair, try a higher --edge"
        )
    x0 = np.full(len(ids), labels.shape[1])
    y0 = np.full(len(ids), labels.shape[0])
    x1 = np.zeros(len(ids), dtype=np.int64)
    y1 = np.zeros(len(ids), dtype=np.int64)
    np.minimum.at(x0, inverse, xs)
    np.minimum.at(y0, inverse, ys)
    np.maximum.at(x1, inverse, xs + 1)
    np.maximum.at(y1, inverse, ys + 1)
    return [(int(a), int(b), int(c), int(d)) for a, b, c, d in zip(x0, y0, x1, y1)]


def merge_boxes(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Join every pair of boxes that overlap, until none do.

    A tooltip's text sits more than GROW pixels inside its outline, so the text and the outline
    become two components whose boxes nest. Joining them gives one number per overlay.
    """
    out = list(boxes)
    changed = True
    while changed:
        changed = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                a, b = out[i], out[j]
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    out[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    del out[j]
                    changed = True
                    break
            if changed:
                break
    return out


def components(base: np.ndarray, donor: np.ndarray, al: Alignment, edge: int = EDGE) -> list[Box]:
    """Every region where the aligned captures differ, numbered in reading order."""
    warped, valid = donor_in_base(base, donor, al)
    raw = _diff(base, warped, valid, edge)
    labels = _label(_grow(raw, 2 * GROW + 1))
    found = [b for b in _boxes(labels) if int(raw[b[1] : b[3], b[0] : b[2]].sum()) >= MIN_PIXELS]
    found = sorted(merge_boxes(found), key=lambda b: (b[1], b[0]))
    return [
        Box(i + 1, *b, int(raw[b[1] : b[3], b[0] : b[2]].sum()))
        for i, b in enumerate(found)
    ]


def check_box(box, shape) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    h, w = shape[:2]
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        raise Unreadable(f"region {list(box)} is not inside the {w}x{h} base image with x0 < x1, y0 < y1")
    return x0, y0, x1, y1


def overlay_mask(base, warped, valid, box, rect: bool, edge: int = EDGE):
    """The overlay inside a box: everything its outline encloses, found by flooding from outside.

    Returns the mask in base coordinates and its solidity (see MIN_SOLID). Low means thin strokes only,
    such as text with no outline around it.
    """
    h, w = base.shape[:2]
    x0, y0, x1, y1 = check_box(box, base.shape)
    mask = np.zeros((h, w), dtype=bool)
    if rect:
        if not valid[y0:y1, x0:x1].all():
            raise Refused(f"box {list(box)} reaches past the part the DONOR covers; shrink it")
        mask[y0:y1, x0:x1] = True
        return mask, 1.0
    gx0, gy0 = max(0, x0 - GROW), max(0, y0 - GROW)
    gx1, gy1 = min(w, x1 + GROW), min(h, y1 + GROW)
    edge_px = _diff(base, warped, valid, edge)[gy0:gy1, gx0:gx1]
    # One pixel of growth seals the gaps antialiasing leaves in a rounded outline.
    sealed = _grow(edge_px, 3)
    open_labels = _label(~sealed)
    rim = np.unique(np.concatenate((open_labels[0], open_labels[-1], open_labels[:, 0], open_labels[:, -1])))
    mask[gy0:gy1, gx0:gx1] = ~np.isin(open_labels, rim[rim > 0])
    # The flood needs a margin around the box, but the paste never leaves the box it was given.
    inside = np.zeros_like(mask)
    inside[y0:y1, x0:x1] = True
    mask &= valid & inside
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return mask, 0.0
    by0, by1, bx0, bx1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    area = (by1 - by0) * (bx1 - bx0)
    differs = np.zeros_like(mask)
    differs[gy0:gy1, gx0:gx1] = edge_px
    enclosed = np.zeros_like(mask)
    enclosed[gy0:gy1, gx0:gx1] = ~sealed
    win = (slice(by0, by1), slice(bx0, bx1))
    shares = ((mask & differs)[win].sum() / area, (mask & enclosed)[win].sum() / area)
    return mask, round(float(sum(shares)), 3)


def merge(base, donor, al: Alignment, boxes, rect: bool, edge: int = EDGE):
    warped, valid = donor_in_base(base, donor, al)
    out = base.copy()
    report = []
    boxes = list(dict.fromkeys(boxes))
    for box in boxes:
        mask, solid = overlay_mask(base, warped, valid, box, rect, edge)
        if not mask.any():
            raise Refused(f"box {list(box)} holds no difference between the captures; nothing to paste")
        out[mask] = warped[mask]
        report.append({"box": list(box), "pixels": int(mask.sum()), "solid": solid})
    return out, report


def proof_sheet(base, donor, al: Alignment, merged, boxes) -> np.ndarray:
    """Per pasted box, at full size and the full image width: the band around the overlay in the
    DONOR as captured, in BASE as captured, and in the result. Row labels sit at the same x in all
    three. The BASE band is untouched, so the item under the paste can be read there even when the
    paste carried the DONOR's own label along."""
    head, gap = 28, 16
    bands = []
    for x0, y0, x1, y1 in boxes:
        m = min(max(60, y1 - y0), 400)
        bands.append((max(0, y0 - m), min(base.shape[0], y1 + m)))
    height = sum(3 * (ry1 - ry0 + head) + gap for ry0, ry1 in bands)
    if height * base.shape[1] > MAX_PIXELS:
        raise Refused(
            "the proof would be larger than the input limit; merge fewer boxes at a time"
            if len(boxes) > 1
            else "the proof would be larger than the input limit; crop both captures around the overlay"
        )
    parts = []
    for ry0, ry1 in bands:
        before = np.zeros((ry1 - ry0, base.shape[1], 4), dtype=np.int16)
        before[...] = (255, 0, 255, 255)  # outside the DONOR
        sy0, sy1 = max(0, ry0 + al.dy), min(donor.shape[0], ry1 + al.dy)
        sx0, sx1 = max(0, al.dx), min(donor.shape[1], base.shape[1] + al.dx)
        if sy1 > sy0 and sx1 > sx0:
            before[sy0 - ry0 - al.dy : sy1 - ry0 - al.dy, sx0 - al.dx : sx1 - al.dx] = donor[sy0:sy1, sx0:sx1]
        parts.append((before, base[ry0:ry1], merged[ry0:ry1]))
    sheet = Image.new("RGB", (base.shape[1], height), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=18)
    y = 0
    for box, (a, b, c) in zip(boxes, parts):
        for img, label in ((a, f"box {list(box)}: DONOR as captured"), (b, "BASE as captured"), (c, "result")):
            draw.text((4, y + 4), label, fill=(0, 0, 0), font=font)
            sheet.paste(Image.fromarray(_on_board(img)), (0, y + head))
            y += img.shape[0] + head
        y += gap
    return np.asarray(sheet.convert("RGBA"), dtype=np.int16)


def save_verified(arr: np.ndarray, path: Path, icc: bytes | None = None, comment: str | None = None) -> None:
    """Write a PNG next to its target, read it back, and only then move it into place."""
    alpha = bool((arr[..., 3] < 255).any()) if arr.shape[2] == 4 else False
    img = arr.astype(np.uint8) if alpha else arr[..., :3].astype(np.uint8)
    info = PngImagePlugin.PngInfo()
    if comment:
        info.add_text("Comment", comment)
    fd, tmp = tempfile.mkstemp(suffix=".png", dir=path.parent if str(path.parent) else ".")
    os.close(fd)
    try:
        Image.fromarray(img).save(tmp, format="PNG", pnginfo=info, icc_profile=icc)
        with Image.open(tmp, formats=["PNG"]) as im:
            back = np.asarray(im.convert("RGBA" if alpha else "RGB"))
        if not np.array_equal(back, img):
            raise Refused(f"{path} would not read back as the image that was written")
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _on_board(img: np.ndarray) -> np.ndarray:
    """RGBA over a checkerboard, so a difference only in transparency is visible too."""
    yy, xx = np.mgrid[0 : img.shape[0], 0 : img.shape[1]]
    board = np.where(((yy // 12 + xx // 12) % 2)[..., None] == 0, 200, 150).astype(np.int32)
    a = img[..., 3:4].astype(np.int32)
    return ((img[..., :3].astype(np.int32) * a + board * (255 - a)) // 255).clip(0, 255).astype(np.uint8)


def contact_sheet(base, donor, al: Alignment, boxes: list[Box]) -> np.ndarray:
    """Base on the left, donor moved into base coordinates on the right, boxes numbered on both."""
    warped, _ = donor_in_base(base, donor, al)
    h, w = base.shape[:2]
    sheet = Image.new("RGB", (2 * w + 16, h), (255, 0, 255))
    for img, off in ((base, 0), (warped, w + 16)):
        sheet.paste(Image.fromarray(_on_board(img)), (off, 0))
    # Scaled on its long edge to what a model sees, and the numbers drawn after the scaling, so they
    # stay legible. The JSON keeps the coordinates in BASE pixels.
    k = min(1.0, SHEET_LONG / max(sheet.width, sheet.height))
    if k < 1.0:
        sheet = sheet.resize((round(sheet.width * k), round(sheet.height * k)), Image.LANCZOS)
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=max(18, max(sheet.size) // 60))
    line = max(3, sheet.width // 500)
    for b in boxes:
        for off in (0, w + 16):
            box = [round(v * k) for v in (b.x0 + off, b.y0, b.x1 + off - 1, b.y1 - 1)]
            draw.rectangle(box, outline=(255, 40, 40), width=line)
            draw.text((box[0] + 4, box[1] + 2), str(b.n), fill=(255, 40, 40), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    return np.asarray(sheet.convert("RGBA"), dtype=np.int16)


# ---------------------------------------------------------------- CLI


def _ints(text: str, n: int | None = None) -> list[int]:
    try:
        values = [int(v) for v in text.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected comma-separated integers, got {text!r}") from exc
    if n is not None and len(values) != n:
        raise argparse.ArgumentTypeError(f"expected {n} integers, got {len(values)}")
    return values


def _bounded(lo: int, hi: int):
    def parse(text: str) -> int:
        value = int(text)
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"expected {lo} to {hi}, got {value}")
        return value

    return parse


def _png_path(text: str) -> Path:
    if not text.lower().endswith(".png"):
        raise argparse.ArgumentTypeError(f"output must be a .png file, got {text!r}")
    return Path(text)


def _join_offset(argv: list[str]) -> list[str]:
    """`--offset -5,-9` as `--offset=-5,-9`: before Python 3.14, argparse reads -5,-9 as an option."""
    out, rest = [], iter(argv)
    for a in rest:
        b = next(rest, None) if a == "--offset" else None
        out.append(a if b is None else f"--offset={b}")
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    argv = _join_offset(argv)
    p = argparse.ArgumentParser(prog="overlays.py", description=__doc__.splitlines()[0])
    p.add_argument("--selftest", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("selftest", help="run the offline selftest")
    for name in ("align", "list", "merge"):
        s = sub.add_parser(name)
        s.add_argument("base", type=Path)
        s.add_argument("donor", type=Path)
        s.add_argument("--max-shift", type=_bounded(0, SHIFT_CEILING), default=MAX_SHIFT)
        s.add_argument("--edge", type=_bounded(4, 128), default=EDGE)
        how = s.add_mutually_exclusive_group()
        how.add_argument("--anchor", type=lambda t: _ints(t, 4), help="X0,Y0,X1,Y1: align on this part of BASE only")
        if name != "align":
            how.add_argument(
                "--offset",
                type=lambda t: _ints(t, 2),
                help="DX,DY to use instead of searching, held to the same trust rule; give the same to list and merge",
            )
    sub.choices["list"].add_argument("--sheet", type=_png_path, required=True)
    m = sub.choices["merge"]
    which = m.add_mutually_exclusive_group(required=True)
    which.add_argument("--pick", type=_ints, help="component numbers from `list`")
    which.add_argument("--region", type=lambda t: _ints(t, 4), help="X0,Y0,X1,Y1 in base pixels")
    m.add_argument("--rect", action="store_true", help="paste the whole box, for an overlay with no outline")
    m.add_argument("--out", type=_png_path, required=True)
    m.add_argument("--proof", type=_png_path, help="also write the band of each pasted box: DONOR and BASE as captured, then the result")
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if args.selftest or args.cmd == "selftest":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from selftest import selftest

        return selftest()
    if not args.cmd:
        p.print_usage(sys.stderr)
        return 2

    try:
        if getattr(args, "proof", None) and (
            args.proof.resolve() == args.out.resolve()
            or (args.proof.exists() and args.out.exists() and os.path.samefile(args.proof, args.out))
        ):
            raise Unreadable("--proof and --out name the same file; give the proof its own name")
        for target in (getattr(args, "out", None), getattr(args, "sheet", None), getattr(args, "proof", None)):
            for src in (args.base, args.donor) if target is not None else ():
                if src.exists() and target.exists() and os.path.samefile(src, target):
                    raise Unreadable(f"{target} is an input; write the result to a new file")
        b, d = load(args.base), load(args.donor)
        for c, path in ((b, args.base), (d, args.donor)):
            if c.lossy:
                print(f"overlays: {path} is a JPEG; compression noise can split overlays or add components", file=sys.stderr)
        if b.icc != d.icc:
            print("overlays: the captures carry different color profiles; colors may not match", file=sys.stderr)
        base, donor = b.rgba, d.rgba
        offset = getattr(args, "offset", None)
        if offset:
            al = at_offset(base, donor, *offset, edge=args.edge)
        elif args.anchor:
            al = at_anchor(base, donor, args.anchor, args.max_shift, args.edge)
        else:
            al = align(base, donor, args.max_shift, args.edge)
        if al.rival is not None:
            print(
                f"overlays: offset {list(al.rival)} may be the true one instead of ({al.dx}, {al.dy}):"
                " the screen repeats, or a big overlay moved between the captures. If the pasted overlay"
                " does not point at the item hovered in the DONOR, rerun list and merge with"
                f" --offset={al.rival[0]},{al.rival[1]} or with --anchor on a part that appears once",
                file=sys.stderr,
            )
        if al.changed >= BIG_DIFF:
            print(
                f"overlays: {al.changed:.0%} of the screen differs after alignment, more than overlays"
                " usually cover. A big overlay may have decided the offset, or a fine texture may hide"
                " an offset that is 1 px off: compare a crop of the sheet at full size",
                file=sys.stderr,
            )
        if al.residual > WARN_RESIDUAL:
            print(
                f"overlays: residual {al.residual:.2f}; much of the screen differs. Check the contact"
                " sheet: this may be another screen state rather than two overlays",
                file=sys.stderr,
            )
        if args.cmd == "align":
            print(json.dumps(asdict(al)))
        elif args.cmd == "list":
            boxes = components(base, donor, al, args.edge)
            save_verified(contact_sheet(base, donor, al, boxes), args.sheet, b.icc if b.icc == d.icc else None)
            print(json.dumps({**asdict(al), "sheet": str(args.sheet), "components": [asdict(x) for x in boxes]}))
        else:
            if args.region:
                boxes = [check_box(tuple(args.region), base.shape)]
            else:
                found = {x.n: x for x in components(base, donor, al, args.edge)}
                picks = list(dict.fromkeys(args.pick))
                missing = [n for n in picks if n not in found]
                if missing:
                    raise Refused(f"no component numbered {missing}; run `list` with the same options")
                boxes = [(found[n].x0, found[n].y0, found[n].x1, found[n].y1) for n in picks]
            out, report = merge(base, donor, al, boxes, args.rect, args.edge)
            rival = list(al.rival) if al.rival else None
            how = "given with --offset" if offset else f"anchored on {args.anchor}" if args.anchor else f"searched, rival {rival}"
            note = (
                f"Composite of two captures (screenshot-overlays); pasted boxes {[r['box'] for r in report]};"
                f" offset ({al.dx}, {al.dy}) {how}, residual {al.residual}"
            )
            # The proof goes first, so a failed proof leaves no result behind.
            if args.proof:
                save_verified(proof_sheet(base, donor, al, out, [tuple(r["box"]) for r in report]), args.proof)
                if args.out.exists() and os.path.samefile(args.proof, args.out):  # a case-insensitive disk
                    args.proof.unlink()
                    raise Unreadable("--proof and --out name the same file; give the proof its own name")
            save_verified(out, args.out, b.icc, note)
            proof = str(args.proof) if args.proof else None
            print(json.dumps({**asdict(al), "out": str(args.out), "pasted": report, "proof": proof}))
            for r in report:
                if r["solid"] < MIN_SOLID:
                    print(
                        f"overlays: box {r['box']} pasted thin strokes only (no closed outline). Look at"
                        " the result, and rerun with --rect if the overlay looks hollow.",
                        file=sys.stderr,
                    )
    except Refused as exc:
        print(f"overlays: refused: {exc}", file=sys.stderr)
        return 1
    except (Unreadable, OSError) as exc:
        print(f"overlays: {exc}", file=sys.stderr)
        return 2
    except MemoryError:
        print("overlays: the captures do not fit in memory; crop them smaller", file=sys.stderr)
        return 2
    except Exception:  # a crash must never read as a refusal (exit 1)
        import traceback

        traceback.print_exc()
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
