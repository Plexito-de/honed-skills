# What makes a hand-drawn diagram read well, measured

Numbers here come from two corpora, counted rather than remembered:

- **20 real published `.excalidraw` scenes**, about 1,700 elements, from `openfga`, `ovn-kubernetes`,
  `kubesphere/kubekey`, `k8s-tutorials`, `kubernetes/enhancements`, `vercel/swr-site`,
  `grafana/grafana-app-sdk`, `cloudflare/quiche`, `wasmCloud`, `chakra-ui/panda`,
  `RedHatInsights/config-manager`, `ardanlabs/service`, `DataDog/go-profiler-notes`, and five
  data-platform scenes.
- **All 232 community libraries** from `libraries.excalidraw.com`, about 55,000 elements,
  1,672,104 total downloads.

Where a number here contradicts a popular skill, the measurement is stated and so is the contradiction.

## Related context

- [`../SKILL.md`](../SKILL.md) - the spec, the workflow, the gotchas.
- [`element-schema.md`](element-schema.md) - the format reference behind the geometry.

## 1. The discipline table, which is the whole finding

Per scene, across the 20:

| Property | Result |
|---|---|
| Distinct **stroke colours** on shapes | median **1.5**; **10 of 20 use exactly one** |
| Distinct **background** colours | median 3.5; the two worst-looking scenes had 13 and 15 |
| Distinct **fill styles** | **17 of 20 use exactly one** |
| Distinct **stroke widths** | **16 of 20 use exactly one** (1 or 2) |
| Distinct **roughness** values | **20 of 20 use exactly one.** 17 use `1`, two use `0`, one uses `2` |
| Distinct **text sizes** | median **2**; six scenes use exactly one |
| Elements with `angle != 0` | **zero in 14 of 20** |
| Elements with `opacity != 100` | zero in 19 of 20 |
| `frame` elements | zero in 20 |
| Canvas background | `#ffffff` in **20 of 20** |
| Grid enabled | 3 of 20 |

Read that as one instruction: **decide each property once for the whole diagram.** Variation is what
the ugly examples have.

## 2. Geometry

| Measure | Value |
|---|---|
| Node width (rectangle) | median 140 to 330 px per scene |
| Node height | median 46 to 84 px, commonly **55 to 73** |
| Node aspect ratio | median 2.3 to 5.3 : 1; the sweet spot cluster is **2.5 to 3.3 : 1** |
| Node height / font size | about **3x** for a one-line label |
| Horizontal gutter | p25 **40 to 120 px**, about **1.4 to 1.8x node height** |
| Vertical gutter | p25 40 to 60 px |
| Nesting inset | clusters at **16, 20, 20, 21, 23, 24, 25, 26 px**. Below 10 reads cramped |
| Nesting depth | stop at **three**. The four-level example is the ugly one |
| Top-level nodes | the clean scenes: 6, 6, 9, 12, 15, 19 |
| Whole-diagram aspect | median 1.56 : 1 |
| Grid alignment | **mostly none** (7 to 20% of coordinates are multiples of 20) |

**Grid snapping is not what makes them look good.** Only 2 of 20 are snapped. Relative alignment
between siblings is what matters; absolute grid position buys nothing.

**The hard floor is Excalidraw's own.** `adjustRoughness()` keeps full roughness only when
`(minSide >= 20 && maxSide >= 50)`, or `(minSide >= 15 && rounded)`, or
`(linear && maxSide >= 50)`. Otherwise it halves it, or divides by three below 10 px. So a node
smaller than about 20x50 renders visibly smoother than its neighbours, which is the real answer to
"why does that one box look different".

**Contradiction worth naming:** the widely-copied prose skills prescribe 200 to 300 px horizontal
gutters. That is two to three times what real diagrams use, and it produces sparse layouts where the
arrows dominate the nodes.

## 3. Colour

### The pairing table is designed, and it is used

Excalidraw's five quick-pick slots are meant to be used slot-for-slot: stroke *i* with background
*i*. Verified as practice rather than intent - across 12,977 filled shapes, where both fill and
stroke are palette colours they are **the same hue family in 1,048 of 1,069 cases (98%)**. Only 21
cross-hue pairs exist in 55,000 elements. Separately, **52% of filled shapes use a neutral stroke**
with a coloured fill.

