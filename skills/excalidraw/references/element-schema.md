# The `.excalidraw` format, verified at source

Read from `excalidraw/excalidraw@master` on 2026-09-09, plus the shipped `.woff2` binaries. **The
public docs never enumerate the element schema**, so the source is the only spec, and two open
requests confirm that gap. Where npm's published package disagrees with `master`, both are given and
`master` wins, because `master` is what excalidraw.com runs.

You should not need this file to use the skill. It is here for the raw escape hatch, and for
debugging a diagram that opens looking wrong.

## Related context

- [`../SKILL.md`](../SKILL.md) - the workflow and the gotchas table.
- [`design-rules.md`](design-rules.md) - what to draw, as opposed to how to encode it.

## 1. Top-level file

```json
{
  "type": "excalidraw",
  "version": 2,
  "source": "https://excalidraw.com",
  "elements": [],
  "appState": { "gridSize": 20, "viewBackgroundColor": "#ffffff" },
  "files": {}
}
```

**The whole validation gate**, from `isValidExcalidrawData`: `type` must be exactly
`"excalidraw"`; if `elements` is present it must be an `Array`; if `appState` is present it must be
an object. `version` and `source` are never checked. Anything else raises `Error: invalid file`.

**Only five `appState` keys survive a file open**: `gridSize`, `gridStep`, `gridModeEnabled`,
`viewBackgroundColor`, `lockedMultiSelections`. `theme`, `scrollX`, `scrollY` and `zoom` are stripped
inbound by `cleanAppStateForExport`, so setting them in a file has **zero effect**. They do work
through the programmatic `initialData` prop.

Defaults and clamps: `gridSize` 20, clamped `[1, 100]`; `gridStep` 5; `viewBackgroundColor`
`#ffffff`; `zoom.value` clamped `[0.1, 30]`.

`files` maps a `fileId` to `{mimeType, id, dataURL, created}`, where `id` must equal both the map key
and the image element's `fileId`. Entries not referenced by a live element are dropped on save.

## 2. Element base

Required for the file to open: **`type` only.** Everything else is back-filled by
`restoreElementWithProperties`. But several defaults are traps, marked below.

| Field | Type | Notes |
|---|---|---|
| `id` | string | unique. A duplicate gets re-randomised, dangling every reference to it |
| `type` | literal | an unknown value is **silently dropped**: `restoreElement` has no default case |
| `x`, `y` | number | top-left of the unrotated box. Negative is legal. **Never write exactly 0** |
| `width`, `height` | number | recomputed from `points` for linear elements |
| `angle` | radians | rotation about the element **centre** |
| `strokeColor` | string | default `#1e1e1e` |
| `backgroundColor` | string | default `transparent`; a fill is invisible while it is transparent |
| `fillStyle` | enum | `hachure` `cross-hatch` `solid` `zigzag`. **Default is `solid`**, not hachure |
| `strokeWidth` | number | `{thin:1, medium:2, bold:4, extraBold:8}` |
| `strokeStyle` | enum | `solid` `dashed` `dotted` |
| `roundness` | null or object | see below; a mismatch silently yields radius 0 |
| `roughness` | 0/1/2 | architect / artist / cartoonist. Default 1 |
| `opacity` | number | **0 to 100**, not 0 to 1. `0.5` means half a percent |
| `seed` | number | **defaults to 1**, so omitting it makes every element wobble identically |
| `version` | number | must be > 0 |
| `versionNonce` | number | default 0 |
| `index` | string or null | fractional index. **Omit it**; see section 6 |
| `isDeleted` | boolean | a tombstone that never renders |
| `groupIds` | string[] | ordered deepest to shallowest: `[0]` innermost, last outermost |
| `frameId` | string or null | membership is one-directional, from child to frame |
| `boundElements` | array or null | `[{id, type: "arrow" \| "text"}]`. The reverse index |
| `updated`, `created` | epoch ms | `created` is a newer field and may be null |
| `link` | string or null | passed through `sanitizeUrl`, so an unsafe scheme silently does nothing |
| `locked` | boolean | |
| `customData` | object | preserved verbatim |

