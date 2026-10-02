#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file of this skill.
"""Build a QR code, a SEPA payment code or a printable card, and prove the file on disk.

Purpose: write a PNG that decodes to exactly the data you asked for, and refuse rather than repair
    any input a scanner would read differently. The finished image is decoded back from disk and
    compared byte for byte before it is put in place.
Usage:
    python3 qr.py text --data TEXT [--logo IMG] [--caption TEXT] [--color COLOUR] --out FILE.png
    python3 qr.py epc --name NAME --iban IBAN [--amount 12.30] [--text TEXT | --reference REF]
                      [--bic BIC] [--purpose CODE] [--info TEXT] --out FILE.png
    python3 qr.py epc-verify FILE.png
    python3 qr.py --selftest

`epc` follows EPC069-12 v3.1 (2024): version 002, character set 1 (UTF-8), error correction level
M, QR version 13 or lower (331 bytes), LF between elements, nothing after the last populated element.
`text` uses level M, or level H with a logo; a card is also decoded after downscaling and blurring.
Exit codes: 0 verified, 1 refused, 2 usage, 3 dependency missing, 4 proof failed.
Needs Python 3.10+ and segno==1.6.6, zxing-cpp==3.1.1, pillow==12.3.0.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import math
import os
import re
import stat
import sys
import tempfile
import unicodedata
import warnings
from decimal import Decimal
from pathlib import Path

# EPC409-09 v8.0 (24 December 2025) lists the countries in the SEPA schemes by the prefix used in
# the IBAN; the lengths come from the SWIFT IBAN registry. Version 002 lets an EEA account omit the
# BIC; any other SEPA country still needs it ("V2: O/M"). Territories borrow a prefix: Guernsey,
# Jersey and the Isle of Man use GB, the French overseas territories use FR.
EEA = {
    "AT": 20,
    "BE": 16,
    "BG": 22,
    "CY": 28,
    "CZ": 24,
    "DE": 22,
    "DK": 18,
    "EE": 20,
    "ES": 24,
    "FI": 18,
    "FR": 27,
    "GR": 27,
    "HR": 21,
    "HU": 28,
    "IE": 22,
    "IS": 26,
    "IT": 27,
    "LI": 21,
    "LT": 20,
    "LU": 20,
    "LV": 21,
    "MT": 31,
    "NL": 18,
    "NO": 15,
    "PL": 28,
    "PT": 25,
    "RO": 24,
    "SE": 24,
    "SI": 19,
    "SK": 24,
}
NON_EEA = {
    "AD": 24,
    "AL": 28,
    "CH": 21,
    "GB": 22,
    "GI": 23,
    "MC": 27,
    "MD": 24,
    "ME": 22,
    "MK": 19,
    "RS": 22,
    "SM": 27,
    "VA": 22,
}
IBAN_LENGTH = EEA | NON_EEA

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_DEPENDENCY, EXIT_PROOF = 0, 1, 2, 3, 4
MAX_BYTES = 331  # QR version 13 at level M holds 331 bytes; EPC069-12 caps the code there.
# Text mode. Version 25 is 117 modules: still about 0.7 mm per module on a 9 cm printed code.
TEXT_MAX_VERSION = 25
# At version 1 a centered zone reaches a format-information module, so a logo starts at version 2.
LOGO_MIN_VERSION = 2
# From version 7 the code carries alignment patterns in its middle, where a centered logo would
# cover them. zone_is_clear models versions 1 to 6 only. Version 6 at level H holds 58 bytes.
LOGO_MAX_VERSION = 6
# Level H restores about 30 % of the codewords (Denso Wave). The cleared zone stays far below that,
# because a module lost to the logo and a module lost to print or blur draw on the same budget.
LOGO_AREA = 0.07
CARD_PPM = 20  # pixels per module on a card: a version-6 card is about 1,000 px wide
PROOF_PPM = (3, 2)  # the card must still decode after a box downscale to these pixels per module
# Gaussian blur, as a share of one module. Measured with zxing-cpp 3.1.1 on versions 3 to 6: every
# variant decoded at 0.4, and the first failures came between 0.5 and 0.6, plain codes included. So
# 0.5 tests the decoder rather than the card, and 0.3 keeps a margin below the first failure.
PROOF_BLUR = 0.3
# A camera blurs and shrinks at once, onto a pixel grid that does not line up with the modules. So
# one pass does both: the blur above, then a downscale to a fractional 2.5 px per module. Measured:
# plain, logo and card codes of versions 2 to 5 all decoded down to 1.8 px per module this way.
PROOF_COMBINED_PPM = 2.5
# Pillow picks a decoder from the file header, not the name, and some decoders start external
# programs (EPS starts Ghostscript). So every image this script reads is limited to these formats.
LOGO_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP")
LOGO_FORMAT_LIST = ", ".join(LOGO_FORMATS)
READ_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP")
MAX_PIXELS = 25_000_000  # a logo or a received code larger than this is refused before decoding
BUG_HINT = (
    "Your input was accepted, so this points to a library version other than the pinned one "
    "or to a bug in this script."
)
# Letters that look alike across these scripts (Latin a, Cyrillic а) are refused inside one word.
LOOKALIKE_SCRIPTS = {"LATIN", "GREEK", "CYRILLIC"}
# The first system font found here wins: macOS, Debian and Ubuntu, Fedora, Arch, then Windows.
FONT_CANDIDATES = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "arial.ttf"),
)
FIELDS = (
    "service tag",
    "version",
    "character set",
    "identification",
    "BIC",
    "name",
    "IBAN",
    "amount",
    "purpose",
    "reference",
    "text",
    "information",
)
# Control, format, surrogate, private-use, unassigned, line and paragraph separators. Each one
# either splits a line in some parser or survives as an invisible difference in the payee name.
FORBIDDEN = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
CHARSETS = {
    "1": "utf-8",
    "2": "iso-8859-1",
    "3": "iso-8859-2",
    "4": "iso-8859-4",
    "5": "iso-8859-5",
    "6": "iso-8859-7",
    "7": "iso-8859-10",
    "8": "iso-8859-15",
}


class Refused(ValueError):
    """A field the guideline or a bank would reject or read differently."""


class ProofFailed(RuntimeError):
    """The PNG does not decode to the payload it was built from."""


class DependencyMissing(RuntimeError):
    """A library is not installed."""


def script(ch: str) -> str:
    """The first word of the Unicode name of a letter: LATIN, GREEK, CYRILLIC, CJK and so on."""
    return unicodedata.name(ch, "UNKNOWN").split(" ", 1)[0]


def printable(text: str) -> str:
    """Escape control and format characters, so untrusted text cannot drive the terminal."""
    return "".join(
        ch.encode("unicode_escape").decode("ascii")
        if unicodedata.category(ch) in FORBIDDEN or "\x80" <= ch <= "\x9f"
        else ch
        for ch in text
    )


def mod97(value: str) -> int:
    """ISO 7064 mod 97-10 over letters as 10..35, the check behind IBANs and RF references."""
    return int("".join(str(int(ch, 36)) for ch in value)) % 97


def clean_text(value: str, field: str, limit: int) -> str:
    """Normalise to NFC, then refuse anything a bank could turn into a different string."""
    text = unicodedata.normalize("NFC", value)
    if not text:
        raise Refused(f"{field}: empty")
    if unicodedata.normalize("NFKC", text) != text:
        raise Refused(
            f"{field}: holds a compatibility character (a ligature, a full-width or "
            "superscript letter, a non-breaking space). Retype it with plain letters."
        )
    for ch in text:
        category = unicodedata.category(ch)
        if category in FORBIDDEN or (category == "Zs" and ch != " "):
            raise Refused(
                f"{field}: holds U+{ord(ch):04X} {unicodedata.name(ch, '')}".rstrip()
                + ", which is not printable text"
            )
    if text != text.strip(" ") or "  " in text:
        raise Refused(f"{field}: leading, trailing or double spaces")
    for word in re.split(r"\W+", text):
        if len({script(ch) for ch in word if ch.isalpha()} & LOOKALIKE_SCRIPTS) > 1:
            raise Refused(
                f"{field}: the word {printable(word)!r} mixes Latin, Greek or Cyrillic letters, "
                "which can look alike. Retype it in one script."
            )
    if len(text) > limit:
        hint = (
            (
                " Shorten it yourself to the account holder's name as the bank holds it, and read "
                "the bank's payee check before you send."
            )
            if field == "name"
            else ""
        )
        raise Refused(f"{field}: {len(text)} characters, the limit is {limit}.{hint}")
    return text


def clean_iban(value: str) -> tuple[str, str]:
    """Drop the print grouping, then check country, length and the mod-97 check digits."""
    iban = value.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]+", iban):  # ASCII digits only, never \d
        raise Refused("IBAN: a country code, two check digits, then letters or digits")
    country = iban[:2]
    if country not in IBAN_LENGTH:
        raise Refused(f"IBAN: {country} is not a SEPA scheme country (EPC409-09 v8.0)")
    if len(iban) != IBAN_LENGTH[country]:
        raise Refused(f"IBAN: {len(iban)} characters, a {country} IBAN has {IBAN_LENGTH[country]}")
    if mod97(iban[4:] + iban[:4]) != 1:
        raise Refused("IBAN: the check digits do not match (mod-97), so a character is wrong")
    return iban, country


def clean_bic(value: str | None, country: str) -> str:
    if not value:
        if country not in EEA:
            raise Refused(
                f"BIC: required for a {country} IBAN, because {country} is outside the EEA"
            )
        return ""
    bic = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?", bic):
        raise Refused("BIC: 8 or 11 characters (bank, country, location, optional branch)")
    return bic


def clean_amount(value: str | None) -> str:
    """Two decimals always. A regex first, because Decimal also accepts 1E+3 and NaN."""
    if value is None or value == "":
        return ""
    match = re.fullmatch(r"([0-9]{1,9})(?:[.,]([0-9]{1,2}))?", value)
    if not match:
        raise Refused("amount: digits with at most two decimals, for example 42.00")
    amount = Decimal(f"{match.group(1)}.{match.group(2) or '0'}")
    if not Decimal("0.01") <= amount <= Decimal("999999999.99"):
        raise Refused("amount: from 0.01 to 999999999.99")
    return f"EUR{amount:.2f}"


def clean_reference(value: str) -> str:
    """Up to 35 characters. Only an ISO 11649 creditor reference (RF...) carries a checksum."""
    reference = clean_text(value, "reference", 35)
    if reference.startswith("RF"):
        if not re.fullmatch(r"RF[0-9]{2}[A-Za-z0-9]{1,21}", reference):
            raise Refused(
                "reference: an RF reference is RF, two check digits, 1 to 21 letters or digits"
            )
        if mod97(reference[4:].upper() + reference[:4]) != 1:
            raise Refused("reference: the RF check digits do not match (ISO 11649 mod-97)")
    return reference


def clean_purpose(value: str) -> str:
    if not re.fullmatch(r"[A-Z0-9]{1,4}", value):
        raise Refused(
            "purpose: 1 to 4 capital letters or digits, an ISO 20022 purpose code such as GDDS"
        )
    return value


def build_payload(
    *,
    name: str,
    iban: str,
    amount: str | None = None,
    text: str | None = None,
    reference: str | None = None,
    bic: str | None = None,
    purpose: str | None = None,
    info: str | None = None,
) -> str:
    if text and reference:
        raise Refused("give a text or a reference, not both (EPC069-12)")
    iban, country = clean_iban(iban)
    fields = [
        "BCD",
        "002",
        "1",
        "SCT",
        clean_bic(bic, country),
        clean_text(name, "name", 70),
        iban,
        clean_amount(amount),
        clean_purpose(purpose) if purpose else "",
        clean_reference(reference) if reference else "",
        clean_text(text, "text", 140) if text else "",
        clean_text(info, "information", 70) if info else "",
    ]
    while fields[-1] == "":
        fields.pop()
    payload = "\n".join(fields)
    size = len(payload.encode("utf-8"))
    if size > MAX_BYTES:
        raise Refused(f"payload: {size} bytes, the limit is {MAX_BYTES}. Shorten the text.")
    return payload


def libraries():
    try:
        import segno
        import zxingcpp
        from PIL import Image
    except ImportError as err:
        raise DependencyMissing(
            f"{err.name} is not installed. In a virtual environment: "
            "pip install segno==1.6.6 zxing-cpp==3.1.1 pillow==12.3.0"
        ) from err
    return segno, zxingcpp, Image


def read_image(image, label: str) -> tuple[bytes, str, int]:
    """The raw bytes, error level and QR version of the one QR code in an image."""
    _, zxingcpp, _ = libraries()
    found = [b for b in zxingcpp.read_barcodes(image) if b.format == zxingcpp.BarcodeFormat.QRCode]
    if len(found) != 1:
        raise ProofFailed(f"{label}: {len(found)} QR codes found, expected exactly one")
    code = found[0]
    return code.bytes, code.ec_level, int(code.extra.get("Version", 0))


@contextlib.contextmanager
def open_image(path: Path, formats: tuple[str, ...]):
    """Open only an allowed format, and refuse a decompression bomb before any pixel is read."""
    _, _, Image = libraries()
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path, formats=formats) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError(
                    f"{image.width} x {image.height} pixels, the limit is {MAX_PIXELS}"
                )
            yield image


def decode(path: Path) -> tuple[bytes, str, int]:
    """Read back from the image file itself, never from what the encoder believes it wrote."""
    _, _, Image = libraries()
    label = printable(str(path))
    try:
        with open_image(path, READ_FORMATS) as image:
            return read_image(image, label)
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as err:
        raise ProofFailed(f"{label}: cannot read the image ({printable(str(err))})") from err


def degraded(image, ppm: int):
    """The image as a phone camera may see it: smaller, and slightly out of focus."""
    from PIL import ImageFilter

    _, _, Image = libraries()
    rgb = image.convert("RGB")
    for target in PROOF_PPM:
        size = (round(rgb.width * target / ppm), round(rgb.height * target / ppm))
        yield f"{target} px per module", rgb.resize(size, Image.BOX)
    blurred = rgb.filter(ImageFilter.GaussianBlur(ppm * PROOF_BLUR))
    yield f"blurred by {PROOF_BLUR} module", blurred
    size = (
        round(rgb.width * PROOF_COMBINED_PPM / ppm),
        round(rgb.height * PROOF_COMBINED_PPM / ppm),
    )
    yield f"blurred, then {PROOF_COMBINED_PPM} px per module", blurred.resize(size, Image.BOX)


def prove(
    path: Path,
    payload: str,
    *,
    level: str = "M",
    max_version: int = 13,
    robust: bool = False,
    ppm: int = CARD_PPM,
) -> tuple[int, str]:
    """The version and level the decoder read, once every check passed."""
    got, read_level, version = decode(path)
    label = printable(str(path))
    if got != payload.encode("utf-8"):
        raise ProofFailed(f"{label}: decodes to different bytes than the payload. {BUG_HINT}")
    if read_level != level or not 1 <= version <= max_version:
        raise ProofFailed(
            f"{label}: reads as version {version}, level {read_level}. "
            f"Expected level {level} and version {max_version} or lower. {BUG_HINT}"
        )
    if robust:
        with open_image(path, ("PNG",)) as image:
            for name, variant in degraded(image, ppm):
                if read_image(variant, f"{label} ({name})")[0] != payload.encode("utf-8"):
                    raise ProofFailed(f"{label} ({name}): decodes to different bytes. {BUG_HINT}")
    return version, read_level


def save_plain(qr, path: Path) -> int:
    qr.save(str(path), kind="png", scale=10, border=4)
    return 10


def encode(payload: str, error: str, min_version: int, max_version: int):
    """The smallest code that holds the payload, within the version range, or a refusal."""
    segno, _, _ = libraries()
    settings = dict(error=error, boost_error=False, micro=False, encoding="utf-8")
    try:
        qr = segno.make(payload, **settings)
        if qr.version < min_version:
            qr = segno.make(payload, version=min_version, **settings)
    except segno.DataOverflowError as err:
        raise Refused(f"data: too long for any QR version at level {error.upper()}") from err
    if qr.version > max_version:
        # Level H is used only with a logo, so this branch names the logo as the cause.
        fix = (
            "With a logo the limit is lower. Drop --logo, or shorten the address."
            if error == "h"
            else "Shorten it."
        )
        raise Refused(
            f"data: needs QR version {qr.version} at level {error.upper()}, "
            f"and the limit here is {max_version}. {fix}"
        )
    return qr


def make(
    payload: str,
    out: Path,
    *,
    error: str = "m",
    min_version: int = 1,
    max_version: int = 13,
    render=save_plain,
    robust: bool = False,
) -> str:
    """Write to a temporary file, prove it, then move it into place. Nothing unproven stays."""
    qr = encode(payload, error, min_version, max_version)
    out = Path(os.path.abspath(out))  # not resolve(): a symlink at --out is refused, not followed
    shown = printable(str(out))
    if out.suffix.lower() != ".png":
        raise Refused(f"out: {shown} does not end in .png, and the file is always a PNG")
    if out.is_symlink():
        raise Refused(f"out: {shown} is a symbolic link. Name the file itself.")
    if out.is_dir():
        raise Refused(f"out: {shown} is a folder. Name the PNG file to write.")
    if not out.parent.is_dir():
        raise Refused(f"out: the folder {printable(str(out.parent))} does not exist")
    umask = os.umask(0)
    os.umask(umask)
    # A replaced file keeps its own permissions, so a private file never becomes readable to others.
    # A new file gets the usual mode (mkstemp alone would make it 0600, unreadable to a print shop).
    mode = stat.S_IMODE(out.stat().st_mode) if out.exists() else 0o666 & ~umask
    try:
        fd, name = tempfile.mkstemp(prefix=".qr-", suffix=".png", dir=out.parent)
    except OSError as err:
        raise Refused(
            f"out: cannot write in {printable(str(out.parent))} ({err.strerror})"
        ) from err
    os.close(fd)
    temp = Path(name)
    proof = dict(level=error.upper(), max_version=max_version, robust=robust)
    try:
        proof["ppm"] = render(qr, temp)
        prove(temp, payload, **proof)
        os.chmod(temp, mode)
        os.replace(temp, out)
        placed = out.stat().st_ino
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    try:
        version, level = prove(out, payload, **proof)
    except BaseException:
        # Remove only the file this run placed: another writer may have replaced it meanwhile.
        with contextlib.suppress(OSError):
            if out.stat().st_ino == placed:
                out.unlink()
        raise
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    return f"VERIFIED {shown} sha256={digest} v{version}-{level}"


def clean_data(value: str) -> str:
    """Text mode accepts one line of printable text. A payment goes through `epc`, never here."""
    if value.startswith(("BCD\n", "BCD\r\n")):
        raise Refused(
            "data: this is an EPC payment payload. Build it with `epc`, which checks the IBAN, "
            "the name and the amount."
        )
    if unicodedata.normalize("NFC", value) != value:
        # Normalising would change the bytes, and a URL path or a signed token is bytes, not text.
        raise Refused(
            "data: holds a decomposed character (a letter plus a separate accent). Retype it as "
            "one character, or use the percent-encoded form of the address."
        )
    return clean_text(value, "data", 4296)  # 4,296 is the QR maximum; the version cap bites first


def zone_side(modules: int) -> int:
    """The largest odd square, padding included, within LOGO_AREA of the module area."""
    side = math.isqrt(int(LOGO_AREA * modules * modules))
    return side if side % 2 else side - 1


def zone_is_clear(version: int) -> bool:
    """True when the centered logo zone misses every finder, timing, format and alignment module."""
    n = 17 + 4 * version
    center, half = n // 2, zone_side(n) // 2
    zone = range(center - half, center + half + 1)
    tail = range(n - 8, n)

    def reserved(row: int, col: int) -> bool:
        finder = (row < 9 and col < 9) or (row < 9 and col in tail) or (row in tail and col < 9)
        timing = row == 6 or col == 6
        alignment = version >= 2 and n - 9 <= row <= n - 5 and n - 9 <= col <= n - 5
        # the finder squares include their separators and the format information beside them
        return finder or timing or alignment

    return not any(reserved(row, col) for row in zone for col in zone)


def find_font(size: int, path: Path | None = None):
    """The font named by --font, else the first system font found, else the Pillow fallback."""
    from PIL import ImageFont

    if path is not None:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError as err:
            raise Refused(f"font: cannot load {printable(str(path))} ({err})") from err
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    print(
        "note: no system font found. The caption uses the plain Pillow fallback.", file=sys.stderr
    )
    return ImageFont.load_default(size)


def parse_color(value: str, field: str) -> tuple[int, int, int]:
    from PIL import ImageColor

    try:
        return ImageColor.getrgb(value)[:3]
    except ValueError as err:
        raise Refused(f"{field}: {value!r} is not a color. Use #RRGGBB or a CSS name.") from err


def load_logo(logo: Path):
    """An allowed format, a sane size, and 8-bit channels, converted to RGBA."""
    _, _, Image = libraries()
    shown = printable(str(logo))
    try:
        with open_image(logo, LOGO_FORMATS) as image:
            if image.mode in ("I", "I;16", "I;16B", "I;16L", "F"):
                raise Refused(f"logo: {shown} has 16-bit or float pixels. Save it as an 8-bit PNG.")
            image.draft("RGBA", (1024, 1024))  # a JPEG decodes at a reduced size when it can
            return image.convert("RGBA")
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as err:
        raise Refused(
            f"logo: cannot use {shown} ({printable(str(err))}). "
            f"Allowed formats: {LOGO_FORMAT_LIST}."
        ) from err


def check_caption_script(caption: str) -> None:
    """The default fonts cover Latin, Greek and Cyrillic. Anything else needs --font."""
    for ch in caption:
        if ch.isalpha() and script(ch) not in LOOKALIKE_SCRIPTS:
            raise Refused(
                f"caption: {ch!r} is outside Latin, Greek and Cyrillic, so the default font may "
                "draw it as an empty box. Pass --font with a font that has it."
            )


def card_renderer(
    *, logo: Path | None, caption: str | None, color: str | None, ink: str, font: Path | None
):
    """A renderer for `make`: the code on a white panel, a cleared logo zone, a colored card."""
    _, _, Image = libraries()
    from PIL import ImageDraw

    frame = parse_color(color, "color") if color else None
    text_rgb = parse_color(ink, "caption color") if caption else None
    if caption and font is None:
        check_caption_script(caption)
    if font is not None:
        find_font(12, font)  # fail before any rendering when the font file is unusable
    mark = load_logo(logo) if logo else None

    def render(qr, path: Path) -> int:
        modules = len(qr.matrix)
        ppm, quiet = CARD_PPM, 4
        side = (modules + 2 * quiet) * ppm
        panel = Image.new("RGB", (side, side), "white")
        draw = ImageDraw.Draw(panel)
        center, half = modules // 2, zone_side(modules) // 2
        cleared = range(center - half, center + half + 1) if mark else range(0)
        for row, line in enumerate(qr.matrix):
            for col, dark in enumerate(line):
                if dark and not (row in cleared and col in cleared):
                    x, y = (col + quiet) * ppm, (row + quiet) * ppm
                    draw.rectangle((x, y, x + ppm - 1, y + ppm - 1), fill="black")
        if mark:
            inner = (2 * half - 1) * ppm  # one module of padding on each side of the zone
            fitted = mark.copy()
            fitted.thumbnail((inner, inner), Image.LANCZOS)
            panel.paste(fitted, ((side - fitted.width) // 2, (side - fitted.height) // 2), fitted)
        if frame is None and caption is None:
            panel.save(path, dpi=(300, 300))
            return ppm
        pad = 2 * ppm
        band = round(side * 0.22) if caption else pad
        width, height = side + 2 * pad, side + pad + band
        card = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(card)
        draw.rounded_rectangle(
            (0, 0, width - 1, height - 1), radius=3 * ppm, fill=frame or (51, 51, 51)
        )
        mask = Image.new("L", panel.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, side - 1, side - 1), radius=2 * ppm, fill=255)
        card.paste(panel, (pad, pad), mask)
        if caption:
            size = round(width * 0.06)
            face = find_font(size, font)
            while draw.textlength(caption, font=face) > width * 0.9 and size > width * 0.03:
                size -= 2
                face = face.font_variant(size=size)
            if draw.textlength(caption, font=face) > width * 0.9:
                raise Refused(
                    "caption: too long for the card even at the smallest font size. Shorten it."
                )
            draw.text(
                (width / 2, side + pad + band / 2), caption, font=face, fill=text_rgb, anchor="mm"
            )
        card.save(path, dpi=(300, 300))
        return ppm

    return render


def make_text(
    data: str,
    out: Path,
    *,
    logo: Path | None = None,
    caption: str | None = None,
    color: str | None = None,
    ink: str = "white",
    font: Path | None = None,
) -> str:
    """Text mode: level M, or level H with a logo. The finished card is proven, degraded too."""
    payload = clean_data(data)
    if caption is not None:
        caption = clean_text(caption, "caption", 80)
    render = card_renderer(logo=logo, caption=caption, color=color, ink=ink, font=font)
    return make(
        payload,
        out,
        error="h" if logo else "m",
        min_version=LOGO_MIN_VERSION if logo else 1,
        max_version=LOGO_MAX_VERSION if logo else TEXT_MAX_VERSION,
        render=render,
        robust=True,
    )


def split_payload(raw: bytes) -> list[str]:
    """Decode by the character set the payload names, and split on LF or CRLF."""
    head = raw.split(b"\n", 3)
    if head[0].rstrip(b"\r") != b"BCD":
        raise Refused("not an EPC QR code: the first element is not BCD")
    if len(head) < 3:
        raise Refused("not an EPC QR code: it ends before the character set element")
    code = head[2].rstrip(b"\r").decode("ascii", "replace")
    charset = CHARSETS.get(code)
    if charset is None:
        raise Refused(f"character set: {printable(code)!r}, allowed are 1 to 8")
    try:
        text = raw.decode(charset)
    except UnicodeDecodeError as err:
        raise Refused(
            f"character set {code}: the bytes are not valid {charset} "
            f"({err.reason} at byte {err.start})"
        ) from err
    return text.replace("\r\n", "\n").split("\n")


def check_symbol(raw: bytes, level: str, version: int) -> None:
    """The symbol rules EPC069-12 sets, checked on the code as read from the image."""
    if level != "M":
        raise Refused(f"error correction level {level}. EPC069-12 requires level M.")
    if not 1 <= version <= 13:
        raise Refused(f"QR version {version}. EPC069-12 allows version 13 or lower.")
    if len(raw) > MAX_BYTES:
        raise Refused(f"payload: {len(raw)} bytes. EPC069-12 allows {MAX_BYTES}.")


def check_foreign(fields: list[str]) -> list[str]:
    """Validate a code somebody else made. Lenient on form where the guideline allows it."""
    notes = []
    if fields and fields[-1] == "":
        notes.append("a separator follows the last element. EPC069-12 says nothing may.")
        fields = fields[:-1]
    if len(fields) < 7 or len(fields) > 12:
        raise Refused(f"{len(fields)} elements. An EPC QR code has 7 to 12.")
    fields += [""] * (12 - len(fields))
    tag, version, _charset, ident, bic, name, iban, amount, purpose, reference, text, info = fields
    if version not in ("001", "002") or ident != "SCT":
        raise Refused("version must be 001 or 002 and identification SCT")
    _, country = clean_iban(iban)
    if version == "001" and not bic:
        raise Refused("BIC: version 001 requires it")
    if bic or version == "002":
        clean_bic(bic, country)
    clean_text(name, "name", 70)
    if amount:
        match = re.fullmatch(r"EUR([0-9]{1,9}(?:\.[0-9]{1,2})?)", amount)
        if not match:
            raise Refused(f"amount: {printable(amount)!r} is not EUR plus up to two decimals")
        if not Decimal("0.01") <= Decimal(match.group(1)) <= Decimal("999999999.99"):
            raise Refused(f"amount: {amount} is outside EUR0.01 to EUR999999999.99")
    if purpose:
        clean_purpose(purpose)
    if reference and text:
        raise Refused("carries both a reference and a text")
    if reference:
        clean_reference(reference)
    if text:
        clean_text(text, "text", 140)
    if info:
        clean_text(info, "information", 70)
    return notes


def show(fields: list[str]) -> None:
    for number, (label, value) in enumerate(zip(FIELDS, fields), 1):
        print(f"{number:3d} {label:15s} {printable(value)}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # a Windows console would print cp1252 otherwise
    parser = argparse.ArgumentParser(description="QR code, EPC payment code or card, decode-proven")
    parser.add_argument("--selftest", action="store_true", help="offline tests, then exit")
    sub = parser.add_subparsers(dest="command")
    text = sub.add_parser("text", help="any one line of text, such as a URL, optionally on a card")
    text.add_argument("--data", required=True, help="the text to encode, exactly")
    text.add_argument("--logo", type=Path, help="an image for the center (forces level H)")
    text.add_argument("--caption", help="one line under the code, up to 80 characters")
    text.add_argument("--color", help="card color, #RRGGBB or a CSS name")
    text.add_argument("--caption-color", default="white", help="caption color (default white)")
    text.add_argument("--font", type=Path, help="a TrueType or OpenType font for the caption")
    text.add_argument("--out", required=True, type=Path, help="the PNG to write")
    build = sub.add_parser("epc", help="build and prove an EPC payment code (GiroCode)")
    build.add_argument("--name", required=True, help="payee, exactly as the account holder's name")
    build.add_argument("--iban", required=True)
    build.add_argument(
        "--amount", help="EUR, for example 42.00. Leave it out to let the payer type it."
    )
    build.add_argument("--text", help="unstructured remittance, up to 140 characters")
    build.add_argument(
        "--reference", help="structured remittance, up to 35 characters (RF... is checked)"
    )
    build.add_argument("--bic", help="required outside the EEA")
    build.add_argument("--purpose", help="ISO 20022 purpose code, up to 4 characters")
    build.add_argument("--info", help="beneficiary-to-originator information, up to 70 characters")
    build.add_argument("--out", required=True, type=Path, help="the PNG to write")
    read = sub.add_parser("epc-verify", help="decode an EPC payment code and check every element")
    read.add_argument("png", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.selftest:
            return selftest()
        if args.command == "text":
            print(
                make_text(
                    args.data,
                    args.out,
                    logo=args.logo,
                    caption=args.caption,
                    color=args.color,
                    ink=args.caption_color,
                    font=args.font,
                )
            )
            return EXIT_OK
        if args.command == "epc":
            payload = build_payload(
                name=args.name,
                iban=args.iban,
                amount=args.amount,
                text=args.text,
                reference=args.reference,
                bic=args.bic,
                purpose=args.purpose,
                info=args.info,
            )
            show(payload.split("\n"))
            print(make(payload, args.out))
            return EXIT_OK
        if args.command == "epc-verify":
            if not args.png.is_file():
                print(f"usage: {printable(str(args.png))} is not a file", file=sys.stderr)
                return EXIT_USAGE
            raw, level, version = decode(args.png)
            if not raw.startswith(b"BCD"):
                # Text from a code made by a stranger is data, never instructions: escaped and cut short.
                text = printable(raw.decode("utf-8", "replace")[:200])
                raise Refused(f"not an EPC payment code. Its untrusted text, escaped: {text!r}")
            check_symbol(raw, level, version)
            fields = split_payload(raw)
            show(fields)
            for note in check_foreign(fields):
                print(f"note: {note}")
            print(f"VALID {printable(str(args.png.resolve()))} v{version}-{level}")
            return EXIT_OK
    except Refused as err:
        print(f"refused: {err}", file=sys.stderr)
        return EXIT_REFUSED
    except DependencyMissing as err:
        print(f"dependency: {err}", file=sys.stderr)
        return EXIT_DEPENDENCY
    except ProofFailed as err:
        print(f"proof failed: {err}", file=sys.stderr)
        return EXIT_PROOF
    parser.print_usage(sys.stderr)
    return EXIT_USAGE


def selftest() -> int:
    """Offline. Every refusal, the boundaries, golden bytes, a round trip and two negative controls."""
    global PROOF_BLUR, PROOF_PPM  # two checks set them out of range on purpose, then restore them
    passed, failed = 0, []

    def check(label: str, condition: bool) -> None:
        nonlocal passed
        if condition:
            passed += 1
        else:
            failed.append(label)

    def refused(label: str, **fields) -> None:
        try:
            build_payload(**fields)
        except Refused:
            check(label, True)
        else:
            check(label, False)

    def refusal(call) -> str:
        """The refusal message, or an empty string when the call was not refused."""
        try:
            call()
        except Refused as err:
            return str(err)
        return ""

    # The guideline's own public examples (EPC069-12 v3.1, section 2.3), rebuilt as version 002.
    # V1's German IBAN is swapped for the documentation placeholder DE89 3704 0044 0532 0130 00:
    # same length, so the payload size and the QR version the guideline states are unchanged.
    v1 = dict(
        name="Franz Mustermänn",
        iban="DE89370400440532013000",
        amount="12.3",
        reference="RF18539007547034",
        bic="BHBLDEHHXXX",
        purpose="GDDS",
    )
    v2 = dict(
        name="François D'Alsace S.A.",
        iban="FR1420041010050500013M02606",
        amount="12.3",
        text="Client:Marie Louise La Lune",
    )
    check(
        "golden V1",
        build_payload(**v1) == "BCD\n002\n1\nSCT\nBHBLDEHHXXX\nFranz Mustermänn\n"
        "DE89370400440532013000\nEUR12.30\nGDDS\nRF18539007547034",
    )
    check(
        "golden V2",
        build_payload(**v2) == "BCD\n002\n1\nSCT\n\nFrançois D'Alsace S.A.\n"
        "FR1420041010050500013M02606\nEUR12.30\n\n\nClient:Marie Louise La Lune",
    )
    base = dict(name="Erika Mustermann", iban="DE89 3704 0044 0532 0130 00")
    check("last element is the IBAN", build_payload(**base).endswith("\nDE89370400440532013000"))
    check(
        "last element is the information",
        build_payload(**base, info="Thanks").endswith("013000\n\n\n\n\nThanks"),
    )
    check("no amount keeps the slot", build_payload(**base, text="x").endswith("013000\n\n\n\nx"))
    check("comma decimal", build_payload(**base, amount="42,5").endswith("EUR42.50"))

    refused("IBAN check digits", name="A", iban="FR1420041010050500013M02607")
    refused("IBAN length", name="A", iban="DE8937040044053201300")
    refused("IBAN outside SEPA", name="A", iban="US64SVBKUS6S3300958879")
    refused("non-EEA without BIC", name="A", iban="CH9300762011623852957")
    check(
        "non-EEA with BIC",
        build_payload(name="A", iban="CH9300762011623852957", bic="UBSWCHZH80A").startswith(
            "BCD\n002\n1\nSCT\nUBSWCHZH80A\n"
        ),
    )
    refused("BIC form", name="A", iban="DE89370400440532013000", bic="ABC")
    for bad in ("0.00", "0", "1000000000.00", "12.345", "1E+3", "NaN", "-5", "12."):
        refused(f"amount {bad}", name="A", iban="DE89370400440532013000", amount=bad)
    refused("text and reference", **base, text="x", reference="y")
    refused("RF check digits", **base, reference="RF19539007547034")
    check(
        "non-RF reference",
        build_payload(**base, reference="Invoice 2026-042").endswith("Invoice 2026-042"),
    )
    refused("purpose form", **base, purpose="gdds")
    for label, bad in (
        ("line feed", "Erika\nMustermann"),
        ("line separator", "Erika" + chr(0x2028) + "M"),
        ("zero-width", "Erika" + chr(0x200B) + "Mustermann"),
        ("no-break space", "Erika" + chr(0x00A0) + "M"),
        ("ligature", "Pro" + chr(0xFB01) + "t GmbH"),
        ("leading space", " Erika"),
        ("double space", "Erika  Mustermann"),
        ("empty", ""),
    ):
        refused(f"name {label}", name=bad, iban="DE89370400440532013000")

    for field, limit, extra in (
        ("name", 70, {}),
        ("text", 140, {"name": "A"}),
        ("reference", 35, {"name": "A"}),
        ("info", 70, {"name": "A"}),
    ):
        fields = dict(extra, iban="DE89370400440532013000")
        check(f"{field} at {limit}", bool(build_payload(**{**fields, field: "x" * limit})))
        refused(f"{field} at {limit + 1}", **{**fields, field: "x" * (limit + 1)})
    big = dict(name="ä" * 70, iban="DE89370400440532013000", amount="999999999.99")
    size = len(build_payload(**big, text="a").encode()) - 1
    check(
        "331 bytes", len(build_payload(**big, text="a" * (MAX_BYTES - size)).encode()) == MAX_BYTES
    )
    refused("332 bytes", **big, text="a" * (MAX_BYTES - size + 1))

    foreign = split_payload(
        "BCD\n001\n1\nSCT\nBHBLDEHHXXX\nFranz Mustermänn\nDE89370400440532013000"
        "\nEUR12.3\nGDDS\nRF18539007547034".encode()
    )
    check("verify accepts the guideline's V1 as printed", check_foreign(foreign) == [])
    check(
        "verify notes a trailing separator",
        len(check_foreign(split_payload(b"BCD\n002\n1\nSCT\n\nA\nDE89370400440532013000\n"))) == 1,
    )

    try:
        libraries()
    except DependencyMissing as err:
        print(f"dependency: {err}", file=sys.stderr)
        return EXIT_DEPENDENCY
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _, _, Image = libraries()
        good = folder / "v1.png"
        line = make(build_payload(**v1), good)
        check(
            "round trip VERIFIED line",
            line.startswith(f"VERIFIED {os.path.abspath(good)} sha256="),
        )
        check("round trip UTF-8 bytes", decode(good)[0] == build_payload(**v1).encode("utf-8"))
        check("guideline size: version 6", line.endswith(" v6-M"))
        with Image.open(good) as image:  # four modules of 10 px each: the ISO quiet zone
            gray = image.convert("L")
            ring = [gray.crop(box) for box in ((0, 0, gray.width, 40), (0, 0, 40, gray.height))]
            check(
                "EPC code keeps a 4-module quiet zone",
                all(r.getextrema() == (255, 255) for r in ring),
            )
        try:
            prove(good, build_payload(**v1), level="H")
        except ProofFailed:
            check("negative control: level M read as H", True)
        else:
            check("negative control: level M read as H", False)
        try:
            prove(good, build_payload(**v1), max_version=5)
        except ProofFailed:
            check("negative control: version above the limit", True)
        else:
            check("negative control: version above the limit", False)
        try:
            prove(good, build_payload(**v2))
        except ProofFailed:
            check("negative control: wrong payload", True)
        else:
            check("negative control: wrong payload", False)
        _, _, Image = libraries()
        blank = folder / "blank.png"
        Image.new("RGB", (400, 400), "white").save(blank)
        try:
            decode(blank)
        except ProofFailed:
            check("negative control: blank image", True)
        else:
            check("negative control: blank image", False)
        check(
            "nothing but the two files",
            sorted(p.name for p in folder.iterdir()) == ["blank.png", "v1.png"],
        )

    # Text mode. Every check reads the finished file back; nothing here trusts the encoder.
    from PIL import ImageDraw

    quiet = contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        url = "https://example.com/s/AbC123xyz"
        plain = folder / "plain.png"
        line = make_text(url, plain)
        check("text round trip bytes", decode(plain)[0] == url.encode("utf-8"))
        check("text without a logo is level M", line.endswith("-M"))
        logo = folder / "logo.png"
        Image.new("RGBA", (300, 300), (230, 120, 20, 255)).save(logo)
        card = folder / "card.png"
        line = make_text(url, card, logo=logo, caption="Hello", color="#5B3FA0")
        check("a logo forces level H", line.endswith("-H"))
        check("the card decodes to the data", decode(card)[0] == url.encode("utf-8"))
        check(
            "zone sides for versions 1 to 6 are 5, 5, 7, 7, 9, 9 modules",
            [zone_side(17 + 4 * v) for v in range(1, 7)] == [5, 5, 7, 7, 9, 9],
        )
        check("proof sizes are 3 and 2 px per module", PROOF_PPM == (3, 2))
        check("proof blur is 0.3 module", PROOF_BLUR == 0.3)
        check("combined pass is 2.5 px per module", PROOF_COMBINED_PPM == 2.5)
        with Image.open(card) as image:
            names = [name for name, _ in degraded(image, CARD_PPM)]
        check("the proof runs four degraded passes", len(names) == 4)
        black = folder / "black.png"
        Image.new("RGBA", (300, 300), (0, 0, 0, 255)).save(black)
        footprint = folder / "footprint.png"
        version = int(make_text(url, footprint, logo=black).rsplit("v", 1)[1].split("-")[0])
        half = zone_side(17 + 4 * version) // 2
        outer, inner = (half + 0.5) * CARD_PPM - 2, (half - 0.5) * CARD_PPM + 2
        with Image.open(footprint) as image:
            gray = image.convert("L")
            mid = gray.width / 2
            strips = [
                (mid - outer, mid - outer, mid + outer, mid - inner),
                (mid - outer, mid + inner, mid + outer, mid + outer),
                (mid - outer, mid - outer, mid - inner, mid + outer),
                (mid + inner, mid - outer, mid + outer, mid + outer),
            ]
            check(
                "an opaque logo keeps one module of white padding",
                all(gray.crop(tuple(map(round, s))).getextrema() == (255, 255) for s in strips),
            )
        for v in range(1, LOGO_MAX_VERSION + 1):
            check(f"zone avoids the function patterns at version {v}", zone_is_clear(v) == (v > 1))
        short = folder / "short.png"
        check(
            "a logo moves a version-1 code to version 2",
            "v2-H" in make_text("hi", short, logo=logo),
        )
        umask = os.umask(0)
        os.umask(umask)
        check("output honors the umask", (short.stat().st_mode & 0o777) == (0o666 & ~umask))
        private = folder / "private.png"
        private.write_bytes(b"old")
        private.chmod(0o600)
        make_text(url, private)
        check("a replaced file keeps its permissions", (private.stat().st_mode & 0o777) == 0o600)
        kept_blur, PROOF_BLUR = PROOF_BLUR, 3.0  # three modules of blur: the blur pass must fail
        try:
            make_text(url, stray := folder / "x.png")
        except ProofFailed:
            check("the blur pass can fail", True)
        else:
            check("the blur pass can fail", False)
        finally:
            PROOF_BLUR = kept_blur
        check("escapes a control character", printable("a\x1b[2Jb") == "a\\x1b[2Jb")
        clear_logo = folder / "clear.png"
        Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(clear_logo)
        zoned = folder / "zoned.png"
        version = int(make_text(url, zoned, logo=clear_logo).rsplit("v", 1)[1].split("-")[0])
        reach = (zone_side(17 + 4 * version) // 2 + 0.5) * CARD_PPM - 2  # inside the zone edge
        with Image.open(zoned) as image:
            gray = image.convert("L")
            mid = gray.width / 2
            zone = gray.crop(
                (round(mid - reach), round(mid - reach), round(mid + reach), round(mid + reach))
            )
            check("the logo zone is cleared to white", zone.getextrema() == (255, 255))
        stray = folder / "x.png"
        try:
            make_text("BCD\n002\n1\nSCT\n\nA\nDE89", stray)
        except Refused as err:
            check("text sends an EPC payload to `epc`", "`epc`" in str(err))
        else:
            check("text sends an EPC payload to `epc`", False)
        kept, PROOF_PPM = PROOF_PPM, (0.2,)  # unreadable on purpose: text mode must run this proof
        try:
            make_text(url, stray)
        except ProofFailed:
            check("text mode runs the degraded proof", True)
        else:
            check("text mode runs the degraded proof", False)
        finally:
            PROOF_PPM = kept
        eps = folder / "eps.png"  # PostScript under a PNG name: Pillow would hand it to Ghostscript
        eps.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n")
        deep = folder / "deep.png"
        Image.new("I;16", (20, 20)).save(deep)
        huge = folder / "huge.png"
        Image.new("1", (6000, 6000)).save(huge)
        link = folder / "link.png"
        link.symlink_to(folder / "target.png")
        for label, call in (
            ("text zero-width in data", lambda: make_text(url + chr(0x200B), stray)),
            (
                "text zero-width in caption",
                lambda: make_text(url, stray, caption="Hi" + chr(0x200B)),
            ),
            ("text caption too wide", lambda: make_text(url, stray, caption="W" * 400)),
            ("text color unknown", lambda: make_text(url, stray, color="not-a-color")),
            (
                "text mixed-script word",
                lambda: make_text("https://ex" + chr(0x430) + "mple.com", stray),
            ),
            (
                "text decomposed accent",
                lambda: make_text("https://example.com/e" + chr(0x301), stray),
            ),
            ("caption outside Latin, Greek, Cyrillic", lambda: make_text(url, stray, caption="募")),
            ("logo as EPS named .png", lambda: make_text(url, stray, logo=eps)),
            ("logo with 16-bit pixels", lambda: make_text(url, stray, logo=deep)),
            ("logo above the pixel limit", lambda: make_text(url, stray, logo=huge)),
            ("output not .png", lambda: make_text(url, folder / "x.jpg")),
            ("output is a symbolic link", lambda: make_text(url, link)),
            ("logo above version 6", lambda: make_text(url + "/" + "a" * 80, stray, logo=logo)),
            ("text above version 25", lambda: make_text(url + "/" + "a" * 1300, stray)),
            ("text beyond any version", lambda: make_text("a" * 3000, stray)),
            ("logo beyond any version", lambda: make_text("a" * 1400, stray, logo=logo)),
            ("output folder missing", lambda: make_text(url, folder / "none" / "x.png")),
            ("output is a folder", lambda: make_text(url, folder)),
        ):
            try:
                call()
            except Refused:
                check(label, True)
            else:
                check(label, False)
        check("refusals leave no file", not stray.exists())
        damaged = folder / "damaged.png"
        with Image.open(card) as image:
            image = image.convert("RGB")
            w, h = image.size
            ImageDraw.Draw(image).rectangle((w * 0.3, w * 0.3, w * 0.7, w * 0.7), fill="white")
            image.save(damaged)
        try:
            prove(damaged, url, level="H", max_version=6, robust=True)
        except ProofFailed:
            check("negative control: covered card", True)
        else:
            check("negative control: covered card", False)
        with quiet[0], quiet[1]:
            check("epc-verify on a URL code", main(["epc-verify", str(plain)]) == EXIT_REFUSED)
            try:
                main(
                    [
                        "epc",
                        "--name",
                        "A",
                        "--iban",
                        "DE89370400440532013000",
                        "--logo",
                        str(logo),
                        "--out",
                        str(stray),
                    ]
                )
            except SystemExit as err:
                check("epc takes no logo", err.code == EXIT_USAGE)
            else:
                check("epc takes no logo", False)
            segno, _, _ = libraries()
            crafted = {
                "level-h.png": dict(error="h", content=build_payload(**v1)),
                "bad-utf8.png": dict(
                    error="m", content=b"BCD\n002\n1\nSCT\n\nA\xff\nDE89370400440532013000"
                ),
                "version-14.png": dict(error="m", content=build_payload(**v1), version=14),
            }
            for name, spec in crafted.items():
                content = spec.pop("content")
                # encoding pinned: segno would otherwise write the ä of the sample as Latin-1, and
                # the code would be refused for its bytes before the symbol rules are ever read
                settings = dict(boost_error=False, micro=False, encoding="utf-8")
                segno.make(content, **settings, **spec).save(str(folder / name), scale=10, border=4)
                check(
                    f"epc-verify refuses {name}",
                    main(["epc-verify", str(folder / name)]) == EXIT_REFUSED,
                )
            check(
                "epc-verify: a missing file is a usage error",
                main(["epc-verify", str(stray)]) == EXIT_USAGE,
            )
            check(
                "epc-verify: an EPS file reads as no code",
                main(["epc-verify", str(eps)]) == EXIT_PROOF,
            )
        check(
            "a lone BCD element names the real fault",
            "ends before" in refusal(lambda: split_payload(b"BCD")),
        )
        check(
            "ASCII digits only in an IBAN",
            "IBAN"
            in refusal(lambda: clean_iban("DE" + chr(0x668) + chr(0x669) + "370400440532013000")),
        )
        check(
            "mixed-script payee refused",
            "mixes" in refusal(lambda: clean_text("Erik" + chr(0x430), "name", 70)),
        )
        check(
            "a whole Cyrillic name is accepted",
            clean_text("Иван Петров", "name", 70) == "Иван Петров",
        )
        check(
            "nothing but the expected files",
            sorted(p.name for p in folder.iterdir())
            == sorted(
                [
                    "bad-utf8.png",
                    "black.png",
                    "card.png",
                    "clear.png",
                    "damaged.png",
                    "deep.png",
                    "eps.png",
                    "footprint.png",
                    "huge.png",
                    "level-h.png",
                    "link.png",
                    "logo.png",
                    "plain.png",
                    "private.png",
                    "short.png",
                    "version-14.png",
                    "zoned.png",
                ]
            ),
        )

    for label in failed:
        print(f"FAIL: {label}", file=sys.stderr)
    print(f"qr selftest: {passed}/{passed + len(failed)} checks pass")
    return EXIT_OK if not failed else EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
