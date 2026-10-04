#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file of this skill.
"""Draw the example picture for SKILL.md from invented data.

Purpose: write din5008-letter-example.png, page one of the selftest's full letter, rendered and
    proven by letter.py. Every name, address, date, number and reference in it is invented.
Usage:
    python3 make_example_image.py [--out-dir DIR]   (default: the current folder; replaces the file)
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True  # importing letter must not leave a __pycache__ in the skill folder
import letter  # noqa: E402  (after the bytecode switch on purpose)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out-dir", default=".")
    out = Path(p.parse_args().out_dir) / "din5008-letter-example.png"
    with tempfile.TemporaryDirectory() as tmp:
        letter.build(letter.FULL, Path(tmp) / "example.pdf", png=out, force=True, quiet=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