**Never emit** `selection` (dropped first), `focus`, `gap`, `strokeSharpness` or `boundElementIds`
(all legacy, migrated then deleted), or `lastCommittedPoint` (gone from element types on master).

Unknown properties are preserved verbatim, deliberately, for forward compatibility.

### `roundness`, and which type per element

| Type | Name | Radius |
|---|---|---|
| 1 | legacy | `min(w,h) * 0.25` |
| 2 | proportional | same formula |
| 3 | adaptive | `r = value ?? 32`; if `min(w,h) <= r/0.25` then `min(w,h)*0.25`, else the flat 32 |

- `{type: 3}` for **rectangle, image, iframe, embeddable**
- `{type: 2}` for **line, arrow, diamond**
- `null` for **ellipse** (the shape generator ignores it) and **text**

`{type:3}` on a diamond is accepted and applies a flat 32 px to both radii, which looks wrong at
small sizes.

## 3. Element types

### rectangle, diamond, ellipse
No extra fields.

### text

| Field | Notes |
|---|---|
| `fontSize` | `{sm:16, md:20, lg:28, xl:36}`, default 20 |
| `fontFamily` | numeric id, table below |
| `text` | the RENDERED string, including newlines inserted by wrapping |
| `originalText` | the unwrapped source. A mismatch reflows the label when a user double-clicks |
| `textAlign` | `left` `center` `right` |
| `verticalAlign` | `top` `middle` `bottom`. Does nothing on standalone text: centring lives only in `computeBoundTextPosition`, which is reached only via a container |
| `containerId` | the container, or null |
| `lineHeight` | **unitless** multiplier. Always write it |
| `autoResize` | true = box fits text; false = `width` IS the wrap width |
| `labelPosition` | newer; normalised 0 to 1 along an arrow container's path, default 0.5 |

`FONT_FAMILY`, with **id 4 deliberately unused**:

| id | Family | Picker label | `lineHeight` | Lowercase mean advance (em) |
|---|---|---|---|---|
| 1 | Virgil | deprecated | 1.25 | 0.5040 |
| 2 | Helvetica | deprecated, local only | 1.15 | not shipped |
| 3 | Cascadia | deprecated, mono | 1.20 | 0.5859 |
| **5** | **Excalifont** | **Hand-drawn, default** | **1.25** | **0.5215** |
| 6 | Nunito | Normal | 1.25 | 0.5118 |
| 7 | Lilita One | heading face | 1.15 | 0.4903 |
| 8 | Comic Shanns | Code, mono | 1.25 | 0.5500 |
| 9 | Liberation Sans | private | 1.15 | 0.4895 |
| 10 | Assistant | private | 1.25 | 0.4699 |

The advance column is measured, and is a *mean*: do not use it as a multiplier. Per-glyph spread in
Excalifont is 3.5x. Use `assets/font-metrics.json`.

**Omitting `lineHeight` while supplying a `height`** makes `detectLineHeight` back-derive
`height / lineCount / fontSize`, producing a non-standard value that permanently mis-spaces
multi-line text.

### line and freedraw

- `points: [[x,y], ...]` **relative to the element's `x`/`y`**, and **`points[0]` must be `[0,0]`**.
  Otherwise restore subtracts the first point from every point and adds it to `x`/`y`, so the element
  visibly moves.
- `width`/`height` are recomputed from the points; whatever you write is overwritten.
- Fewer than two valid points, or two points within 0.1 px, marks the element deleted.
- A `line` cannot bind: `startBinding` and `endBinding` are forced to null.
- `polygon: boolean` is kept only when the points genuinely close.
- freedraw adds `pressures[]`, `simulatePressure` (**no restore default: write it**), and
  `strokeOptions: {variability, streamline}`.

### arrow

