#!/usr/bin/env python3
"""Build the two placeholder files behind the screenshot in the README, then open them for review.

The screenshot shows a card and a draft on one page with three marks on them. This script produces
the card and the draft. It does not draw the marks: a person draws them, which is the whole point of
the skill, so the picture in the README is a real session rather than a mock.

Everything here is invented. No real product, person, venue or date.

    python3 scripts/make_example_inputs.py            # write the files, print the command
    python3 scripts/make_example_inputs.py --open     # write them and open the review page

Standard library only. Rendering the card needs a Chrome or Chromium binary, because the card is an
HTML file screenshotted at a fixed size. Everything else is text.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
)

DRAFT = """# Aurora, spring release

Aurora ships on 12 March. This note goes to the mailing list on the morning of the
launch, so it has to say what changed, who it is for, and what to do next.

## What is new

- **One page for the whole review.** Text and pictures no longer live in two tools.
- **A note per mark.** Circle the spot, say what is wrong, move on.
- **Crops come back with the batch**, so the reader sees the detail, not a coordinate.
"""
CARD_HTML = """<!doctype html>
<meta charset="utf-8">
<style>
  html, body { margin: 0; }
  body { width: 1000px; height: 620px; font-family: "Helvetica Neue", Arial, sans-serif;
         background: linear-gradient(140deg, #10233f 0%, #1d3b63 55%, #2f5c86 100%);
         color: #f4f7fb; display: flex; align-items: stretch;
         padding: 54px 60px; box-sizing: border-box; gap: 48px; }
  .left { flex: 1; display: flex; flex-direction: column; justify-content: space-between; }
  .kicker { letter-spacing: .34em; text-transform: uppercase; font-size: 16px; opacity: .72; }
  h1 { font-size: 104px; line-height: .9; margin: 18px 0 0; font-weight: 700; }
  .rule { height: 4px; width: 150px; background: #ffce4f; margin: 26px 0 0; }
  p { font-size: 23px; line-height: 1.45; margin: 26px 0 0; max-width: 470px; opacity: .9; }
  .right { width: 320px; display: flex; flex-direction: column;
           align-items: flex-end; justify-content: space-between; text-align: right; }
  .dot { width: 118px; height: 118px; border-radius: 50%; border: 4px solid #ffce4f;
         display: flex; align-items: center; justify-content: center;
         font-size: 34px; color: #ffce4f; }
  .meta { font-size: 19px; line-height: 1.6; opacity: .85; }
  .meta b { display: block; font-size: 22px; opacity: 1; margin-bottom: 4px; }
</style>
<div class="left">
  <div>
    <div class="kicker">Spring release</div>
    <h1>Aurora</h1>
    <div class="rule"></div>
    <p>One page for the whole review. Text and pictures together, one batch back.</p>
  </div>
</div>
<div class="right">
  <div class="dot">12/3</div>
  <div class="meta">
    <b>Thursday 12 March, 18:00</b>
    Gallery Nine, 4 Example Street
    <br>Doors at 17:30. No ticket needed.
  </div>
</div>
"""

def chrome() -> str:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
        found = shutil.which(c)
        if found:
            return found
    sys.exit("no Chrome or Chromium found: the card is an HTML file rendered to PNG")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="directory for the files (default: a temp directory)")
    ap.add_argument("--open", action="store_true", help="also open the review page on them")
    args = ap.parse_args()

    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="human-review-example-"))
    out.mkdir(parents=True, exist_ok=True)
    draft = out / "launch-note.md"
    html = out / "card.html"
    card = out / "card.png"
    draft.write_text(DRAFT)
    html.write_text(CARD_HTML)

    subprocess.run([chrome(), "--headless", "--disable-gpu", "--hide-scrollbars",
                    "--window-size=1000,620", f"--screenshot={card}", str(html)],
                   check=True, capture_output=True)
    html.unlink()

    review = Path(__file__).resolve().parent.parent / "bin" / "review"
    print(f"draft: {draft}\ncard:  {card}")
    if args.open:
        subprocess.run([str(review), "open", str(card), str(draft)], check=True)
    else:
        print(f"\nopen it with:\n  {review} open {card} {draft}")
    print("\nThen draw the marks yourself. The README picture shows three:\n"
          "  1  a circle on the date, note: this reads as a price\n"
          "  2  an arrow at the headline, note: too much air under it\n"
          "  3  a comment on a selected sentence, plus one retyped line")
    return 0


if __name__ == "__main__":
    sys.exit(main())
