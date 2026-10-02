---
name: video-to-readings
description: Use when a device's display was filmed and every value on it has to be read back, especially a meter, gauge or appliance scrolled through its stored history. Covers seven-segment and LCD panels, handheld footage, and any long series where the answer is only useful if NO value was missed. Triggers on read a meter from video, values off a display, scrolled through the readings, stored daily values, seven-segment, LCD readout, frames from a video of a screen, and on a series that has to be proven complete rather than mostly right.
license: Apache-2.0
compatibility: Requires Python 3.9+ with Pillow 10.1+, and ffmpeg on PATH. ffprobe is used when present, to catch a start time past the end of the clip before ffmpeg is called. The selftest additionally needs an ffmpeg with libx264, because it builds its own fixtures; the skill itself does not. No numpy, no OpenCV, no OCR engine, no network access. Agent-agnostic; the reading itself is done by whatever model runs the skill.
metadata:
  version: "1.0.1"
  changeSummary: Frontmatter only. metadata values are strings under the Agent Skills spec, so isBreaking is now "false" rather than a YAML boolean. The skill itself is unchanged.
  isBreaking: "false"
---

# Video to readings

## The problem, and why the obvious tools miss it

Somebody filmed a display while scrolling through its stored values. The footage holds every number. Getting them back out is not OCR and not video summarisation, and the tools for both fail here in the same way.

![A three-by-three contact sheet. Each tile shows the same seven-segment panel carrying an index from minus one to minus nine and an invented value between 7.9 and 19.4 kWh, with its own timestamp stamped in yellow on black in the top-left corner. The fifth tile is dimmed, standing for a frame the picker rejects as unreadable.](../../docs/assets/scrolling-display-contact-sheet.png)

The picture shows the SHAPE of what this skill produces: the sharpest readable frame per time window, cropped to the panel, stamped with its own timestamp, tiled so a model reads a sheetful in one look instead of one image per frame. It is drawn, not captured, so read it as a diagram: nine tiles where a real sheet holds sixteen to twenty, and a flat synthetic panel where a real one carries noise and reflection. Measured, because the difference matters: the drawn tiles have a histogram spread of 7 to 48 levels and the picker's floor is 60, so the illustration's own tiles would be rejected by the scorer that made the layout. Real footage clears it comfortably; a flat render does not, and adding noise up to sigma 24 only took it to 51. Regenerate it with [`scripts/make_example_images.py`](scripts/make_example_images.py); everything in it is invented. If you copied this folder out on its own the image above will not resolve, because it deliberately lives in the repository's `docs/assets/` and not here: run that script and it draws the picture for you, into your working directory. It lives in the repository's `docs/assets/` rather than in this folder, because a skill folder that bundles a binary is reported HIGH by a security scanner that cannot read it.

**The measurement that decides the approach.** Every general video skill selects frames by scene change. A camera held on a display is a near-static scene: only a few segments change between one reading and the next. Run ffmpeg's own detector over that shape of footage and the scores come back between `0.000000` and `0.013281`, with **0 of 60 frames above 0.3**, against ffmpeg's own documentation calling 0.3 to 0.5 "generally a sane choice" for a cut. A digit flip scores roughly 23x below the bottom of the sane range, so `select='gt(scene,0.3)'` selects **no frame at all**.

Not one frame, none: the first frame is discarded along with the rest, because `scene` has no predecessor to diff against there and evaluates to exactly `0.000000`, which no `gt` threshold passes. That last step is the one worth checking rather than assuming, so it was checked against a control. On a generated clip with two real cuts the command emits exactly two frames, at a peak score of `1.000000`. On a generated static-display clip it emits zero, at a peak of `0.038682`, which is three times the real footage above and still an order of magnitude below the threshold. ffmpeg then exits `0` and says `Output file is empty, nothing was encoded(check -ss / -t / -frames parameters if used)`, which sends the reader to the extraction window rather than to the selector that actually refused everything. That misdirection is the cost: the tool reports success, the message blames the wrong parameter, and the obvious next move is to widen a time range that was never the problem.

## Overview

Three principles carry the skill.