- `points[0]` is `[0,0]`, as for line.
- **`endArrowhead` defaults to `"arrow"` when undefined.** Write `null` explicitly for none.
- Arrowheads available today: `arrow` `bar` `circle` `circle_outline` `triangle` `triangle_outline`
  `diamond` `diamond_outline`, plus the ER set `cardinality_one` `cardinality_many`
  `cardinality_one_or_many` `cardinality_exactly_one` `cardinality_zero_or_one`
  `cardinality_zero_or_many`. Legacy `dot` and `crowfoot_*` are rewritten on load.
  **Crow's-foot ERD cardinality is native**, so an ERD needs no library.
- `elbowed: boolean` for orthogonal routing; elbow-only extras are `fixedSegments`,
  `startIsSpecial`, `endIsSpecial`.

### image, frame, embeddable

- `image`: `fileId` (no restore default), `status` (`pending`/`saved`/`error`), `scale` as an axis
  mirror pair, `crop`. New image elements force `strokeColor: transparent`.
- `frame` / `magicframe`: `name`, and **no children array** - membership is only each child's
  `frameId`. The frame's own box is yours and nothing recomputes it, so a child outside the box stays
  a member and is clipped away invisibly. Convention: children immediately **before** the frame in
  the array. `FRAME_STYLE` is hard-coded (`#bbb`, width 2, radius 8) and not read from the element.
- `embeddable`: the URL lives in the base `link`, and renders live only for an allow-listed host.
- `LIBRARY_DISABLED_TYPES = {iframe, embeddable, image}`.

## 4. Binding, which is where files break

Both relationships are **reciprocal**, and each edge is stored twice.

### Bound text in a container

```json
{ "type": "rectangle", "id": "box1", "boundElements": [{ "type": "text", "id": "lbl1" }] },
{ "type": "text", "id": "lbl1", "containerId": "box1", "textAlign": "center",
  "verticalAlign": "middle", "lineHeight": 1.25 }
```

Valid containers: rectangle, ellipse, diamond, **arrow**. Not line, not image, not frame.

Restore repairs a one-sided link, with two caveats: two containers claiming one label leaves one
rendering empty, and only the **first** `{type:"text"}` entry is used. **Position is not derived on
load** - a wrong label `x`/`y` sits wrong until a human nudges the container.

### Arrow to shape, current schema

```json
"startBinding": { "elementId": "A", "fixedPoint": [1.0501, 0.5001], "mode": "orbit" }
```

- `focus` and `gap` **are gone from the type** on master, removed upstream. npm `0.18.1` still
  declares them, so a file round-tripped through an older client can be rewritten.
- A legacy binding is migrated by keeping only `elementId` and **recomputing `mode` and `fixedPoint`
  from where the arrow's endpoint actually is**. So the real contract is endpoint geometry, not the
  binding numbers.
- `fixedPoint` is a proportional coordinate in the target's unrotated box: `[0,0]` top-left, `[1,1]`
  bottom-right. Ratios legally exceed 0 to 1, clamped to `[-10, 10]`, which is how an orbit point
  sits outside the outline.
- **Neither coordinate may be exactly `0.5`.** It is snapped to `0.5001`, with the source reason
  being to stop the arrow heading jumping on floating-point imprecision.
- `mode`: `orbit` (stops outside the outline, the safe default), `inside`, `skip`.
- **There is no `gap` field.** The gap is computed as `BASE_BINDING_GAP (5) + target.strokeWidth / 2`.

### The two mechanisms, and why both sides are needed

| Written | Missing | Symptom |
|---|---|---|
| arrow binding only | shape's `boundElements` | opens fine; moving the **shape** does not drag the arrow, because `updateBoundElements` walks the shape's list. Detaches on first interaction |
| `boundElements` only | arrow binding | arrow stays put when the shape moves |
| both, but `points` inconsistent with `fixedPoint` | - | **renders correctly, then snaps** on first interaction. The classic "looked right until I touched it" |

Bindable targets: rectangle, diamond, ellipse, text, image, iframe, embeddable, frame, magicframe.

## 5. Text metrics without a browser

```
height = fontSize * lineHeight * lineCount        # exactly, no padding
width  = max over lines of the advance width
```

`BOUND_TEXT_PADDING = 5`. Max text width inside a container:

