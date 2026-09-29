#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file beside this script.
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
# Rank only frames that carry a picture at all. ALL THREE constants below, and the two-gate
# shape they form, are taken with thanks from ADR 0008 of
# https://github.com/andrewii23/ii23-skills, which documents the same trap: the luma bounds
# and the histogram-spread floor are its answer, not just its first half.
#   MIT License, Copyright (c) 2026 warodom (andrewii23).
# No code was copied; what is reused is the two thresholds and the idea of gating before
# ranking. The notice is here because MIT asks for the copyright line, and because a reader
# who wants the reasoning should be able to find whose it is.
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
        except (OSError, ImportError):
            # ImportError, not only OSError: a Pillow built without FreeType raises it from
            # truetype itself, which is the one environment where the fallback below matters.
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


def probe_duration(video: Path) -> float | None:
    """Seconds of video, or None when ffprobe is absent or cannot tell.

    None is a legitimate answer and never an error: a container without a duration header
    exists, and refusing to extract from one would be worse than the mistake this prevents.
    """
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    proc = subprocess.run(
        [exe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        capture_output=True,
        text=True,
    )
    try:
        return float(proc.stdout.strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- extract


def extract(video: Path, out: Path, start: float, duration: float | None, fps: float) -> Path:
    """Write frames plus the manifest that pins each frame to a real timestamp.

    The manifest exists because the alternative is arithmetic in the head of whoever wrote
    the extraction command. A hardcoded start-plus-index-over-fps in the labelling step is wrong
    the moment either value changes, and nothing errors: every label is simply off.
    """
    # Nothing is deleted until the input has been checked. Measured against the version before
    # this line: pointing the command at a video that does not exist still unlinked the
    # pre-existing f_*.jpg in the output directory and then failed, so the user paid a delete
    # and got nothing back. An input error must never cost the caller data.
    if not video.is_file():
        sys.exit(f"{video} is not a file, so nothing was read and nothing was deleted.")

    # Check the start against the real length FIRST. The friendly message further down is
    # unreachable when --start is past the end: ffmpeg returns non-zero, ffmpeg() exits on it,
    # and all the user sees is "Terminating thread with return code -22 (Invalid argument)"
    # and "Conversion failed!", which names neither the start time nor the video. This is the
    # likeliest first mistake, because the documented example carries a --start of its own.
    length = probe_duration(video)
    if length is not None and start >= length:
        sys.exit(
            f"--start {start}s is past the end of this {length:.1f}s video, so there is "
            "nothing to extract. Drop --start and --duration to take the whole clip."
        )
    if length is not None and duration is not None and start + duration > length + 0.5:
        print(
            f"NOTE: --start {start} plus --duration {duration} runs past the end of this "
            f"{length:.1f}s video. Extracting to the end instead."
        )

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


def detect_panel(im: Image.Image, invert: bool = False) -> tuple[int, int, int, int] | None:
    """Return the bounding box of the display panel, or None when there is no panel.

    A resize to one pixel wide with a BOX filter IS the per-row mean, and to one pixel high
    is the per-column mean. That is the whole reason this needs no array library.

    The search is for a region DARKER than its surroundings, which is a non-backlit LCD read
    in ambient light. A backlit panel is the other way round and every frame then comes back
    with no panel, so `invert` flips the greyscale first and the same logic finds it. The
    caller decides; `build_sheets` probes for it and says so rather than leaving the user to
    guess, because the failure looks exactly like bad framing and is not.
    """
    grey = im.convert("L")
    if invert:
        grey = grey.point(lambda v: 255 - v)
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
    invert: bool = False,
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
            box = detect_panel(im, invert=invert)
            if box is None:
                no_panel += 1
                continue
            sharp = score_sharpness(im, box)
            if sharp is None:
                no_picture += 1
                continue
            scored.append((entry["t"], entry["file"], sharp, box))

    if not scored:
        # "Fix the framing" is the wrong advice more than half the time, and it costs the user a
        # re-shoot they did not need: a backlit panel is BRIGHTER than its body, which is the
        # opposite of what the detector looks for, and a perfectly framed clip then reports
        # nothing at all. So probe the inverse before blaming the camera.
        if not invert:
            probe = entries[:: max(1, len(entries) // 12)][:12]
            hits = 0
            for entry in probe:
                with Image.open(frames_dir / entry["file"]) as im:
                    im.load()
                    if detect_panel(im, invert=True) is not None:
                        hits += 1
            if hits >= len(probe) // 2:
                sys.exit(
                    f"No frame carried a readable panel, but {hits} of {len(probe)} sampled "
                    "frames DO carry one when the image is inverted. Your panel is lighter "
                    "than the device around it, which is a backlit LCD. The framing is fine. "
                    "Re-run this command with --invert."
                )
        sys.exit(
            "No frame carried a readable panel, with and without --invert. The detector needs a "
            "panel that contrasts with the body around it and fills at least a quarter of the "
            "frame width. Check the framing and the lighting before reading anything."
        )

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
        path = out_dir / f"sheet_{len(sheets) + 1:03d}.jpg"
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
        # score_sharpness returns Optional, and max() over a list holding None raises a
        # TypeError that says nothing about which size failed. Fail on the real question.
        check("every synthetic size produced a score", all(s is not None for s in scores),
              str(scores))
        scores = [s for s in scores if s is not None] or [0.0]
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
        with Image.open(root / "sheets" / "sheet_001.jpg") as im:
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

        # A backlit LCD is brighter than its body, which is the reverse of what the detector
        # assumes. Without --invert the run must say so instead of blaming the framing, which
        # is the wrong advice and costs a re-shoot; with --invert it must simply work.
        # An input error must not cost the caller data. Before the existence check, pointing
        # the command at a missing video still deleted the f_*.jpg already in the output
        # directory and then failed, so the user paid a delete and received nothing.
        keep = root / "keepme"
        keep.mkdir()
        (keep / "f_000001.jpg").write_bytes(b"not mine to delete")
        import subprocess as _sp0
        missing = _sp0.run(
            [sys.executable, str(Path(__file__).resolve()), "extract",
             str(root / "no-such-video.mp4"), str(keep), "--fps", "30"],
            capture_output=True, text=True,
        )
        check(
            "a missing input deletes nothing in the output directory",
            missing.returncode != 0 and (keep / "f_000001.jpg").exists(),
            f"rc={missing.returncode}, file kept={(keep / 'f_000001.jpg').exists()}",
        )

        # The documented example carries a --start of its own, so a stranger with a short clip
        # copies it. Before this check the only output was ffmpeg's "return code -22".
        probed = probe_duration(video)
        check("the video's real length is probed", probed is not None and 5.9 < probed < 6.2,
              f"{probed}")
        import subprocess as _sp
        far = _sp.run(
            [sys.executable, str(Path(__file__).resolve()), "extract", str(video),
             str(root / "never"), "--fps", "30", "--start", "900", "--duration", "60"],
            capture_output=True, text=True,
        )
        # Match the FATAL wording only. "past the end of this" also appears in the harmless
        # NOTE printed when start+duration overruns, so the looser check passed with the fatal
        # guard deleted: two code paths, one substring, and a mutant walked straight through it.
        far_out = far.stderr + far.stdout
        check(
            "a --start past the end names the start and the length, not ffmpeg's errno",
            far.returncode != 0 and "so there is nothing to extract" in far_out,
            far_out.strip().splitlines()[-1][:90] if far_out.strip() else "(no output)",
        )

        print("fixture with a BRIGHT panel on a dark body:")
        bright = root / "bright.mp4"
        ffmpeg(
            "-f", "lavfi", "-i", "color=c=0x202020:s=900x440:r=30:d=3",
            "-vf", "drawbox=x=150:y=120:w=600:h=200:color=0xD8E8D0:t=fill,"
                   "drawbox=x=200:y=170:w=60:h=100:color=0x102010:t=fill,"
                   "drawbox=x=300:y=170:w=60:h=100:color=0x102010:t=fill",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(bright),
        )
        bright_frames = root / "brframes"
        extract(bright, bright_frames, start=0.0, duration=None, fps=10.0)
        with Image.open(sorted(bright_frames.glob("f_*.jpg"))[0]) as im:
            im.load()
            plain = detect_panel(im)
            flipped = detect_panel(im, invert=True)
        check("a bright panel is invisible to the default detector", plain is None, str(plain))
        check("the same panel is found with invert", flipped is not None, str(flipped))
        bright_report = build_sheets(
            bright_frames, root / "brsheets", step=1.0, per_sheet=20, cols=3,
            cell=(700, 420), invert=True,
        )
        check(
            "invert recovers every window of a backlit clip",
            bright_report["picked"] == bright_report["windows"],
            f"{bright_report['picked']} of {bright_report['windows']}",
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
    extract_cmd.add_argument(
        "out", type=Path, help="frame directory. Any existing f_*.jpg in it is DELETED first"
    )
    extract_cmd.add_argument("--start", type=float, default=0.0)
    extract_cmd.add_argument("--duration", type=float, default=None)
    extract_cmd.add_argument(
        "--fps", type=float, required=True, help="the rate of the CAMERA itself, never a guess"
    )

    sheets_cmd = sub.add_parser(
        "sheets", help="labelled contact sheets, sharpest usable frame per window"
    )
    sheets_cmd.add_argument("frames", type=Path)
    sheets_cmd.add_argument(
        "out", type=Path, help="sheet directory. Any existing sheet_*.jpg in it is DELETED first"
    )
    sheets_cmd.add_argument("--step", type=float, required=True, help="seconds per window")
    sheets_cmd.add_argument("--per-sheet", type=int, default=20)
    sheets_cmd.add_argument("--cols", type=int, default=4)
    sheets_cmd.add_argument("--cell", type=int, nargs=2, default=(700, 420), metavar=("W", "H"))
    sheets_cmd.add_argument(
        "--invert",
        action="store_true",
        help="the panel is LIGHTER than the device around it, which is any backlit LCD",
    )

    sub.add_parser("selftest", help="prove the whole path on a generated fixture")

    args = parser.parse_args()
    if args.cmd == "extract":
        extract(args.video, args.out, args.start, args.duration, args.fps)
        return 0
    if args.cmd == "sheets":
        build_sheets(
            args.frames, args.out, args.step, args.per_sheet, args.cols, tuple(args.cell),
            invert=args.invert,
        )
        return 0
    if args.cmd == "selftest":
        return selftest()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