| Slot | Stroke | Background | Canvas |
|---|---|---|---|
| 1 | `#1e1e1e` | `transparent` | `#ffffff` |
| 2 | `#e03131` | `#ffc9c9` | `#f8f9fa` |
| 3 | `#2f9e44` | `#b2f2bb` | `#f5faff` |
| 4 | `#1971c2` | `#a5d8ff` | `#fffce8` |
| 5 | `#f08c00` | `#ffec99` | `#fdf8f6` |

Never cross hues between a shape's fill and its stroke.

### Budget and strategy

**One stroke colour plus three to five fills, and every colour must have a stated job.** Pick one of
two strategies and commit to it:

- **Monochrome stroke.** One stroke colour, uniform width, all colour in pale background tints.
  Nesting expressed by containment and size alone. This is what the cleanest Kubernetes scenes do:
  `openfga`, `ovn-kubernetes`, `kubekey` and `k8s-tutorials` each use exactly one stroke colour,
  `strokeWidth: 2` on 100% of elements, and put all colour in the lightest palette row.
- **Colour as identity.** One hue per subsystem, applied to its boundary, its children, its arrows
  *and* its arrow-label text. Needs no legend.

Above about five hues you owe the reader a legend panel binding each hue to a sentence.

### Fill style

**Hachure is not the Excalidraw look any more.** `DEFAULT_ELEMENT_PROPS.fillStyle` is `solid`, and
across 232 libraries: solid 16,314 (80%), hachure 2,575 (13%), cross-hatch 1,429 (7%), zigzag 0.
Hachure survives as a whole-diagram idiom, notably wireframe kits. **Never cross-hatch a large
nested boundary** - fill on fill on fill at three or four depths is the most reliable way to make a
cloud diagram unreadable.

`hachureGap = strokeWidth * 4`, so hachure at `strokeWidth: 4` is a 16 px gap and reads as stripes
rather than texture.

### Dashes are a scarce accent

Corpus-wide, **dashed is 0.9% and dotted 0.5%** of all strokes. The official
`components-of-kubernetes.svg` spends **exactly one dash in the whole file**, on the single most
important boundary. `openfga` and `ovn-kubernetes` use zero.

The semantics are near-universal:

| Style | Means |
|---|---|
| solid | fixed, owned, physical, synchronous, the main path |
| dashed | elastic, logical, claimed, async, inferred, a return, a boundary |
| dotted | a note, a lane separator, a grid |

One real counter-example to learn from: a project with 28 of 39 elements dashed, where dashed
therefore means nothing.

## 4. Type

`FONT_SIZES = {sm: 16, md: 20, lg: 28, xl: 36}`, `DEFAULT_FONT_SIZE = 20`.

- **Two sizes, three at most, on the 16/20/28/36 ladder.** 36 title, 28 section, 20 body, 16
  annotation. Nothing below 16.
- **A fractional font size is the highest-signal machine-detectable tell of an unmade diagram.**
  Values like `22.879256155` come from dragging a text box instead of using the size buttons.
- **Two typefaces, two jobs.** Hand-drawn (`Excalifont`, id 5) for labels and prose; the code font
  (`Comic Shanns` id 8, or `Cascadia` id 3) for anything literal - IPs, paths, commands, topic and
  table names, config values. Independently arrived at by six unrelated sources.
- `Lilita One` (id 7) is Excalidraw's own display face for titles, and appears 5 times in 55,000
  elements. Use it deliberately or not at all.
- Across the libraries: hand-drawn faces carry 84% of text, code fonts 9%, normal sans 12%.

## 5. Labels

| Measure | Value |
|---|---|
| Words per node label | median **2**, p75 3, p90 4. **106 of 218 are one word** |
| Lines per label | median **1** |
| Labels bound to a container | 42% overall, but **the cleanest scenes bind 15/15, 9/9, 25/28, 15/18, 14/17** and the messy ones bind 0/16 and 1/22 |
| Arrows carrying a bound label | 2 of 154 |

**Bind labels.** Binding is what centres the label, grows the box with the text, and punches the
arrow-label hole. The widely-copied "aim for under 30% of text elements inside containers" rule is
backwards against this measurement.