1. **Sample at the camera's own frame rate, and only then decide what to keep.** A "good enough" rate is the mistake that costs most. On one long scroll, 10 fps left about a third of the values with no readable frame at all and produced the conclusion that the footage did not contain them; the same clip at its native 30 fps contained every one. A hand scrolling a display peaks around three values per second, which leaves three or four frames per value at 10 fps, and motion blur eats most of those.
2. **A score you compare across frames must be measured on the same number of pixels.** The panel moves and changes size in handheld footage. Measured on identical synthetic content at three sizes, an edge score taken on the raw crop runs 22.2, 14.8, 11.1 as the panel grows, so "the sharpest frame" quietly becomes "the frame whose panel was smallest". Normalise the crop to a fixed size first and the same three score 25.6, 25.4, 26.4.
3. **Completeness is the deliverable, and no timestamp proves it.** A contact sheet gives you order inside one sheet. Whether you have every value is a separate claim that has to be established separately, and the method for that is the last section below. Of the two halves of this job, getting the values out and showing that none is missing, every adjacent tool does the first and none does the second.

## When to use

- A meter, gauge, thermostat, scale or appliance was filmed while somebody paged through its stored readings, and you need all of them.
- A display was filmed once and a single value has to be read reliably, with the doubtful digits identified rather than guessed.
- Any video of a screen where the subject changes and the camera does not.

