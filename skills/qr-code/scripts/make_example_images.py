#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file of this skill.
"""Draw the illustrations for this skill, with no input files and no real data.

Purpose: write qr-code-epc-example.png (the code `qr.py epc` writes for the example in the EPC
    guideline, EPC069-12 v3.1 section 2.3, next to its elements) and qr-code-card-example.png (a
    menu card `qr.py text` writes, with a logo this script draws and an example.com address).
Usage:
    .venv-qr/bin/python scripts/make_example_images.py                 # into the current folder
    .venv-qr/bin/python scripts/make_example_images.py --out-dir DIR   # somewhere else

Needs the same libraries as qr.py. Both pictures are decoded again after they are saved.
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True  # no __pycache__ inside the skill folder
import qr  # noqa: E402  (the module beside this script, so the pictures use the real code path)

SAMPLE = dict(
    name="Franz Mustermänn",
    iban="DE89370400440532013000",
    amount="12.3",
    reference="RF18539007547034",
    bic="BHBLDEHHXXX",
    purpose="GDDS",
)
CARD_URL = "https://example.com/menu"
CARD_CAPTION = "Scan for today's menu"
CARD_COLOR = "#1F7A8C"
INK, PAPER, MUTED, OK = (24, 24, 28), (255, 255, 255), (110, 110, 120), (22, 120, 60)


def epc_example(out: Path) -> None:
    payload = qr.build_payload(**SAMPLE)
    with tempfile.TemporaryDirectory() as tmp:
        code_path = Path(tmp) / "code.png"
        verified = qr.make(payload, code_path)
        code = Image.open(code_path).convert("RGB")  # pasted 1:1, so every module stays 10 px

    card = Image.new("RGB", (560 + 640, max(540, code.height + 60)), PAPER)
    card.paste(code, (30, 30))
    draw = ImageDraw.Draw(card)
    title, body, small = (qr.find_font(size) for size in (34, 24, 18))
    left = code.width + 70
    draw.text((left, 44), "EPC QR code, proven from the file", font=title, fill=INK)
    y = 110
    for number, (label, value) in enumerate(zip(qr.FIELDS, payload.split("\n")), 1):
        if number < 5:
            continue  # the four header elements are the same in every code
        draw.text((left, y), f"{label}", font=body, fill=MUTED)
        draw.text((left + 200, y), value, font=body, fill=INK)
        y += 40
    draw.text((left, y + 20), "VERIFIED  ...  " + verified.rsplit(" ", 1)[-1], font=body, fill=OK)
    draw.text(
        (left, card.height - 40),
        "Adapted from EPC069-12 v3.1, section 2.3: version 002, placeholder IBAN",
        font=small,
        fill=MUTED,
    )
    card.save(out, optimize=True)
    if qr.decode(out)[0] != payload.encode("utf-8"):
        out.unlink()
        raise SystemExit(f"{out}: the saved picture does not decode to the payload")


def invented_logo(path: Path) -> None:
    """A five-pointed star on a disc: a logo nobody owns, drawn here."""
    size = 400
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, size - 8, size - 8), fill=(214, 69, 65, 255), outline=INK, width=14)
    center, outer, inner = size / 2, size * 0.36, size * 0.15
    points = [
        (
            center + (outer if i % 2 == 0 else inner) * math.sin(math.pi * i / 5),
            center - (outer if i % 2 == 0 else inner) * math.cos(math.pi * i / 5),
        )
        for i in range(10)
    ]
    draw.polygon(points, fill=PAPER, outline=INK, width=8)
    image.save(path)


def card_example(out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        logo = Path(tmp) / "logo.png"
        invented_logo(logo)
        print(qr.make_text(CARD_URL, out, logo=logo, caption=CARD_CAPTION, color=CARD_COLOR))


def cutout_example(out: Path) -> None:
    """A card whose logo was pasted straight onto the code, and the logo lifted back out of it."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        code = work / "code.png"
        qr.make_text(CARD_URL, code)
        with Image.open(code) as base:
            welded = base.convert("RGBA")
        patch = Image.new("RGBA", welded.size, (0, 0, 0, 0))
        span, mid = 2.5 * qr.CARD_PPM, welded.width / 2
        ImageDraw.Draw(patch).ellipse(
            (mid - span, mid - span, mid + span, mid + span),
            fill=(214, 69, 65, 255),
            outline=INK,
            width=8,
        )
        pasted = work / "pasted.png"
        Image.alpha_composite(welded, patch).convert("RGB").save(pasted)
        lifted = work / "lifted.png"
        qr.cutout(pasted, lifted, None, qr.CUTOUT_MODULES)
        with Image.open(pasted) as a, Image.open(lifted) as b:
            source, mark = a.convert("RGB").resize((420, 420), Image.LANCZOS), b.convert("RGBA")

    sheet = Image.new("RGB", (1000, 540), PAPER)
    sheet.paste(source, (40, 60))
    # A checker behind the lifted mark, so its transparent background reads as transparent.
    board = Image.new("RGB", (420, 420), PAPER)
    for row in range(0, 420, 20):
        for col in range(0, 420, 20):
            if (row + col) // 20 % 2:
                ImageDraw.Draw(board).rectangle(
                    (col, row, col + 19, row + 19), fill=(232, 232, 236)
                )
    fitted = mark.copy()
    fitted.thumbnail((300, 300), Image.LANCZOS)
    board.paste(fitted, ((420 - fitted.width) // 2, (420 - fitted.height) // 2), fitted)
    sheet.paste(board, (540, 60))
    draw = ImageDraw.Draw(sheet)
    label, small = qr.find_font(26), qr.find_font(18)
    draw.text((40, 24), "a card, logo pasted onto the code", font=label, fill=MUTED)
    draw.text((540, 24), "lifted: the mark, and what was welded to it", font=label, fill=MUTED)
    draw.text((482, 250), "->", font=label, fill=INK, anchor="mm")
    draw.text(
        (40, 500),
        "This logo was pasted onto the code, so parting them leaves a module behind. Over a "
        "cleared zone the lift is exact. Nothing here is real: the script draws it all.",
        font=small,
        fill=MUTED,
    )
    sheet.save(out, optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    epc = args.out_dir / "qr-code-epc-example.png"
    epc_example(epc)
    print(epc)
    card_example(args.out_dir / "qr-code-card-example.png")
    lift = args.out_dir / "qr-code-cutout-example.png"
    cutout_example(lift)
    print(lift)
    return 0


if __name__ == "__main__":
    sys.exit(main())