When a label needs more, use `Title` plus a smaller qualifier on a second line rather than a longer
first line.

## 6. Arrows

| Measure | Value |
|---|---|
| 2-point straight | **82%** |
| `(startArrowhead, endArrowhead)` | `(null, "arrow")` in **13 of 13** arrow-bearing scenes; corpus-wide `(null,"arrow")` 55%, `(null,null)` 20%, `(null,"triangle")` 19% |
| Double-headed | **2%** |
| Bound at both ends | 59% overall; **the best-looking scenes are 100%** |
| Binding gap | median about 5.9 px |
| Elbowed | rare |

Draw bidirectionality as **two parallel single-headed arrows**, which is what Excalidraw's own
templates do.

Excalidraw's four documented routing constraints, from its own engineering post on elbow arrows:
take the most direct path; fewer turns; **arrowheads point at the target and never enter it**; the
path must avoid connected shapes along its whole length.

**Fan-in via a shared bus, not N diagonals.** The single biggest legibility win observed: merge
spokes into one trunk before the hub, so ten sources produce four arrowheads rather than ten.

**Arrow labels get an opaque backing only when bound.** The renderer clears a rect of
`label.width + 10` by `label.height + 10` behind a text element whose `containerId` is the arrow.
The source comment is literally `punch the label "hole"`. Free text laid over an arrow gets nothing
and reads as a collision. The two proven alternatives when you cannot bind: a small hand-drawn
ellipse riding on the edge containing the label, or text rotated to follow a diagonal.

## 7. Roughness is a fidelity signal, not a style

| Value | Name | Use for |
|---|---|---|
| `0` | architect | Formal notation where the shapes carry meaning: BPMN, ERD, state machines, notation kits, generated graphs, icon sets |
| `1` | artist | Architecture and exploratory work. 17 of 20 real scenes, 51% of the corpus |
| `2` | cartoonist | Sticker-style canvases only. 7% of the corpus |

The strongest single piece of evidence: the author of the most lavish hand-drawn Obsidian mind maps
sets `roughness: 0` on both nodes and links in his *generated* graph tool. Jitter is a feature at ten
elements and noise at thirty.

`preserveVertices` is true whenever roughness is under 2, so corners still meet at `artist`. The
visible "corners do not quite meet" gap is a cartoonist-only artefact. Arrowheads are auto-clamped
to roughness 1 or less regardless, so they stay crisp.

**Choosing wrongly is a communication error, not an aesthetic one.** Rough says "argue with me about
the structure"; clean says "this is the shipped notation". The named failure in the wireframing
literature is having to deliver a drawing with a disclaimer because it looked too finished.

A recognised hybrid exists and is worth knowing: crisp frame, hand-written label. AWS's own boundary
containers render at `roughness 0, strokeWidth 2` while their labels render at `roughness 1,
strokeWidth 1`. Treat that as a deliberate two-layer choice, never as drift.

## 8. What makes it feel hand-drawn yet professional

1. **The wobble is one setting, applied uniformly.**
2. **Nothing is rotated and nothing is hand-jittered.** Zero rotated elements in 14 of 20. The
   imperfection is delegated entirely to the renderer; the layout is strict. Deliberate tilt is the
   tell of an amateur hand-drawn diagram. The two legitimate exceptions are structural: a swimlane
   name rotated in a narrow header, and an axis label rotated outside the frame.
3. **Sibling alignment, not grid snapping.**
4. **Type sits exactly on the ladder.**
5. **The hand-drawn font does the personality work.** A useful negative result: one well-known
   illustrator tried synthesising per-glyph handwriting variation and shipped the original
   single-glyph font unchanged because the output felt uncanny. One honest hand-made font beats
   synthesised variation, which is the choice Excalidraw itself makes.

## 9. Per-type recipes

**Flow / architecture.** Layered left-to-right or top-down. One stroke colour, pale tints for
grouping. 6 to 12 nodes. Straight 2-point arrows, heads only at the target end.

**Boundary and nesting.** Solid = a fixed, owned boundary; dashed = an elastic or logical grouping
whose membership changes at runtime. Colour per boundary *type*, not per instance. Label top-left
inside, about 20 px inset, three levels maximum.

