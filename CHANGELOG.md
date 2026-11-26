# Changelog

Notable changes to the skills in this repository. Each skill also carries its own version in
its `SKILL.md` frontmatter. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Contribution guide, issue forms, pull request template, and a CLA check for first-time
  contributors.
- `.github/scripts/check_skills.py`: a CI check for the rules a script can check.
- `screenshot-overlays`: aligns two screenshots of one screen and merges the hover tooltip or
  popover of one into the other, cut along its own border.
- `blockkit-forge`: Block Kit messages a bot builds from data it did not write: safe text from other
  systems, a dry-post as the bot that fails closed, scannable cards, tags added by an edit, live
  cards and thread timelines, the live enums from `blocks.validate`, and an example card.
- `screenshot-callouts`: arrows, boxes, numbered steps, callouts, highlights and solid redactions
  on a screenshot from a JSON spec, with measured targets, collision-free label placement, a
  rough.js sketch style, Excalidraw export and a read-back proof of every written file.

### Changed

- `human-review` 1.6.0: video review. Open an `.mp4`, `.m4v`, `.mov` or `.webm`, draw
  on a paused frame, note a moment or a span, and get back each moment's frame with the marks drawn
  in, a crop per mark, and the time in seconds and as a timecode. ffmpeg converts a file that not
  every browser plays, once per render. The server now answers byte ranges and several requests at
  once, so a video download cannot hold up Send.
- `excalidraw` 1.2.0: lint rules for arrows that cross, overlap or run through another arrow's
  label, ranked by the evidence (Purchase 1997: crossings hurt reading most). C4 element types,
  technologies and per-edge source provenance in the spec, with matching lint notes.
  `exfidelity.py` proves every label and edge of a Mermaid flowchart survives into the drawing.
  Repeated arrows between two boxes get their own lanes. Ids are now hashed from the parsed spec,
  so every existing diagram gets new ids and seeds once on its next build.

## 2026-10-04

### Added

- `din5008-letter`: a German business letter laid out to DIN 5008 Form B, as a print-ready PDF.

## 2026-10-02

### Added

- `qr-code`: QR codes and EPC payment codes, proven by decoding the finished image.

## 2026-09-29

### Added

- `video-to-readings`: every value read back from a filmed display.

## 2026-09-22

### Added

- `drive-audit`: classify and revoke Google Drive sharing, against a policy file.

## 2026-09-10

### Added

- `excalidraw`: hand-drawn diagrams from a short spec, with measured font widths.

## 2026-09-09

### Added

- `human-review`: a local page to mark up drafts, images and PDF pages in one batch.

## 2026-09-08

### Added

- `comb-pdf-form-filling`: fill comb fields one character per box.
