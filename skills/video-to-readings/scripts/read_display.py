#!/usr/bin/env python3
"""Turn a video of a scrolling instrument display into labelled contact sheets.

Purpose: extract frames at the rate of the camera itself, find the display panel in each one, score
    how sharp it is without being fooled by a dark frame, and tile the sharpest frame per
    time window into sheets a model can read in one look. It does NOT read the digits: a
    model reads them, off these sheets.

Usage:
    python3 read_display.py extract VIDEO FRAMES_DIR --fps 30 [--start 0 --duration 60]
    python3 read_display.py sheets FRAMES_DIR SHEETS_DIR --step 0.3 [--per-sheet 20 --cols 4]
    python3 read_display.py selftest

Needs Pillow and ffmpeg, nothing else. numpy is deliberately absent: the per-row and
per-column means it would provide are a BOX-filtered resize to one pixel, computed in
the C core of Pillow, and a pipeline without numpy also never asks anyone to unpickle a
downloaded artifact.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat
except ImportError:  # pragma: no cover - the message is the whole point
    sys.exit("Pillow is required: python3 -m pip install Pillow")

# Pillow moved the resampling constants under Image.Resampling in 9.1 and kept the aliases.
BOX = getattr(Image, "Resampling", Image).BOX
BILINEAR = getattr(Image, "Resampling", Image).BILINEAR

# A dark frame has almost no edges, so an edge-based sharpness score reports it as sharp.
# Rank only frames that carry a picture at all. The two bounds are taken, with thanks, from
# ADR 0008 of https://github.com/andrewii23/ii23-skills (MIT), which documents the same trap.
LUMA_MIN, LUMA_MAX = 25.0, 235.0
SPREAD_MIN = 60

# Sharpness is compared ACROSS frames, so it has to be measured on the same number of
# pixels every time. A detected panel varies in size from frame to frame, and an edge
# statistic taken on the raw crop makes "the sharpest frame" partly mean "the frame whose
# panel happened to be biggest".
SCORE_SIZE = (320, 160)

DARK_FRACTION = 0.30  # a row or column counts as panel when this much of it is dark
PANEL_MAX_AREA = 0.90  # a "panel" filling the frame is the detector failing, not a panel
PANEL_MIN_CONTRAST = 10.0  # the panel has to be darker than what surrounds it

FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


def load_font(size: int):
    """Return a label font that exists everywhere.

    ImageFont.load_default(size) returns a scalable font embedded in Pillow itself, so the
    fallback needs no font file at all. It needs Pillow 10.1 or newer. Never reach for the
    zero-argument form: it is 10 px on every version, and the labels stop being readable.
    """
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # pragma: no cover - Pillow < 10.1
        sys.exit("Pillow 10.1+ is required for a legible fallback font, or install DejaVu.")


def ffmpeg(*args: str) -> None:
    """Run ffmpeg and fail loudly, because a silent ffmpeg failure looks like an empty video."""
    exe = shutil.which("ffmpeg")
    if not exe:
        sys.exit("ffmpeg is required and was not found on PATH.")
    proc = subprocess.run([exe, "-nostdin", "-y", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"ffmpeg failed:\n{proc.stderr[-2000:]}")


# --------------------------------------------------------------------------- extract


def extract(video: Path, out: Path, start: float, duration: float | None, fps: float) -> Path:
    """Write frames plus the manifest that pins each frame to a real timestamp.

    The manifest exists because the alternative is arithmetic in the head of whoever wrote
    the extraction command. A hardcoded start-plus-index-over-fps in the labelling step is wrong
    the moment either value changes, and nothing errors: every label is simply off.
    """
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("f_*.jpg"):
        old.unlink()
    args = ["-ss", f"{start}"]
    if duration is not None:
        args += ["-t", f"{duration}"]
    args += ["-i", str(video), "-vf", f"fps={fps}", "-q:v", "3", str(out / "f_%06d.jpg")]
    ffmpeg(*args)
    frames = sorted(out.glob("f_*.jpg"))
    if not frames:
        sys.exit("ffmpeg produced no frames. Check the start time against the video length.")
    manifest = {
        "video": str(video),
        "start": start,
        "fps": fps,
        "count": len(frames),
        "frames": [{"file": f.name, "t": start + i / fps} for i, f in enumerate(frames)],
    }
    path = out / "manifest.json"
    path.write_text(json.dumps(manifest, indent=1))
    print(f"{len(frames)} frames at {fps} fps from t={start}s -> {out}")
    return path


# --------------------------------------------------------------------------- detect


def detect_panel(im: Image.Image) -> tuple[int, int, int, int] | None:
    """Return the bounding box of the display panel, or None when there is no panel.

    A resize to one pixel wide with a BOX filter IS the per-row mean, and to one pixel high
    is the per-column mean. That is the whole reason this needs no array library.
    """
    grey = im.convert("L")
    width, height = grey.size
    small_w = 320
    small_h = max(1, round(small_w * height / width))
    small = grey.resize((small_w, small_h), BILINEAR)
    stat = ImageStat.Stat(small)
    threshold = stat.mean[0] - 0.5 * stat.stddev[0]
    mask = small.point(lambda v: 255 if v < threshold else 0)

    row_mean = mask.resize((1, small_h), BOX).tobytes()
    col_mean = mask.resize((small_w, 1), BOX).tobytes()
    cut = DARK_FRACTION * 255
    rows = [i for i, v in enumerate(row_mean) if v > cut]
    cols = [i for i, v in enumerate(col_mean) if v > cut]
    if len(rows) < 8 or len(cols) < 20:
        return None

    scale_x, scale_y = width / small_w, height / small_h
    box = (
        max(0, int(cols[0] * scale_x)),
        max(0, int(rows[0] * scale_y)),
        min(width, int((cols[-1] + 1) * scale_x)),
        min(height, int((rows[-1] + 1) * scale_y)),
    )
    box_w, box_h = box[2] - box[0], box[3] - box[1]
    if box_w < width * 0.25 or box_h < height * 0.10:
        return None
    if box_w * box_h > PANEL_MAX_AREA * width * height:
        # The detector returned the frame. That is not a panel, and a caller that trusts it
        # goes on to "read" a picture of a wall.
        return None

    inside = ImageStat.Stat(grey.crop(box)).mean[0]
    fraction = (box_w * box_h) / float(width * height)
    # The area check above is load-bearing here, not only a plausibility test: a box that IS the
    # frame gives fraction 1.0 and this line raises ZeroDivisionError. PANEL_MAX_AREA below 1.0 is
    # what keeps the denominator away from zero, so do not raise it to 1.0 without a guard.
    outside = (stat.mean[0] - inside * fraction) / (1.0 - fraction)
    if outside - inside < PANEL_MIN_CONTRAST:
        return None
    return box


def score_sharpness(im: Image.Image, box: tuple[int, int, int, int]) -> float | None:
    """Return the edge energy of the panel, or None when the frame carries no usable picture."""
    crop = im.crop(box).convert("L").resize(SCORE_SIZE, BILINEAR)
    stat = ImageStat.Stat(crop)
    if not (LUMA_MIN <= stat.mean[0] <= LUMA_MAX):
        return None
    histogram = crop.histogram()
    total = sum(histogram)
    # `low is None` is the sentinel, never `low == 0`. Bin 0 is a legal answer: a crop where more
    # than a tenth of the pixels are pure black has its 10th percentile AT bin 0, and a zero
    # sentinel cannot tell "not found yet" from "found, and it is 0". The old form then kept
    # looking, assigned 1 on the next bin, and understated the spread by one level, which rejects
    # a frame sitting exactly on SPREAD_MIN.
    low: int | None = None
    high = 0
    seen = 0
    for value, count in enumerate(histogram):
        seen += count
        if low is None and seen >= total * 0.10:
            low = value
        if seen >= total * 0.90:
            high = value
            break
    if low is None or high - low < SPREAD_MIN:
        return None
    return ImageStat.Stat(crop.filter(ImageFilter.FIND_EDGES)).stddev[0]


# --------------------------------------------------------------------------- sheets


def build_sheets(
    frames_dir: Path,
    out_dir: Path,
    step: float,
    per_sheet: int,
    cols: int,
    cell: tuple[int, int],
) -> dict:
    """Tile the sharpest usable frame per time window, and report what was NOT covered."""
    manifest = json.loads((frames_dir / "manifest.json").read_text())
    entries = manifest["frames"]
    if not entries:
        sys.exit("The manifest holds no frames.")

    scored: list[tuple[float, str, float, tuple[int, int, int, int]]] = []
    no_panel = 0
    no_picture = 0
    for entry in entries:
        with Image.open(frames_dir / entry["file"]) as im:
            im.load()
            box = detect_panel(im)
            if box is None:
                no_panel += 1
                continue
            sharp = score_sharpness(im, box)
            if sharp is None:
                no_picture += 1
                continue
            scored.append((entry["t"], entry["file"], sharp, box))

    if not scored:
        sys.exit("No frame carried a readable panel. Fix the framing before reading anything.")

    first_t = entries[0]["t"]
    last_t = entries[-1]["t"]
    # int(...) + 1, never round(...). Rounding to nearest truncates the tail whenever the span is
    # not close to a whole number of windows, and the loss is silent in the one way this tool must
    # never be silent: the dropped frames are outside every window, so they are neither tiled NOR
    # counted as an unread window, and the report then states full coverage. Measured on a 5.3667 s
    # span at step 1.0: round gives 5 windows covering 5.0 s, the last 12 frames fall outside every
    # window, and the report reads "5 of 5 windows produced a tile" with empty_windows empty.
    # Truncating and adding one puts every frame in exactly one window, at the cost of a final
    # window that may be short, which is reported honestly if nothing lands in it.
    windows = max(1, int((last_t - first_t) / step) + 1)
    picks = []
    empty_windows = []
    for index in range(windows):
        low, high = first_t + index * step, first_t + (index + 1) * step
        inside = [s for s in scored if low <= s[0] < high]
        if not inside:
            empty_windows.append(round(low, 3))
            continue
        picks.append(max(inside, key=lambda s: s[2]))

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("sheet_*.jpg"):
        old.unlink()
    cell_w, cell_h = cell
    font = load_font(max(14, cell_h // 16))
    sheets: list[str] = []
    cells: list[dict] = []
    for start in range(0, len(picks), per_sheet):
        chunk = picks[start : start + per_sheet]
        tiles = []
        for moment, name, _sharp, box in chunk:
            with Image.open(frames_dir / name) as im:
                im.load()
                pad_x = int((box[2] - box[0]) * 0.05)
                pad_y = int((box[3] - box[1]) * 0.10)
                wide = (
                    max(0, box[0] - pad_x),
                    max(0, box[1] - pad_y),
                    min(im.width, box[2] + pad_x),
                    min(im.height, box[3] + pad_y),
                )
                tile = im.crop(wide).convert("RGB").resize((cell_w, cell_h), BILINEAR)
            draw = ImageDraw.Draw(tile)
            draw.rectangle((0, 0, int(cell_w * 0.22), int(cell_h * 0.09)), fill="black")
            draw.text((4, 2), f"{moment:.2f}s", font=font, fill="yellow")
            draw.rectangle((0, 0, cell_w - 1, cell_h - 1), outline="red", width=3)
            tiles.append(tile)
            cells.append(
                {"sheet": len(sheets) + 1, "cell": len(tiles), "frame": name, "t": moment}
            )
        rows = (len(tiles) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), "white")
        for i, tile in enumerate(tiles):
            sheet.paste(tile, ((i % cols) * cell_w, (i // cols) * cell_h))
        path = out_dir / f"sheet_{len(sheets) + 1:02d}.jpg"
        sheet.save(path, quality=88)
        sheets.append(path.name)

    report = {
        "frames": len(entries),
        "with_panel": len(scored),
        "rejected_no_panel": no_panel,
        "rejected_no_picture": no_picture,
        "windows": windows,
        "picked": len(picks),
        "empty_windows": empty_windows,
        "sheets": sheets,
        "cells": cells,
    }
    (out_dir / "sheets.json").write_text(json.dumps(report, indent=1))
    print(
        f"{len(entries)} frames, {len(scored)} with a readable panel "
        f"({no_panel} no panel, {no_picture} no picture)."
    )
    print(
        f"{len(picks)} of {windows} windows produced a tile "
        f"-> {len(sheets)} sheet(s) in {out_dir}"
    )
    if empty_windows:
        shown = empty_windows[:8]
        more = " ..." if len(empty_windows) > 8 else ""
        print(
            f"WARNING: {len(empty_windows)} window(s) produced NO tile, starting at "
            f"{shown}{more}. "
            "Those seconds are unread. Re-extract them at a finer step before you claim "
            "the series is complete."
        )
    return report


# --------------------------------------------------------------------------- selftest


FIXTURE_PANEL = (150, 120, 750, 320)


def panel_pattern(width: int, height: int) -> Image.Image:
    """A synthetic panel with two bright bars, at any size, for the scale-invariance check."""
    im = Image.new("L", (width, height), 16)
    draw = ImageDraw.Draw(im)
    draw.rectangle(
        (int(width * 0.15), int(height * 0.25), int(width * 0.28), int(height * 0.75)), fill=224
    )
    draw.rectangle(
        (int(width * 0.40), int(height * 0.25), int(width * 0.53), int(height * 0.75)), fill=224
    )
    return im


def make_fixture(path: Path, with_panel: bool = True, hide_from: float | None = None) -> None:
    """Build a six-second test video with ffmpeg alone.

    Exactly one frame per second stays unblurred, so the correct pick per one-second window
    is forced rather than decided by a tie-break between equally sharp frames. That frame
    sits in the MIDDLE of its window, at t = n + 0.5, and the placement is the whole point:
    with the sharp frame first, "pick the sharpest" and "pick the first" agree, and the
    assertion below passes over an implementation that never looks at sharpness at all.
    """
    if with_panel:
        # hide_from blanks the panel for one second, which is what a camera looking away
        # produces: frames that decode fine and hold nothing to read.
        gate = ""
        if hide_from is not None:
            gate = f":enable='not(between(t,{hide_from},{hide_from + 1}))'"
        video_filter = (
            f"drawbox=x=150:y=120:w=600:h=200:color=0x101010:t=fill{gate},"
            f"drawbox=x=200:y=170:w=60:h=100:color=0xE0E0E0:t=fill{gate},"
            f"drawbox=x=300:y=170:w=60:h=100:color=0xE0E0E0:t=fill{gate},"
            "gblur=sigma=6:enable='gt(abs(mod(t,1)-0.5),0.02)'"
        )
    else:
        video_filter = "noise=alls=8:allf=t+u"
    ffmpeg(
        "-f", "lavfi", "-i", "color=c=0xC8C8C8:s=900x440:r=30:d=6",
        "-vf", video_filter, "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path),
    )


def selftest() -> int:
    """Prove the whole path offline, asserting frame indices and sizes, never float scores."""
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        mark = "ok  " if ok else "FAIL"
        tail = " - " + detail if detail else ""
        print(f"  {mark} {name}{tail}")
        if not ok:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        print("fixture with a panel:")
        video = root / "panel.mp4"
        make_fixture(video, with_panel=True)
        frames = root / "frames"
        extract(video, frames, start=0.0, duration=None, fps=30.0)
        manifest = json.loads((frames / "manifest.json").read_text())
        check("180 frames extracted", manifest["count"] == 180, str(manifest["count"]))

        boxes = []
        for entry in manifest["frames"][:30]:
            with Image.open(frames / entry["file"]) as im:
                im.load()
                boxes.append(detect_panel(im))
        found = [b for b in boxes if b is not None]
        check("panel found in every sampled frame", len(found) == 30, f"{len(found)}/30")
        if found:
            worst = max(max(abs(a - b) for a, b in zip(box, FIXTURE_PANEL)) for box in found)
            check("panel box within 4 px of ground truth", worst <= 4, f"worst {worst} px")

        black = Image.new("L", (400, 200), 6)
        flat = Image.new("L", (400, 200), 128)
        whole = (0, 0, 400, 200)
        check("a near-black crop scores None", score_sharpness(black, whole) is None)
        check("a flat grey crop scores None", score_sharpness(flat, whole) is None)

        # The two crops above are rejected by the SPREAD test, not by the luma gate: both are flat,
        # so p90 - p10 is 0 either way. Nothing here pinned the luma gate until these three, and
        # removing it left the suite green. Each of the next two has ample spread and fails only on
        # its mean, so it is rejected by the luma gate alone.
        def bands(background: int, band: int) -> Image.Image:
            im = Image.new("L", (400, 200), background)
            ImageDraw.Draw(im).rectangle((0, 0, 399, 23), fill=band)  # 24 of 200 rows, so 12%
            return im

        check(
            "a dark crop with plenty of contrast is still rejected, on its mean alone",
            score_sharpness(bands(0, 120), whole) is None,
            "mean 14.4, spread 120",
        )
        check(
            "a blown-out crop with plenty of contrast is rejected the same way",
            score_sharpness(bands(255, 135), whole) is None,
            "mean 240.6, spread 120",
        )

        # Exactly on SPREAD_MIN, with the 10th percentile AT bin 0. This is the case a zero
        # sentinel in the percentile walk gets wrong: it cannot tell "not found yet" from
        # "found, and it is 0", reports low as 1, and rejects a frame that is inside the bound.
        edge = Image.new("L", (400, 200), 0)
        ImageDraw.Draw(edge).rectangle((0, 0, 399, 99), fill=SPREAD_MIN)
        check(
            "a crop sitting exactly on the spread bound is accepted, not rejected by one level",
            score_sharpness(edge, whole) is not None,
            f"p10 at bin 0, p90 at bin {SPREAD_MIN}",
        )

        # The same panel filmed closer must not score differently. Measured on the raw crop
        # the score halves as the panel grows, so "the sharpest frame" would really mean
        # "the frame whose panel was smallest", which is the opposite of the intent.
        sizes = [(400, 200), (900, 450), (1600, 800)]
        scores = [score_sharpness(panel_pattern(w, h), (0, 0, w, h)) for w, h in sizes]
        spread = (max(scores) - min(scores)) / (sum(scores) / len(scores))
        check(
            "score is independent of how large the panel is in frame",
            spread < 0.10,
            f"spread {spread:.1%} over {[round(s, 1) for s in scores]}",
        )

        report = build_sheets(
            frames, root / "sheets", step=1.0, per_sheet=20, cols=3, cell=(700, 420)
        )
        picked = [c["frame"] for c in report["cells"]]
        expected = [f"f_{i:06d}.jpg" for i in (16, 46, 76, 106, 136, 166)]
        check("one tile per second, the unblurred MIDDLE frame", picked == expected, str(picked))
        check("no window left empty", not report["empty_windows"], str(report["empty_windows"]))
        with Image.open(root / "sheets" / "sheet_01.jpg") as im:
            size = im.size
        check("sheet is 2100x840", size == (2100, 840), str(size))

        print("fixture with the panel hidden for one second:")
        gapped = root / "gap.mp4"
        make_fixture(gapped, with_panel=True, hide_from=2.0)
        gap_frames = root / "gframes"
        extract(gapped, gap_frames, start=0.0, duration=None, fps=30.0)
        gap_report = build_sheets(
            gap_frames, root / "gsheets", step=1.0, per_sheet=20, cols=3, cell=(700, 420)
        )
        check(
            "the unread second is reported, not silently dropped",
            gap_report["empty_windows"] == [2.0],
            str(gap_report["empty_windows"]),
        )
        check(
            "five windows produced a tile, not six",
            gap_report["picked"] == 5,
            str(gap_report["picked"]),
        )

        # A span that is NOT close to a whole number of windows. This is the case a round() in the
        # window count drops silently: the tail frames land outside every window, so they are
        # neither tiled nor reported as unread, and the report claims full coverage.
        print("fixture cut so the last window is a partial one:")
        tail_frames = root / "tframes"
        extract(video, tail_frames, start=0.0, duration=5.4, fps=30.0)
        tail_manifest = json.loads((tail_frames / "manifest.json").read_text())
        tail_report = build_sheets(
            tail_frames, root / "tsheets", step=1.0, per_sheet=20, cols=3, cell=(700, 420)
        )
        span = tail_manifest["frames"][-1]["t"] - tail_manifest["frames"][0]["t"]
        check(
            "a partial last window is still a window",
            tail_report["windows"] == 6,
            f"span {span:.4f}s over step 1.0 gave {tail_report['windows']} window(s)",
        )
        covered = tail_manifest["frames"][0]["t"] + tail_report["windows"] * 1.0
        beyond = [e for e in tail_manifest["frames"] if e["t"] >= covered]
        check(
            "no frame falls outside every window",
            not beyond,
            f"{len(beyond)} frame(s) past {covered:.4f}s would be unread AND unreported",
        )

        print("fixture with NO panel:")
        blank = root / "blank.mp4"
        make_fixture(blank, with_panel=False)
        blank_frames = root / "bframes"
        extract(blank, blank_frames, start=0.0, duration=None, fps=2.0)
        blank_manifest = json.loads((blank_frames / "manifest.json").read_text())
        rejected = 0
        for entry in blank_manifest["frames"]:
            with Image.open(blank_frames / entry["file"]) as im:
                im.load()
                if detect_panel(im) is None:
                    rejected += 1
        expected_total = blank_manifest["count"]
        check(
            "no panel is reported where there is none",
            rejected == expected_total,
            f"{rejected} of {expected_total} rejected",
        )

    verdict = "PASS" if not failures else "FAIL"
    print(f"\n{verdict}: {len(failures)} failures")
    return 1 if failures else 0


# --------------------------------------------------------------------------- cli


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd")

    extract_cmd = sub.add_parser(
        "extract", help="frames plus the manifest that pins each frame to a timestamp"
    )
    extract_cmd.add_argument("video", type=Path)
    extract_cmd.add_argument("out", type=Path)
    extract_cmd.add_argument("--start", type=float, default=0.0)
    extract_cmd.add_argument("--duration", type=float, default=None)
    extract_cmd.add_argument(
        "--fps", type=float, required=True, help="the rate of the CAMERA itself, never a guess"
    )

    sheets_cmd = sub.add_parser(
        "sheets", help="labelled contact sheets, sharpest usable frame per window"
    )
    sheets_cmd.add_argument("frames", type=Path)
    sheets_cmd.add_argument("out", type=Path)
    sheets_cmd.add_argument("--step", type=float, required=True, help="seconds per window")
    sheets_cmd.add_argument("--per-sheet", type=int, default=20)
    sheets_cmd.add_argument("--cols", type=int, default=4)
    sheets_cmd.add_argument("--cell", type=int, nargs=2, default=(700, 420), metavar=("W", "H"))

    sub.add_parser("selftest", help="prove the whole path on a generated fixture")

    args = parser.parse_args()
    if args.cmd == "extract":
        extract(args.video, args.out, args.start, args.duration, args.fps)
        return 0
    if args.cmd == "sheets":
        build_sheets(
            args.frames, args.out, args.step, args.per_sheet, args.cols, tuple(args.cell)
        )
        return 0
    if args.cmd == "selftest":
        return selftest()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
