#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file beside this script.
"""Draw the illustration for this skill, from nothing.

Purpose: regenerate ../../docs/assets/scrolling-display-contact-sheet.png, a contact sheet of
    the kind `read_display.py sheets` produces. Every digit is drawn here from seven-segment
    geometry, so the picture shows what the output looks like without any real device, any
    real reading, or any photograph being shipped.

Usage:
    python3 make_example_images.py            # write the asset
    python3 make_example_images.py --out X    # write it somewhere else

Needs Pillow. The values are invented and deliberately unremarkable.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# The picture lives outside the skill folder, in the repository docs, so the folder somebody
# copies out stays text-only: a bundled binary a security scanner cannot read is reported
# HIGH at fixed confidence, once per reference. parents[3] is the repository root publicly
# and the private skills root privately, and docs/assets exists in both.
_REPO_ASSETS = Path(__file__).resolve().parents[3] / "docs" / "assets"
# ...but only when that directory really is the repository's. Copy the skill folder alone into
# an agent's skills directory, which is the install the README documents, and parents[3] climbs
# out into the home directory, where writing a docs/assets the user never asked for would be a
# tool putting files outside anything it was pointed at. Fall back to the working directory.
ASSETS = _REPO_ASSETS if _REPO_ASSETS.is_dir() else Path.cwd()

CELL = (420, 260)
COLS = 3
PANEL_BG = (24, 26, 24)
BODY_BG = (206, 206, 200)
LIT = (122, 152, 196)
# Barely above the panel, because an unlit segment you can read turns every 1 into an 8.
DIM = (31, 33, 32)

# Which of the seven segments each digit lights, in the order a b c d e f g.
SEGMENTS = {
    "0": "abcdef",
    "1": "bc",
    "2": "abdeg",
    "3": "abcdg",
    "4": "bcfg",
    "5": "acdfg",
    "6": "acdefg",
    "7": "abc",
    "8": "abcdefg",
    "9": "abcdfg",
    "-": "g",
    " ": "",
}

# INVENTED series, and the word has to stay true. These nine pairs were made up for this picture.
# Never paste a real reading in here, however harmless one instrument's numbers look: nine daily
# values against their own index are a household's consumption for nine identifiable days, and the
# illustration is the one file in a skill that gets copied into a README and looked at by strangers.
# The first version of this file failed exactly that test.
SERIES = [
    ("-1", "12.7"),
    ("-2", "8.3"),
    ("-3", "19.4"),
    ("-4", "10.6"),
    ("-5", "13.1"),
    ("-6", "16.8"),
    ("-7", "7.9"),
    ("-8", "11.5"),
    ("-9", "14.2"),
]
# Seconds into a short demo clip, spaced by about half a second, which is what a steady scroll gives.
TIMES = [3.2, 3.7, 4.3, 4.8, 5.4, 5.9, 6.5, 7.0, 7.6]


def draw_digit(draw: ImageDraw.ImageDraw, ch: str, x: int, y: int, w: int, h: int) -> None:
    """Paint one seven-segment glyph, lit segments bright and the rest barely visible."""
    on = SEGMENTS.get(ch, "")
    t = max(2, h // 10)
    bars = {
        "a": (x, y, x + w, y + t),
        "b": (x + w - t, y, x + w, y + h // 2),
        "c": (x + w - t, y + h // 2, x + w, y + h),
        "d": (x, y + h - t, x + w, y + h),
        "e": (x, y + h // 2, x + t, y + h),
        "f": (x, y, x + t, y + h // 2),
        "g": (x, y + h // 2 - t // 2, x + w, y + h // 2 + t // 2),
    }
    for name, box in bars.items():
        draw.rectangle(box, fill=LIT if name in on else DIM)


def draw_number(draw: ImageDraw.ImageDraw, text: str, x: int, y: int, h: int) -> int:
    """Paint a string of glyphs left to right, returning the width used."""
    w = int(h * 0.55)
    gap = max(3, w // 6)
    cursor = x
    for ch in text:
        if ch == ".":
            # The point sits on the baseline between two glyphs, not under the next one.
            r = max(2, h // 14)
            cursor -= gap // 2
            draw.rectangle((cursor, y + h - r * 2, cursor + r * 2, y + h), fill=LIT)
            cursor += r * 2 + gap
            continue
        draw_digit(draw, ch, cursor, y, w, h)
        cursor += w + gap
    return cursor - x


def load_font(size: int):
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def make_tile(index: str, value: str, moment: float, blur_hint: bool) -> Image.Image:
    """One cell: the instrument body, the panel, the two numbers, and the timestamp stamp."""
    w, h = CELL
    tile = Image.new("RGB", (w, h), BODY_BG)
    draw = ImageDraw.Draw(tile)
    panel = (int(w * 0.08), int(h * 0.20), int(w * 0.92), int(h * 0.86))
    draw.rectangle(panel, fill=PANEL_BG)
    draw.rectangle(panel, outline=(70, 72, 70), width=2)

    px, py = panel[0] + 16, panel[1] + 14
    draw_number(draw, "1.8.0", px, py, int(h * 0.11))
    idx_w = draw_number(draw, index, panel[2] - int(w * 0.34), py, int(h * 0.16))
    value_h = int(h * 0.30)
    used = draw_number(draw, value, px, panel[3] - value_h - 12, value_h)
    unit = load_font(max(11, int(h * 0.07)))
    draw.text((px + used + 8, panel[3] - 30), "kWh", font=unit, fill=(120, 132, 150))

    if blur_hint:
        # A dimmer panel stands in for a frame the picker would reject, so the picture shows
        # that not every frame is usable.
        overlay = Image.new("RGB", (panel[2] - panel[0], panel[3] - panel[1]), PANEL_BG)
        tile.paste(Image.blend(tile.crop(panel), overlay, 0.55), (panel[0], panel[1]))

    label = load_font(max(13, int(h * 0.085)))
    draw.rectangle((0, 0, int(w * 0.26), int(h * 0.11)), fill="black")
    draw.text((5, 3), f"{moment:.2f}s", font=label, fill="yellow")
    draw.rectangle((0, 0, w - 1, h - 1), outline=(200, 30, 30), width=3)
    return tile


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ASSETS / "scrolling-display-contact-sheet.png")
    args = ap.parse_args()

    tiles = [
        make_tile(index, value, moment, blur_hint=(i == 4))
        for i, ((index, value), moment) in enumerate(zip(SERIES, TIMES))
    ]
    rows = (len(tiles) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * CELL[0], rows * CELL[1]), "white")
    for i, tile in enumerate(tiles):
        sheet.paste(tile, ((i % COLS) * CELL[0], (i // COLS) * CELL[1]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.out, optimize=True)
    print(f"wrote {args.out} ({sheet.size[0]}x{sheet.size[1]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