**Not for:** a live camera feed (build a monitor, not this), a document scan (use an OCR skill), or narrative video where you want a summary rather than values. If the display is clean, evenly lit, and you want a deterministic non-model read of single digits, [`ssocr`](https://github.com/auerswal/ssocr) is the better tool; shell out to it per still. It is GPL-3.0. Calling the binary from a separate process is fine. Vendoring its source into this skill is not, and the reason is not that GPL-3.0 forbids copying, which it expressly permits: it is that the combined work would then have to be GPL-3.0, and this skill is Apache-2.0.

## Toolchain

| Job | Tool | Note |
|---|---|---|
| Frames at the camera's rate, plus a manifest | `read_display.py extract` | the manifest pins every frame to a real timestamp, so a label cannot drift from its frame |
| Find the panel in a frame | `read_display.py`, Pillow only | a BOX-filtered resize to one pixel wide IS the per-row mean; no array library is needed. `--invert` for a backlit panel |
| Reject a frame with no picture | luma gate, 25 to 235 mean, 60 levels of spread | a dark frame has almost no edges and therefore reads as sharp |
| Sharpest frame per window, tiled and labelled | `read_display.py sheets` | 16 to 20 cells per sheet keeps small digits legible |
| Read the digits | the model, off the sheets | no OCR engine is involved, and none is needed |

**Where ffmpeg alone is enough, use ffmpeg.** `tile` is a real contact-sheet generator, `select='isnan(prev_selected_t)+gte(t-prev_selected_t\,2)'` does interval sampling, `thumbnail=N` picks a representative frame per batch, and `mpdecimate` drops near-duplicates. One command, no script:

```bash
ffmpeg -i clip.mp4 -vf "thumbnail=10,scale=320:-1,tile=5x4:margin=6:padding=4:color=white" \
  -fps_mode passthrough sheet_%03d.png
```

This skill does the tiling in Pillow anyway, for one reason: per-cell timestamps need the `drawtext` filter, and `drawtext` needs a freetype build. A stock Homebrew ffmpeg 9.0.2 answers `No such filter: 'drawtext'`, and you cannot tell from `ffmpeg -version` whether a given install has it. Without labels a grid carries order inside one sheet and nothing across sheets, and the whole output is an ordered series. Pillow is already required for the panel detector, so tiling there costs nothing and removes the dependency on an optional build flag. One caveat on that one-liner, because it is offered as a substitute and is only half of one: `thumbnail=N` picks the most representative frame of each batch, not the sharpest, so on handheld footage it will hand you blurred frames with no way to tell. It is the right tool for a quick look and the wrong one for a series you intend to rely on. If your ffmpeg has `drawtext`, the one-liner above plus `drawtext=text='%{pts\:hms}'` is a fine substitute, and it has to sit before `tile`, not after it. The sampling filters are not the problem: measured with `showinfo`, `thumbnail=10` reports `0.1, 0.467, 0.733, 1.0` and `select` reports the real times of the frames it keeps, so both preserve the timestamp. `tile` is what destroys it, because it collapses N frames into one output frame and keeps only the first one's time. Twelve selected frames tiled three by two came out as two timestamps for twelve cells.

## The method

**Before step 1.** Install the two dependencies: `python3 -m pip install 'Pillow>=10.1'`, and ffmpeg from your package manager (`brew install ffmpeg`, `apt install ffmpeg`, `winget install ffmpeg`). Every command below is written relative to **this skill folder**, so either run them from here and pass absolute paths to your video and output directories, or put the full path to `read_display.py` in front. Nothing is installed globally and nothing writes outside the directories you name.

1. **Film it properly, if the filming has not happened yet.** Hold still, fill the frame with the panel, and scroll at a steady pace with a visible pause on each value. Light it from the side; a direct reflection on an LCD costs more frames than a shaky hand.
2. **Find the camera's real frame rate** with `ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 clip.mp4`, and extract at exactly that. Do not round it down for speed.
   ```bash
   # --start and --duration are YOUR clip's numbers, not these. Omit both to take the whole video.
   python3 scripts/read_display.py extract clip.mp4 frames/ --fps 30 --start 12 --duration 90
   ```
3. **Measure the fastest stretch before choosing a window.** Step through a handful of frames where the scrolling looks quickest and count how long one value stays on screen. Set `--step` to at most half that. Too coarse silently drops values; too fine only costs sheets.
   ```bash
   python3 scripts/read_display.py sheets frames/ sheets/ --step 0.3 --per-sheet 20 --cols 4
   ```
   Add `--invert` if the panel is lighter than the device around it, which is any backlit display. Without it the detector finds nothing at all, and it will tell you so.
4. **Read the coverage report before reading a single digit.** `sheets` prints it and also writes it to `sheets.json` beside the images, where `rejected_no_panel`, `rejected_no_picture` and `empty_windows` are the three fields that matter. A window in `empty_windows` is a stretch of the recording you have not read, it is printed as a `WARNING` line, and it is the only warning you will get.
5. **Read the sheets in order and write down BOTH the index and the value.** Almost every instrument numbers its stored values, and that number is the spine of the whole result. A value without its index cannot be placed and cannot be checked.
6. **Zoom, do not guess.** Where a digit is ambiguous, re-extract that one window at a finer step and render fewer, larger tiles. One uncertain value is worth a second pass; a guessed digit contaminates a total that will later be used as evidence.
7. **Prove the series is complete**, with the two checks in the next section. Only then report a number.

## Proving you read all of it

This is the part that distinguishes a result from an impression, and the part no adjacent tool does.

- **The index sequence must be contiguous.** Sort what you read by the instrument's own index and look for holes. A hole is a value you never saw, and no timestamp, frame count or sheet count reveals it; the sheets look complete either way. If the display numbers its entries, this check is free and it is the strongest evidence you will get.
- **Cross-check the total against an independent anchor.** Most instruments show a running total alongside their history. Take the total, subtract every value you read, and compare the result against a reading taken on a different day, from a photograph or a record. Two independent anchors agreeing to within one period's worth means the series is right; disagreement by one period's worth means exactly one value is missing or misread, and the size of the gap usually says which.
- **State what you did not measure.** A scroll that stopped short covers part of the range and not the rest. Name the range you actually read, in the instrument's own index, rather than implying the whole history.
- **Know what the two checks above do NOT catch.** They are strong against a missing value and weak against a misread one. A contiguous index sequence stays contiguous when a digit is read wrong, and the total cross-check only notices if the error is bigger than the slack in your anchor. So a clean report is not a guarantee: before anything you read here is used as evidence, re-read the values that matter most from the sheets a second time, and say in your result which values were ambiguous and by how much.
- **Keep the raw series, in a format the checks above can be run on.** One line per entry, as you read it, not at the end: the instrument's index, the value, and the sheet and cell it came from, so a disputed digit is one lookup rather than a re-read.

  ```csv
  index,value,sheet,cell
  -1,12.7,sheet_001.jpg,1
  -2,8.3,sheet_001.jpg,2
  ```

  The contiguity check is then a sort on the first column, and re-reading sheets to recover a number you already had is the most expensive mistake available here.

**One more thing before you point a camera at a display.** A meter, a scale or a thermostat records what somebody did in their home, and the reading is about that person even though no name appears on it. Film your own device, or one you have been asked to read. Where the values leave your machine, send the series you need and not the footage: the frames carry the room behind the panel.

## Gotchas

| Symptom | Cause / fix |
|---|---|
| Scene detection produced an empty output directory, and ffmpeg said nothing was wrong | The camera is static and only the subject changes, so every frame scores below any sane cut threshold and the selector passes none of them. ffmpeg exits `0` and blames `-ss / -t / -frames`, which is not the cause. Use interval or per-frame selection; a cut detector has nothing to detect. |
| A third of the values have no readable frame | Extracted below the camera's rate. Re-extract the disputed stretch at the native fps before concluding the video does not hold them. |
| "Sharpest" tiles are consistently the ones where the panel looks small | The score was taken on the raw crop, so it is resolution dependent. Normalise to a fixed size before scoring. |
| A black or washed-out frame wins its window | An edge score reads a frame with no picture as sharp. Gate on mean luma and on histogram spread before ranking. |
| Every label is off by a constant | The frame index to time mapping was hardcoded instead of read from the extraction parameters. That failure is silent; the labels just lie. |
| The coverage report says every window produced a tile, and values are still missing | The window count was rounded to nearest rather than truncated and incremented, so the tail of the recording falls outside every window. Those frames are neither tiled nor counted as unread, which is the one failure a completeness report must never have. Truncate and add one. |
| The detector reports a panel on a picture of a wall | A dark-region bounding box with no sanity check returns the whole frame. Reject a box that fills the frame or that is not meaningfully darker than its surroundings. |
| Your own files vanished from the output directory | Both commands clear `f_*.jpg` and `sheet_*.jpg` from the directory you name, because a stale frame from an earlier run would otherwise be read as part of this one. Point them at a directory of their own. A missing or unreadable input deletes nothing. |
| The panel box is far too wide, and the tiles show half the device | The box is the extreme of every qualifying row and column, with no contiguity requirement, so one dark object elsewhere in the frame stretches it. Crop the video to the panel before extracting, or move the object. |
| `No frame carried a readable panel` | The panel is LIGHTER than the body around it, which is every backlit LCD: a thermostat, a scale, a fridge, most smart meters. The detector looks for a dark region, so a perfectly framed clip reports nothing. Re-run `sheets` with `--invert`. The command probes for this itself and says so rather than blaming your framing. |
| `No such filter: 'drawtext'` | ffmpeg built without freetype. Label in Pillow, or install a full build. |
| Labels render tiny and unreadable | `ImageFont.load_default()` with no size argument gives you 10 px, whatever else changes: a bitmap font on Pillow below 10.1, a 10 px scalable font on 10.1 and later. Pass a size, which needs Pillow 10.1+. |
| Tiles are stretched by different amounts | The crop is forced to a fixed aspect. Either pad to the aspect or accept it, but do not compare sharpness after the stretch. |
| Frames you can read yourself are rejected as having no picture | `SPREAD_MIN` wants 60 levels between the 10th and 90th percentile of the panel, and **sparse** content does not reach it: lit segments cover too few pixels, so the 90th percentile still sits in the background. Measured on nine flat synthetic tiles, spread 7 to 48, all rejected, and adding sensor noise up to sigma 24 only reached 51. `--invert` does NOT rescue them: the inverted frame has a bright panel on a dark body, so the detector finds no panel at all. Real photographed footage clears the floor comfortably, which is why the constant is set where it is. If yours does not, lower `SPREAD_MIN` deliberately and know what you are trading: it is the check that stops a frame with no picture from winning its window. |
| The digits are legible alone but not on the sheet | Too many cells. Sixteen to twenty cells keeps small digits readable; sixty-four does not. |

## Example

[`scripts/read_display.py`](scripts/read_display.py) is the working implementation, and it is also its own proof:

```bash
python3 scripts/read_display.py selftest
```

That builds four fixture videos with ffmpeg alone, runs the whole path over them, and asserts twenty-seven things: frame count, that the panel is found in every frame and within 4 px of where the fixture drew it, that a near-black and a flat-grey crop both score `None`, that a crop with ample contrast but a mean outside the luma bounds is rejected at both ends, that a crop sitting exactly on the spread bound is accepted rather than rejected by one level, that every one of three panel sizes produces a score at all and that the score varies less than 10% across them, that the tile picked per window is the one unblurred frame sitting in the MIDDLE of that window, that a deliberately hidden second is reported as an unread window rather than dropped, that no frame falls outside every window, checked at three starts including one where the division is inexact, that a start time past the end of the clip is named as such instead of surfacing an ffmpeg errno, that a missing input file deletes nothing in the output directory, that a bright panel is invisible to the default detector and recovered in full by `--invert`, and that a video with no panel at all yields no detections.

Three of those fixtures exist because the obvious version of the test passes over a broken implementation. With the sharp frame first in its window, "pick the sharpest" and "pick the first" agree, so the assertion says nothing about sharpness. Without a gap, nothing tests the completeness warning. And a flat crop is rejected by the spread test whatever the luma gate does, so removing the gate left the suite green until a crop with real contrast and a bad mean was added.

Twelve deliberate mutations were run against the suite and eleven of the twelve turn it red: rounding the window count to nearest instead of truncating and adding one, removing the luma gate, removing the spread gate, taking the first frame instead of the sharpest, scoring on the raw crop, dropping the unread-window report, reverting the percentile walk to a zero sentinel, removing the panel sanity check, making `--invert` do nothing, deleting the start-time check, deleting the input-exists check, which puts the data loss straight back, and removing the loop that grows the window count until it covers the last frame.

The twelfth is the interesting one, because it is the mutation that STOPPED being a defect. Replacing the initial window estimate with a round-to-nearest no longer turns the suite red, and should not: the growth loop makes the starting estimate irrelevant, since it raises the count until coverage is proven either way. A mutant that survives because the code got stronger is a different thing from one that survives because the test is weak, and the two are only distinguishable by looking.

Two of them are worth knowing about individually. The panel sanity check fails by raising `ZeroDivisionError` rather than by failing an assertion, because a box that fills the frame makes the area fraction exactly 1.0, so that check is load-bearing for the arithmetic below it and not only for plausibility. And the start-time mutant **escaped on the first run**, because the assertion matched the substring "past the end of this", which a harmless warning on a different code path also prints. Two paths, one substring, and the mutant walked through. The assertion now matches the fatal wording alone. That is the value of a battery over a single falsification: it tests the test.

## Prior art (checked 2026-09)

**[`andrewii23/ii23-skills`](https://github.com/andrewii23/ii23-skills) (`video-understand`, MIT) is better than this skill at frame extraction, grid packing and deduplication, and this skill takes from it rather than competing.** Its ADR 0001 measures what a grid is worth: 64 frames as separate images cost about 12,500 tokens and the same 64 as one grid about 1,900, with a cells-to-legibility table that says 16 cells for small text. Its ADR 0008 documents the dark-frame trap, and both gates used here are taken from it with thanks (MIT, Copyright (c) 2026 warodom (andrewii23); no code was copied): the luma bounds and the histogram-spread floor together, not one of the two. Its `frames.py` also solves timestamp labels without freetype, by writing small bitmap glyphs and overlaying them, which is a better answer than this skill's if you want to stay inside ffmpeg. Where it stops for this problem: its coverage guarantee is one frame per shot and shots come from scene scores, which a static camera never produces, and it does not read values or check that a series is complete, which it does not claim to.

**[`auerswal/ssocr`](https://github.com/auerswal/ssocr) (GPL-3.0-or-later, maintained since 2004)** is the reference deterministic seven-segment reader and the right tool when the display is clean and you want no model in the loop. It reads exactly one image per invocation and does not decode video, so the video half stays yours.

**[`suyashkumar/seven-segment-ocr`](https://github.com/suyashkumar/seven-segment-ocr)** is the closest by problem statement and is not usable: it is Python 2, it opens an OpenCV window and blocks until a human drags a box around each digit, those boxes are taken once from the first frame and reused for the whole video, and an unresolved digit triggers an interactive `input()` prompt. **It carries no licence file at all**, so it cannot be vendored regardless.

Also looked at and further away: `OICWS/lcd-digit-recognition` (AGPL-3.0, images only), `skaringa/emeocv` (GPL-3.0-or-later, a fixed-mount monitoring daemon), `jomjol/AI-on-the-edge-device` (ESP32-CAM firmware with its own camera, and **not open source**: its terms are a custom Dual Use License whose section 1.2 prohibits any commercial use without a separate agreement with the author, so do not lift code or a design from it into anything paid. Its file is named `Licence.md`, which is why GitHub reports the repository as `NOASSERTION` and a `LICENSE` fetch returns 404), and several general frame-extraction skills whose default selector is scene detection at 0.3. The skill registries returned nothing for seven-segment or meter reading; their OCR entries are all document and PDF skills.

## Status

This skill grew out of one long scroll on a utility meter, and was then rebuilt for a stranger: the session path, the platform-specific font and the undocumented extraction command all became arguments, and numpy was removed after checking the array-free detector returns byte-identical boxes. The selftest is the regression suite and it is mutation-tested, so a change that breaks the method turns it red rather than passing quietly. What is not covered by the fixture: real motion blur and reflections. A panel lighter than its surroundings, which is every backlit display, is covered by `--invert` and by a fixture of its own; the detector probes for that case and names it rather than reporting bad framing.
