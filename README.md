# honed-skills

[Agent Skills](https://agentskills.io/specification), honed against real work rather than written from imagination. Each skill here was extracted after doing the task for real, and it keeps the parts that only show up once you have hit them: the failure that looked like success, the flag that lied, the check that has to happen before you can claim the job is done.

The Agent Skills format is an open, cross-agent spec, so nothing here is tied to one product. A skill is a folder with a `SKILL.md`; the agent reads the `description`, decides the task matches, and loads the rest. Everything below is plain Python and command-line tools.

## Skills

| Skill | Use it when |
| --- | --- |
| [`human-review`](skills/human-review/) | A draft has to go in front of the person you are working for before it ships, and "does this look right?" in chat gets "looks good" back. They retype the text, comment on any selection, and circle the part of an image or PDF page that is wrong, with a note per mark. A PDF stays a PDF: its pages are drawn at the pixel ratio of the screen and its words can be selected, found with Ctrl+F and commented on, and every crop is re-rendered from the file rather than cut from a raster. One local page for text and pictures together, one Send, one batch back. The server is Python standard library only; the only third-party code is a pinned, checksummed copy of PDF.js served from `127.0.0.1`. No npm, no network, works offline. |
| [`comb-pdf-form-filling`](skills/comb-pdf-form-filling/) | An official PDF form has to be filled so that each character lands inside its own printed box (a comb field): IBAN, tax number, BIC, dates. Built against German Behörden, tax and bank forms, and applies to any AcroForm or flat PDF with printed boxes. |
| [`drive-audit`](skills/drive-audit/) | Something in Google Drive is shared and you cannot tell what. Exposure in Drive lives on the ITEM, so a folder can report itself private while every file inside it is world-readable, and Drive has no view that shows you otherwise at any scale. Classifies every non-owner permission worst-first, diffs the result against a policy file where each approved share carries a reason and a date, and revokes or downgrades with a restorable snapshot written before the first deletion. Dry run until `--apply`. The classifier and its 21 offline cases run with no credentials, so you can check the logic before granting it access. |
| [`excalidraw`](skills/excalidraw/) | A diagram has to be produced as a `.excalidraw` file and look hand-drawn: architecture, flow, sequence, tree, mind map, timeline, network. Writes a short spec and expands it deterministically, with font widths measured in a browser against the real fonts rather than guessed from a multiplier, and a linter that runs before you look. |
| [`video-to-readings`](skills/video-to-readings/) | Somebody filmed a meter, gauge or appliance while scrolling through its stored values, and every one of them has to come back out. The general video tools fail here for one measurable reason: a camera held on a display is a near-static scene, so scene-change frame selection passes no frame at all and ffmpeg still exits `0`, blaming the wrong parameter. Samples at the rate of the camera, scores sharpness in a way a dark frame cannot win, tiles the sharpest frame per window into labelled contact sheets, and reports every second it could not read, because a series is only useful if you can show nothing was missed. Pillow and ffmpeg, no OCR engine, and a mutation-tested selftest that builds its own fixtures. |
| [`qr-code`](skills/qr-code/) | A QR code has to scan the first time: a URL on a printed card with a logo in the middle, or an invoice paid through the EPC QR code a banking app reads (GiroCode in Germany). The usual generators paste the logo over live modules, and few read the file back. This one clears the modules behind the logo and sizes that zone in modules. A PNG counts as written only when an independent decoder has read the finished card back to exactly the input, also shrunk to phone-camera size and blurred. For a payment, every rule refuses rather than repairs: an IBAN that fails its checksum, a payee name the script would have to shorten, a hidden character, a word that mixes Latin and Cyrillic letters. It also goes the other way: `cutout` lifts the logo back out of a picture of an old card, for when that picture is the only copy left. segno, zxing-cpp and Pillow, pinned, no network, no account. |
| [`din5008-letter`](skills/din5008-letter/) | A German letter has to be printed and posted in a DIN lang window envelope, laid out to DIN 5008 Form B. Only the sender and the recipient are required; every other part you give (Rücksendeangabe, Einschreiben, the information block with references and date, subject, salutation, text, closing, signer, enclosures) starts on its line of the norm's 4,23 mm grid, with fold and hole marks on every page. An address the window cannot take, seven lines or one line wider than 80 mm, is refused rather than shrunk, and so is a control or bidi character that would print other than it reads. The PDF is kept only when the start of every part matches constants taken from the norm rather than from the template (plus a few marked template choices), the page breaks keep two lines of the text with the closing, no glyph is an empty box, and pdftotext finds every drawn word inside its area. Typst, poppler and the Python standard library, no network. |

**`human-review`.** One local page holds the card and the draft. Two marks sit on the picture, a third comments on a selected phrase, the retyped headline comes back as a before and after, and the numbered list on the right is what the agent receives:

![The review page: a launch card with a circled date badge numbered 1 and an arrow numbered 2, the draft below it with a highlighted edited heading, and a sidebar listing all four items with a typed note under each](docs/assets/review-page-marked-up.png)

**`comb-pdf-form-filling`.** A comb field prints one box per character, and filling it as an ordinary text field puts the string across the dividers. Same value, same field, both ways:

![A comb field filled wrongly as one continuous string, and filled correctly with one glyph centred per box](docs/assets/comb-field-wrong-vs-right.png)

**`drive-audit`.** The report is the output, so the honest illustration is the report itself:
`scripts/example_report.py` prints this from invented findings, with no Drive and no credentials, and
you can rerun it to see exactly what went in.

```
=== public_indexed: 1 item(s), 0 folder(s) -- PUBLIC and search-indexed (anyone can find it)
  !     Old proposal.pdf                        reader   anyone            0B_EXAMPLE_INDEXED
=== external_writer: 1 item(s), 1 folder(s) -- a person outside the account can EDIT it
  ! DIR Handover                                writer   contractor@elsewhere.example
=== approved by policy: 1 item(s) suppressed
        Filing 2024.xlsx    advisor@example.com    outside accountant (since 2026-05-26)

scanned 8 item(s); reached 7 item(s) carrying a non-owner permission; 5 unapproved share(s) set
directly, 1 inherited, 1 approved
```

**`video-to-readings`.** What the skill hands a model: the sharpest readable frame per time window,
cropped to the panel, each tile stamped with its own timestamp so the order survives across sheets.
The dimmed tile is a frame the picker rejects. Every digit below was drawn from seven-segment
geometry by a script that ships with the skill, so nothing real went into it:

![A three-by-three contact sheet. Each tile shows the same seven-segment panel carrying an index from minus one to minus nine and an invented value between 7.9 and 19.4 kWh, with its own timestamp stamped in yellow on black in the top-left corner. The fifth tile is dimmed, standing for a frame the picker rejects as unreadable.](docs/assets/scrolling-display-contact-sheet.png)

**`qr-code`.** A menu card as `qr.py text` writes it. The star is a logo the illustration script
draws, and the address is `example.com`. The modules behind the logo are cleared, not covered. The
file counts as written only when it decodes to exactly the address at full size, shrunk to 3 and
2 pixels per module, blurred, and blurred and shrunk together:

![A teal card with a QR code for https://example.com/menu on a white panel, a red disc logo with a white star in a cleared square in its center, and the caption "Scan for today's menu" below](docs/assets/qr-code-card-example.png)

**`din5008-letter`.** Page one of the selftest's full letter, as `letter.py` writes it. Every name
and address is invented. The return line, the note and the address sit in the window area from 45
mm, the information block starts at 125 mm on the Rücksendeangabe's grid line, and the three marks
on the left edge are the folds at 105 and 210 mm and the punch mark at 148,5 mm:

![Page one of a DIN 5008 Form B letter: the sender right-aligned in the header, a one-line return address and the note "Einschreiben" above a five-line recipient address on the left, an information block with Ihr Zeichen, Ihre Nachricht vom, E-Mail and Datum on the right, a bold subject line, salutation, two short paragraphs, the closing, a signature gap, the signer, an enclosure and a distribution note, and three short marks on the left edge for folding and punching](docs/assets/din5008-letter-example.png)

## Install

Copy the skill folder into your agent's skills directory. Name the destination explicitly:

```bash
git clone https://github.com/Plexito-de/honed-skills
mkdir -p ~/.claude/skills
cp -r honed-skills/skills/comb-pdf-form-filling ~/.claude/skills/comb-pdf-form-filling
```

Name the destination as above rather than ending the path at the parent. `cp -r src dest/` on a machine where `dest` does not exist yet copies the *contents* instead of the folder, so the skill lands as `~/.claude/skills/SKILL.md`, exits 0, prints nothing, and can never load. Verify with `ls ~/.claude/skills/comb-pdf-form-filling/SKILL.md`.

**Where the directory is** depends on your agent, and several agents share one:

| Agent | Personal skills directory |
| --- | --- |
| Claude Code, Claude | `~/.claude/skills/` |
| Codex, GitHub Copilot CLI, Gemini CLI | `~/.agents/skills/` (shared cross-runtime path; Gemini CLI also reads `~/.gemini/skills/`, and `~/.agents/skills/` wins when both exist) |
| Others | Check your agent's own docs. The [client showcase](https://agentskills.io/clients.md) lists the products that support the format, including Cursor, VS Code, OpenCode, OpenHands, Goose, Amp, Junie, Factory, Roo Code and Kiro. |

Most agents also read a project-level directory (commonly `.claude/skills/` or `.agents/skills/`) if you would rather commit a skill alongside a repo than install it per machine.

To update, `git pull` and copy again: a copied folder is a snapshot and does not track this repo. To uninstall, delete the folder you copied.

## What "honed" means here

A skill in this repo has to earn its lines:

- **Written after the work, not before.** The method is what actually produced a correct artifact, including the attempts that did not.
- **Verification is part of the method.** If a step can silently produce a wrong-looking-right result, the skill says how to look at the output and what to compare it against. In `comb-pdf-form-filling` that means rendering the finished PDF to an image and reading it, because a filled form can look correct in one viewer and broken in another, and a checkbox written to the wrong state reports success while granting nothing.
- **The gotchas are the payload.** A table of symptoms and causes beats a tidy description of the happy path, and it is the part you cannot get from an API reference.
- **Prior art is researched first and credited.** Where a public skill already covers ground, we read it at the source, take the ideas that hold up, say so, and state what ours adds. We do not copy code or text: a borrowed method you have not tested is worse than no method, and an unread dependency is a supply-chain risk.
- **No private data.** Examples use placeholder values only.

## Contributing

Issues and pull requests are welcome. A new skill needs a `SKILL.md` with `name` and `description` frontmatter, a gotchas section drawn from real use, placeholder-only examples, and a picture of the thing working where the output is something you can look at. The picture is generated from a script that ships with the skill, so anyone can regenerate it and see that nothing real went into it.

Two rules exist because a skills repo does not distribute documentation, it distributes instructions that land in someone else's agent context along with code that agent may run:

- **No `allowed-tools` declaration and no shell substitution in a skill body.** A skill must not pre-approve tools for itself or smuggle command execution into its text. If a skill needs a command run, it says so in prose and the human decides.
- **Scripts stay readable and dependency-light.** Anything under `scripts/` should be short enough to audit in one sitting, and its dependencies named in the frontmatter `compatibility` field.
- **Every skill here is scanned before it ships, and no finding is hidden.** We run [NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector) over each skill and read the JSON rather than the exit code, because `skillspector scan` exits `0` for anything scoring 50 or less and a skill can carry a HIGH finding at 31. Whatever survives is either fixed or written down: a skill that keeps a finding ships a `.skillspector-baseline.yaml` giving the reason per rule and naming the alternative we rejected. That file changes nothing about your own scan, by design, since the tool ignores a baseline the author ships unless you ask for it with `--use-shipped-baseline`. It is there so you can see the decision and disagree with it. Two consequences you can check: **binary assets live outside the skill folder** (`docs/assets/`, referenced by absolute URL) because a bundled image the scanner cannot read is reported HIGH at fixed confidence, twice over in our case; and where a skill cannot reach `SAFE` we say why rather than suppressing the reason.

## License

[Apache-2.0](LICENSE). Each skill folder carries a copy, so the licence travels with it when you copy the folder out.
