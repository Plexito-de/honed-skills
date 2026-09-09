---
name: human-review
description: Put a draft in front of the person you are working for and get their markup back in one batch: they retype text, comment on any selection, and circle or scribble on images and PDF pages with a note per mark. Use before anything they will read, print or publish: post, spec, plan, report, newsletter, landing page, deck, invitation, poster, chart, rendered design, screenshot.
license: Apache-2.0
---

# human-review

An agent that asks "does this look right?" in chat gets "looks good" back. Prose about a draft is not
the draft. This skill puts the draft itself in a browser. The reviewer edits the words, selects any
phrase and comments on it, and circles the part of the image that is wrong. One Send button returns
the whole batch.

Text and pictures sit on the same page on purpose. A draft is rarely only prose or only an image. An
invitation is a rendered card and its copy. A post is text and its hero image. Two surfaces split the
reviewer's attention. They also cost a round every time an item crosses the boundary.

## The loop

```sh
skills/human-review/bin/review open draft.md card.png form.pdf   # opens the page, returns at once
skills/human-review/bin/review poll --session <id>               # blocks until they hit Send
skills/human-review/bin/review poll --ack                        # clears the batch you handled
skills/human-review/bin/review status                            # is a batch waiting
skills/human-review/bin/review close                             # stop the server
```

1. Write or render the draft.
2. Open it with `open`. The command prints a session id and a `127.0.0.1` URL. It also copies the URL
   to the clipboard and opens a browser. Nothing leaves the machine. The server is local, the page is
   local, and no account is involved.
3. Wait for `poll`. It blocks until Send, then prints the batch as JSON. **Do not busy-wait, and do
   not add a timeout loop.** Some harnesses can run a command in the background and wake you when it
   exits. Use that, and end your turn. Otherwise keep the foreground poll alive inside the turn.
4. Apply the batch, render again, and offer the next round on the new file. Two or three tight rounds
   beat one long round.

## What the reviewer can do

| On | How | What you get back |
| --- | --- | --- |
| An image or a PDF page | circle, box, freehand or arrow, one note per mark | the picture with the marks rendered into it, plus one padded crop per mark |
| A Markdown, HTML or text draft | select any text, click Comment, type the note | the quote with a prefix and suffix anchor, and the block it sits in |
| The same draft | type straight into it | a before and after per block |

Every item lands in one numbered list. Their "2" is your "2", on a picture and in a paragraph alike.

## The batch

```json
{ "status": "feedback", "session": "0281f317ca76",
  "pages": [
    { "kind": "media", "name": "card.png", "source": "/abs/card.png",
      "overview": "/…/overview_card.png",
      "marks": [ { "number": 1, "tool": "ellipse", "note": "head smaller",
                   "box": {"x":0.30,"y":0.05,"w":0.40,"h":0.17},
                   "crop": "/…/crops/card_mark_01.png" } ] },
    { "kind": "text", "name": "draft.md", "source": "/abs/draft.md", "edits_saved": false,
      "comments": [ { "number": 2, "quote": "…", "anchor": {"prefix":"…","suffix":"…"},
                      "block": "draft#4", "note": "cut this" } ],
      "edits": [ { "block": "draft#2", "label": "h2", "before": "…", "after": "…" } ] } ],
  "overall_note": "the rest is fine" }
```

### Rules for reading it

- **Open every `crop`.** This is the point of the media path, and the step you are most likely to
  skip. A box at `0.30, 0.05` tells you nothing. The crop shows you the eyebrow, the kerning, the
  seam you got wrong. Look at the images if your model can see them. Say so rather than guess from
  coordinates if it cannot.
- **Open the `overview` first.** It shows what they saw while they wrote the notes.
- **`source` is the original file.** Edit that file. The copy in the session directory is a working
  copy, and a rewrite of it changes nothing.
- **`edits_saved` is always false.** This tool never writes to their files. Apply `after`
  **verbatim**, and keep the syntax of the source. An edit made in a rendered Markdown block goes
  back into the Markdown, not as HTML.
- **An edit is their wording, not a suggestion.** Never revert it, and never improve it. Say so and
  show the evidence if it introduces an error of fact. The wording is still theirs to decide.
- **A mark with an empty note means "look here".** Ask what is wrong. Do not invent a reason.
- **Answer in your own channel, never in the tool.** The page has no chat. They see the result when
  you render again.
- Keep their numbering when you report back: "1 done, 2 done, 3 needs a decision".

`marks.md` in the session directory carries the same batch as plain text. `receipt.json` records the
files and the time. A later gate can then treat "a human looked at this" as a fact.

## Triage before you implement

A batch is not a task list. Sort every item into one of three buckets, and say which one, before you
touch anything:

- **do it now**,
- **defer**, and write the item down somewhere durable in the same turn,
- **reject**, with the reason in one line.

Two failure modes look like diligence:

- **You implement a comment you do not understand.** Ask. One question now is cheaper than a round on
  the wrong fix.
- **You drop the awkward item in silence.** Say that you disagree, then do it anyway. The one
  exception is an item that is wrong on the facts.

## Where it fits

Run the machine checks first: types, tests, linters, and any automated review you have. Then look at
the rendered draft yourself, with your own eyes. Only then spend a human's attention. Their time is
the most expensive input in the loop. Do not spend it on a defect a tool can find.

## Requirements and limits

- **Python 3.9 or newer, standard library only.** No pip, no npm, no Node. It works offline.
- The browser renders the marked-up overview and cuts the crops, so the tool needs no image library.
- The tool renders a PDF to page images with `pdftoppm` (poppler), or with `sips` on macOS. `sips`
  handles the first page only. Without either program, the tool skips PDFs and prints a message.
- The Markdown renderer is a deliberate **subset**: headings, lists, quotes, code, tables, links,
  emphasis. Anything else stays a paragraph, and nothing disappears. Full CommonMark needs a
  dependency, and a draft review does not need it.
- The tool reviews **files**. A page from a development server is out of scope. Screenshot it, or
  review the template behind it.
- The tool does not support video. Export a frame.
- Sessions live in `~/.claude/.review/<id>/`. Delete the directory to discard one.

## Credit

Two ideas come from [human-review](https://github.com/petergyang/human-review) by Peter Yang (MIT).
The first anchors a selection by prefix, quote and suffix rather than by offset. The second reports
an edit as a before and after on one block, instead of a diff of the whole document. The
implementation here is independent, and the two projects share no code.