| Container | Max text width | Grown from text |
|---|---|---|
| rectangle | `w - 10` | `d + 10` |
| ellipse | `round(w/2 * sqrt2) - 10` | `round(((d+10)/sqrt2) * 2)` |
| diamond | `round(w/2) - 10` | `2 * (d + 10)` |
| arrow | `max(0.7*w, fontSize*11)` | `d + 80` |

Label origin: offset 5,5 from the container, **plus** `(w/2)(1 - sqrt2/2)` and `(h/2)(1 - sqrt2/2)`
for an ellipse, or `w/4` and `h/4` for a diamond. That inset is what the naive centring formula
misses.

Worked, `Hello world` in Excalifont 20 (measured 101.90 x 25):

| Container | Grown width | Grown height |
|---|---|---|
| rectangle | 112 | 35 |
| ellipse | 158 | 49 |
| diamond | 224 | 70 |

**Excalidraw never re-measures on file open.** `loadFromBlob` passes `repairBindings` and
`deleteInvisibleElements`, and **not** `refreshDimensions`. With `autoResize: false`, `width` is the
wrap width and a wrong value persists visibly until someone edits the text.

`setCustomTextMetricsProvider` is the sanctioned hook for supplying widths without a canvas. A code
search found 136 uses, all forks of excalidraw itself: nobody exploits it.

## 6. Z-order and `index`

**Array order is authoritative. Later in the array draws on top.** `syncInvalidIndices` syncs
`index` *to* the array and never re-sorts by it.

**Omit `index`.** Restore runs `syncInvalidIndices` unconditionally and generates consistent keys.
A hand-written partially-invalid set throws `invalid order key`, the throw is swallowed, and **the
canvas renders blank** - and a set that happens to be monotonic slips through, which is why a
three-element repro looks fine while a real board blanks.

The format, for reference only: base-62 keys where the integer part's length is encoded in its first
character, so the first element is `a0`, appends run `a1`, `a2` ... and a prepend before `a0` borrows
to `Zz`. Bound text must sort immediately after its container.

## 7. Silent drops and "opens but looks wrong"

Dropped without an error: an unknown `type`; a `selection` element; a duplicate `id` (re-randomised);
empty `text`; `width` and `height` both 0; a linear element with under two points; an arrow whose two
points coincide within 0.1 px; a linear element above **75,000 px** in either dimension (replaced by a
100x100 stub and marked deleted).

Opens and looks wrong: `points[0]` not `[0,0]`; wrong text width; omitted `seed`; omitted
`lineHeight`; `opacity` on a 0-to-1 scale; `roundness` type mismatched to the element class; a
one-sided binding; `points` inconsistent with `fixedPoint`; `fixedPoint` exactly `0.5`;
`endArrowhead` omitted when none was wanted; `angle` in degrees; frame children outside the frame box;
`groupIds` on only some members.

## 8. `.excalidrawlib`

```json
{ "type": "excalidrawlib", "version": 2, "source": "...",
  "libraryItems": [ { "id": "...", "status": "unpublished", "created": 0, "name": "...",
                      "elements": [] } ] }
```

`isValidLibrary` accepts versions **1 or 2 only**; v1 used a top-level `library` holding an array of
arrays. Only `elements` is strictly required; the rest is back-filled. An item whose elements all
restore to deleted is dropped entirely.

Reuse notes from parsing the whole public corpus (232 libraries, 4,187 items, 55,113 elements):
**zero image elements and zero `files` payloads**, so reuse never needs blob handling. Only 2 of
4,187 items sit at the origin, median offset (766, 330), so normalise by `getCommonBounds` and
translate. **2,169 element ids already collide across the corpus**, so regenerate `id`, `groupIds`,
`seed` and `versionNonce` on insert. 92% of elements carry a non-empty `groupIds`, so group every
multi-element item.

The index is machine-readable at `libraries.excalidraw.com/libraries.json`, with `itemNames` giving
3,147 searchable names without downloading anything, and `stats.json` giving download counts for
ranking. One-click install is restricted to an allow-list of two hosts, so a self-hosted library
cannot be installed by URL.