**Sequence.** Column per actor. Head as a rectangle, a dashed lifeline, and the activation drawn as
a narrow rectangle's *width* rather than a fill. Solid arrowhead = synchronous, lined = async, dashed
+ lined = a return. A self-message is a four-point elbow U. Their own tip: a synchronous message
already implies its return, so drawing the return is usually clutter.

**Decision flow.** Diamond for the question, and the highest-value trick from a 31,682-download
library: put the `Yes` / `No` label inside a small hand-drawn ellipse riding on the edge, which
survives crossing lines where free text does not. Exactly three stroke colours: one for yes, one for
no, one for text.

**Mind map.** True radial, centre pushed to the middle-left of a wide frame. **The centre node is the
only boxed node.** Branches are thick tapered strokes, not boxes - the stroke *is* the container.
One colour per primary branch, six branches. Depth encoded by text size, about 1.4x step-down per
level. Left-of-centre branches read right-to-left with labels right-aligned to the line end. Long
prose is parked unboxed in open canvas, never inside a node.

**Timeline.** One axis, and **name the axis as a first-class element**: a long thin labelled arrow
spanning the band, so the direction of travel is stated rather than implied.

**Data lineage.** Draw fan-in as a vertical bus with horizontal spurs rather than N diagonals.
Zone headers can *be* the legend by carrying the layer name plus its two identifying properties, in
which case no separate legend is needed.

**Wireframe.** Black strokes, transparent fills, **sharp** corners (the reverse of every diagram
type), stroke width graded 1/2/4 by nesting depth. A rectangle with an X through it is an image
placeholder. Because the drawing is monochrome, any colour automatically reads as annotation, so
reserve colour for the comment layer. Widths 1440 desktop, 768 tablet, 375 mobile.

## 10. Annotation and legends

- **Ship a title and a legend the moment you have a second encoding.** The best notation-agnostic
  checklist to review against asks: does it have a title; a key; do you understand every colour,
  shape, icon, border style and element size used; does every arrow have a label describing intent;
  does the description match the arrow's direction.
- **Two-tier annotation.** Short labels on the artefact, everything long pushed to a numbered
  callout placed as a legend. With six or fewer steps, bare numerals beside the arrow shaft are
  enough. Place the badge at the arrow's tail, immediately before its label, so number and text read
  as one unit.
- **Alternatives that make a legend unnecessary:** colour-matched caption text, where the sentence
  explaining an arrow is tinted the same hue as the arrow; and row labels in the left gutter, each in
  its own layer's colour.
- **State the conclusion inside the diagram.** Put the delta in the caption rather than leaving the
  reader to compute it.
- **Elide rather than enumerate.** Three offset outlines behind a box for "many of these"; a brace
  with a count for a repeated block; cell count as dimensionality.

## 11. Ugly patterns, each observed

**Colour:** a palette grown by accretion; two layers coded with the same hue at different lightness
(collapses in greyscale and under red-green deficiency); colour redundant with shape *and* label text
recoloured to match; one colour per node instead of per role; an unexplained accent with no legend;
full-colour vendor logos fighting a monochrome scheme.

**Line:** an arrow-to-node stroke ratio above 4:1, which lets the connectors shout down the content;
dashing almost everything; one arrow style doing four different jobs; bidirectional arrows; arrows
piercing unrelated shapes or entering the target instead of pointing at it.

**Fill and nesting:** cross-hatch or saturated fill at four levels of nesting; nesting padding
swinging from 9 to 47 px inside one drawing; inconsistent decoration on structurally identical
elements; boxes drawn around things that are not a real group.

**Shape:** the vocabulary used backwards, notably a decision flowchart where the terminals are
diamonds and the questions are rounded rectangles; pictorial metaphor past about six node types;
cartoon mascots as nodes; depth encoded only in a word.

**Text:** fractional font sizes; unlabelled sequence arrows; orphan labels floating between two
arrows with no owner; an arrow direction contradicting its own label; anything below 16 px; typos
frozen into raster artwork, because diagrams do not get spellchecked.

**Structure:** no reading order; the subject of the diagram not marked; lopsided whitespace; mixed
roughness; instructional furniture left in from a template.
