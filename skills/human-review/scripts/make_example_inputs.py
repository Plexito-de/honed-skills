#!/usr/bin/env python3
"""Build the placeholder files behind the screenshots in the README, then open them for review.

The first screenshot shows a card and a draft on one page with three marks on them. The second
shows a vertical Short with its voice-over script. This script produces those files. It does not
draw the marks: a person draws them, which is the whole point of the skill, so each picture in the
README is a real session rather than a mock.

Everything here is invented. No real product, person, venue or date.

    python3 scripts/make_example_inputs.py            # write the files, print the command
    python3 scripts/make_example_inputs.py --open     # write them and open the review page
    python3 scripts/make_example_inputs.py --video    # the Short and its script instead

Standard library only. Rendering the card and the frames of the Short needs a Chrome or Chromium
binary, because each is an HTML file screenshotted at a fixed size. The Short also needs ffmpeg,
which joins the frames into a video. Everything else is text.
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

# The Short: three scenes of two seconds each, 540x960. Scene two puts its caption too low on
# purpose, so there is something to circle.
SHORT_SCENE = """<!doctype html>
<meta charset="utf-8">
<style>
  html, body {{ margin: 0; }}
  body {{ width: 540px; height: 960px; font-family: "Helvetica Neue", Arial, sans-serif;
         background: {bg}; color: #f8fafc; position: relative; overflow: hidden; }}
  .big {{ position: absolute; left: 44px; right: 44px; top: {top}px; font-size: {size}px;
          line-height: 1.02; font-weight: 800; letter-spacing: -0.01em; }}
  .cap {{ position: absolute; left: 50%; transform: translateX(-50%); bottom: {cap_bottom}px;
          background: rgba(10, 14, 22, .78); padding: 10px 18px; border-radius: 10px;
          font-size: 25px; font-weight: 600; white-space: nowrap; }}
  .tag {{ position: absolute; left: 44px; top: 48px; font-size: 17px; letter-spacing: .3em;
          text-transform: uppercase; opacity: .75; }}
</style>
<div class="tag">Morning lab</div>
<div class="big">{text}</div>
<div class="cap">{caption}</div>
"""
SHORT_SCENES = (
    {
        "bg": "linear-gradient(160deg, #3b1d60 0%, #a3324d 100%)",
        "top": 300,
        "size": 64,
        "text": "Three things your mornings get wrong",
        "caption": "Stay to the end",
        "cap_bottom": 150,
    },
    {
        "bg": "linear-gradient(160deg, #0f3d3e 0%, #1f7a6d 100%)",
        "top": 250,
        "size": 58,
        "text": "1. The phone comes first",
        "caption": "Leave it in another room",
        "cap_bottom": 18,
    },
    {
        "bg": "linear-gradient(160deg, #1d2b53 0%, #3a6ea5 100%)",
        "top": 330,
        "size": 60,
        "text": "Follow for part two",
        "caption": "New every Friday",
        "cap_bottom": 150,
    },
)
SHORT_SCRIPT = """# Morning lab, episode 1: voice-over

Three things your mornings get wrong. Stay to the end.

The first one: the phone comes first. Leave it in another room tonight.

Follow for part two. A new one every Friday.
"""


def make_short(out: Path) -> tuple[Path, Path]:
    """The Short and its voice-over script, as invented as the card."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("ffmpeg is needed to join the frames of the Short into a video")
    frames = []
    for n, scene in enumerate(SHORT_SCENES, 1):
        html = out / f"scene{n}.html"
        png = out / f"scene{n}.png"
        html.write_text(SHORT_SCENE.format(**scene))
        subprocess.run(
            [
                chrome(),
                "--headless",
                "--disable-gpu",
                "--hide-scrollbars",
                "--window-size=540,960",
                f"--screenshot={png}",
                str(html),
            ],
            check=True,
            capture_output=True,
        )
        html.unlink()
        frames.append(png)
    short = out / "morning-lab-ep1.mp4"
    inputs = []
    for png in frames:
        inputs += ["-loop", "1", "-t", "2", "-framerate", "30", "-i", str(png)]
    joined = "".join(f"[{i}:v]" for i in range(len(frames)))
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *inputs,
            "-f",
            "lavfi",
            "-t",
            "6",
            "-i",
            "sine=frequency=330:sample_rate=48000",
            "-filter_complex",
            f"{joined}concat=n={len(frames)}:v=1:a=0,format=yuv420p[v]",
            "-map",
            "[v]",
            "-map",
            f"{len(frames)}:a",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(short),
        ],
        check=True,
        capture_output=True,
    )
    for png in frames:
        png.unlink()
    script = out / "voice-over.md"
    script.write_text(SHORT_SCRIPT)
    return short, script


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
    ap.add_argument(
        "--video",
        action="store_true",
        help="make the Short and its script instead of the card and the draft",
    )
    args = ap.parse_args()

    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="human-review-example-"))
    out.mkdir(parents=True, exist_ok=True)
    review = Path(__file__).resolve().parent.parent / "bin" / "review"
    if args.video:
        short, script = make_short(out)
        print(f"short:  {short}\nscript: {script}")
        if args.open:
            subprocess.run([str(review), "open", str(short), str(script)], check=True)
        else:
            print(f"\nopen it with:\n  {review} open {short} {script}")
        print(
            "\nThen mark it yourself. The README picture shows three notes:\n"
            "  1  a box on the caption at 0:03, note: the caption sits in the app's buttons\n"
            "  2  a note at 0:00.5 with no drawing, note: the music starts too loud\n"
            "  3  a span from 0:04 to 0:06, note: the end card can go after one second"
        )
        return 0
    draft = out / "launch-note.md"
    html = out / "card.html"
    card = out / "card.png"
    draft.write_text(DRAFT)
    html.write_text(CARD_HTML)

    subprocess.run([chrome(), "--headless", "--disable-gpu", "--hide-scrollbars",
                    "--window-size=1000,620", f"--screenshot={card}", str(html)],
                   check=True, capture_output=True)
    html.unlink()

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
