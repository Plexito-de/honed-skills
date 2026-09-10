#!/usr/bin/env python3
"""Build a valid .excalidraw file from a compact spec. Standard library only.

WHY THIS EXISTS
---------------
Asking a model to emit .excalidraw JSON directly fails in three separable ways, and each has a
deterministic answer:

  1. Coordinates. A model placing boxes by hand is doing arithmetic it is bad at, and the field is
     full of reports of overlapping nodes and arrows crossing through shapes. A layout algorithm
     does not have that problem.
  2. Text width. Excalidraw NEVER re-measures text when a file is opened: `restoreElements` runs
     without `refreshDimensions`, so the stored width is final and a wrong one silently clips
     glyphs. Every published generator guesses `len * fontSize * 0.55`. Measured against the real
     font, that is +20% on "Authentication Service", -28% on "MMMMM" and +146% on "iiiii".
  3. Invariants. Most violations open fine and look wrong, which is the worst failure mode: a
     half-written binding renders correctly and detaches the first time somebody drags the shape.

The model writes a spec of tens of lines; this produces the hundreds of lines of JSON.

DETERMINISM IS A GUARANTEE, NOT A HAPPY ACCIDENT
------------------------------------------------
The same spec produces a BYTE-IDENTICAL file. Element ids, `seed` and `versionNonce` are derived
from a BLAKE2b of the spec content plus the element's path within it; timestamps default to a fixed
epoch; `index` is omitted so Excalidraw derives it from array order; every layout tie-break sorts on
a stable key. No prior implementation does this: the most-installed one seeds from Python's `hash()`,
which is salted per process by PYTHONHASHSEED, so it looks reproducible and is not.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
No network. No subprocess. No third-party import. Rendering and screenshots are commands a human or
an agent runs, described in SKILL.md, so this file cannot execute anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"
METRICS_PATH = ASSETS / "font-metrics.json"

# ---------------------------------------------------------------- constants, all from source

# packages/common/src/constants.ts
BOUND_TEXT_PADDING = 5
DEFAULT_FONT_SIZE = 20
FONT_SIZES = {"sm": 16, "md": 20, "lg": 28, "xl": 36}
ARROW_LABEL_WIDTH_FRACTION = 0.7
ARROW_LABEL_FONT_SIZE_TO_MIN_WIDTH_RATIO = 11
BASE_BINDING_GAP = 5
MAX_LINEAR_PX = 75_000

# `fixedPoint` coordinates are snapped away from exactly 0.5 by normalizeFixedPoint, with the
# reason in the source: "to avoid jumping arrow heading due to floating point imprecision".
#
# 0.5002 rather than the 0.5001 the upstream plugin uses, and the reason is measurable: the check is
# `abs(v - 0.5) < EPSILON` with EPSILON = 0.0001, and 0.5001 is not exactly representable, so
# `0.5001 - 0.5` evaluates to 9.999999999999899e-05 in IEEE 754 double, which is BELOW the epsilon.
# 0.5001 therefore sits on the wrong side of the very guard it was chosen to clear. 0.5002 is
# unambiguously outside it and is 0.02% of the box away from centre, which is invisible.
# Found by our own linter on the first diagram this builder produced, 2026-09-09.
HALF = 0.5002

# adjustRoughness() quietly halves roughness below these sizes, so a small node renders visibly
# smoother than its neighbours. Keeping every node above the floor is why they look consistent.
MIN_NODE_W, MIN_NODE_H = 50, 20

# Excalidraw's own quick-pick pairing table: slot i of the stroke strip belongs with slot i of the
# background strip. Verified as real practice, not just intent: across 55,000 published library
# elements, fill and stroke are the same hue family in 1,048 of 1,069 palette-to-palette pairs.
PALETTE = {
    "ink":    {"stroke": "#1e1e1e", "bg": "transparent"},
    "red":    {"stroke": "#e03131", "bg": "#ffc9c9"},
    "green":  {"stroke": "#2f9e44", "bg": "#b2f2bb"},
    "blue":   {"stroke": "#1971c2", "bg": "#a5d8ff"},
    "yellow": {"stroke": "#f08c00", "bg": "#ffec99"},
    "violet": {"stroke": "#6741d9", "bg": "#d0bfff"},
    "grey":   {"stroke": "#343a40", "bg": "#e9ecef"},
}
NEUTRAL_STROKE = "#1e1e1e"
ANNOTATION_GREY = "#868e96"

# One roughness per diagram: measured across 20 real published scenes, 20 of 20 use exactly one
# value, and every drawing that mixes them is in the ugly pile. `formal` turns sloppiness off,
# which is what every notation kit and every generated-graph tool does.
STYLES = {
    "architecture": {"roughness": 1, "fillStyle": "solid", "font": 5, "strokeWidth": 2},
    "formal":       {"roughness": 0, "fillStyle": "solid", "font": 6, "strokeWidth": 2},
    "sticker":      {"roughness": 2, "fillStyle": "solid", "font": 5, "strokeWidth": 2},
}

# Geometry, from measurement rather than taste. Gutters are about 1.5x node height in real
# diagrams (p25 40-120 px horizontal); the popular prose skills prescribe 200-300, which is two to
# three times reality and produces sparse, arrow-dominated layouts.
GUTTER_X, GUTTER_Y = 110, 70
NEST_INSET = 20
ORIGIN = 100          # never emit exactly 0: three upstream falsy-zero bugs are open on it

ROUNDNESS_ADAPTIVE = {"type": 3}      # rectangle, image, iframe, embeddable
ROUNDNESS_PROPORTIONAL = {"type": 2}  # line, arrow, diamond


# ---------------------------------------------------------------- text metrics


class Metrics:
    """Advance widths measured in a browser, so they match what Excalidraw stored."""

    def __init__(self, path: Path = METRICS_PATH):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.by_id = {}
        for name, fam in data["families"].items():
            fam = dict(fam)
            fam["name"] = name
            self.by_id[int(fam["id"])] = fam

    def family(self, font_id: int) -> dict:
        return self.by_id.get(int(font_id)) or self.by_id[5]

    def line_height(self, font_id: int) -> float:
        return float(self.family(font_id)["lineHeight"])

    def line_width(self, text: str, font_id: int, font_size: float) -> float:
        fam = self.family(font_id)
        widths, kern, fallback = fam["widths"], fam["kern"], fam["fallbackWidth"]
        total = 0.0
        previous = ""
        for ch in text:
            total += widths.get(ch, fallback)
            if previous:
                total += kern.get(previous + ch, 0.0)
            previous = ch
        return total * font_size

    def wrap(self, text: str, font_id: int, font_size: float, max_width: float) -> list[str]:
        """Greedy word wrap, which is what the editor does. A word longer than the line is not
        broken, because breaking it would change the text rather than its layout."""
        lines: list[str] = []
        for paragraph in text.split("\n"):
            if not paragraph:
                lines.append("")
                continue
            current = ""
            for word in paragraph.split(" "):
                candidate = f"{current} {word}".strip()
                if current and self.line_width(candidate, font_id, font_size) > max_width:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            lines.append(current)
        return lines or [""]

    def measure(self, text: str, font_id: int, font_size: float) -> tuple[float, float]:
        """(width, height). Height is exactly fontSize * lineHeight * lineCount, no padding."""
        lines = text.split("\n")
        width = max((self.line_width(line or " ", font_id, font_size) for line in lines),
                    default=0.0)
        height = font_size * self.line_height(font_id) * max(1, len(lines))
        return width, height


# ---------------------------------------------------------------- container geometry

def max_text_width(container_type: str, width: float, font_size: float) -> float:
    """getBoundTextMaxWidth. The naive (width - textWidth)/2 that every published skill uses is
    wrong for an ellipse and a diamond by the inscribed-box inset."""
    if container_type == "ellipse":
        return round(width / 2 * math.sqrt(2)) - BOUND_TEXT_PADDING * 2
    if container_type == "diamond":
        return round(width / 2) - BOUND_TEXT_PADDING * 2
    if container_type == "arrow":
        return max(ARROW_LABEL_WIDTH_FRACTION * width,
                   font_size * ARROW_LABEL_FONT_SIZE_TO_MIN_WIDTH_RATIO)
    return width - BOUND_TEXT_PADDING * 2


def grow_container(dimension: float, container_type: str) -> float:
    """computeContainerDimensionForBoundText."""
    d = math.ceil(dimension)
    pad = BOUND_TEXT_PADDING * 2
    if container_type == "ellipse":
        return round(((d + pad) / math.sqrt(2)) * 2)
    if container_type == "diamond":
        return 2 * (d + pad)
    if container_type == "arrow":
        return d + BOUND_TEXT_PADDING * 8 * 2
    return d + pad


def label_offsets(container_type: str, width: float, height: float) -> tuple[float, float]:
    """getContainerCoords: the inset from the container origin to its text box."""
    ox = oy = float(BOUND_TEXT_PADDING)
    if container_type == "ellipse":
        ox += (width / 2) * (1 - math.sqrt(2) / 2)
        oy += (height / 2) * (1 - math.sqrt(2) / 2)
    elif container_type == "diamond":
        ox += width / 4
        oy += height / 4
    return ox, oy


# ---------------------------------------------------------------- deterministic identity

class Ids:
    """Stable ids, seeds and nonces derived from the spec, so the same spec is byte-identical."""

    def __init__(self, spec_bytes: bytes):
        self.root = hashlib.blake2b(spec_bytes, digest_size=16).digest()

    def _digest(self, path: str) -> bytes:
        return hashlib.blake2b(self.root + path.encode("utf-8"), digest_size=16).digest()

    def element_id(self, path: str) -> str:
        # 21 chars, matching the shape of the app's nanoid, from a base62 alphabet.
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        value = int.from_bytes(self._digest("id:" + path), "big")
        out = []
        for _ in range(21):
            value, rem = divmod(value, len(alphabet))
            out.append(alphabet[rem])
        return "".join(out)

    def seed(self, path: str) -> int:
        # Distinct per element and never 1: omitting seed gives every element seed 1, so every
        # hand-drawn stroke wobbles in lockstep and the diagram reads as synthetic.
        return int.from_bytes(self._digest("seed:" + path), "big") % (2 ** 31 - 2) + 1

    def nonce(self, path: str) -> int:
        return int.from_bytes(self._digest("nonce:" + path), "big") % (2 ** 31 - 1)


# ---------------------------------------------------------------- element factories

FIXED_EPOCH = 1757376000000   # 2026-09-09T00:00:00Z, so a rebuild does not churn the diff


class Scene:
    def __init__(self, spec: dict, metrics: Metrics, ids: Ids, stamp: int = FIXED_EPOCH):
        self.spec = spec
        self.m = metrics
        self.ids = ids
        self.stamp = stamp
        self.elements: list[dict] = []
        style_name = spec.get("style", "architecture")
        if style_name not in STYLES:
            raise SpecError(f"unknown style {style_name!r}; pick one of {sorted(STYLES)}")
        self.style = STYLES[style_name]

    # -- base ------------------------------------------------------------------

    def base(self, path: str, kind: str, x: float, y: float, w: float, h: float,
             stroke: str | None = None, bg: str = "transparent",
             roundness: dict | None = None, stroke_style: str = "solid",
             stroke_width: float | None = None) -> dict:
        return {
            "id": self.ids.element_id(path),
            "type": kind,
            "x": round(x, 2), "y": round(y, 2),
            "width": round(w, 2), "height": round(h, 2),
            "angle": 0,
            "strokeColor": stroke or NEUTRAL_STROKE,
            "backgroundColor": bg,
            "fillStyle": self.style["fillStyle"],
            "strokeWidth": self.style["strokeWidth"] if stroke_width is None else stroke_width,
            "strokeStyle": stroke_style,
            "roughness": self.style["roughness"],
            "opacity": 100,                      # 0-100, not 0-1
            "roundness": roundness,
            "seed": self.ids.seed(path),
            "version": 1,
            "versionNonce": self.ids.nonce(path),
            "index": None,                       # syncInvalidIndices derives it from array order
            "isDeleted": False,
            "groupIds": [],
            "frameId": None,
            "boundElements": [],
            "updated": self.stamp,
            "created": self.stamp,
            "link": None,
            "locked": False,
        }

    # -- text ------------------------------------------------------------------

    def text_element(self, path: str, text: str, x: float, y: float, font_size: int,
                     font_id: int, stroke: str = NEUTRAL_STROKE,
                     align: str = "left", vertical: str = "top",
                     container_id: str | None = None, width: float | None = None,
                     height: float | None = None) -> dict:
        measured_w, measured_h = self.m.measure(text, font_id, font_size)
        el = self.base(path, "text", x, y, width if width is not None else measured_w,
                       height if height is not None else measured_h, stroke=stroke)
        el["roundness"] = None                   # text and ellipse take null, never a type
        el.update({
            "text": text,
            "originalText": text,
            "fontSize": font_size,
            "fontFamily": font_id,
            "lineHeight": self.m.line_height(font_id),   # ALWAYS: omitting it back-derives a
                                                          # bogus value from height
            "textAlign": align,
            "verticalAlign": vertical,
            "containerId": container_id,
            "autoResize": True,
            "labelPosition": None,
        })
        return el

    # -- labelled shape --------------------------------------------------------

    def labelled_shape(self, path: str, kind: str, label: str, x: float, y: float,
                       min_w: float, min_h: float, stroke: str, bg: str,
                       font_size: int, stroke_style: str = "solid") -> tuple[dict, dict | None]:
        font_id = self.style["font"]
        roundness = (ROUNDNESS_PROPORTIONAL if kind == "diamond"
                     else None if kind == "ellipse" else ROUNDNESS_ADAPTIVE)
        if not label:
            shape = self.base(path, kind, x, y, max(min_w, MIN_NODE_W), max(min_h, MIN_NODE_H),
                              stroke=stroke, bg=bg, roundness=roundness,
                              stroke_style=stroke_style)
            return shape, None

        # Wrap to the widest line the requested box allows, then grow the box to the text.
        allowance = max_text_width(kind, max(min_w, MIN_NODE_W), font_size)
        lines = self.m.wrap(label, font_id, font_size, allowance)
        wrapped = "\n".join(lines)
        tw, th = self.m.measure(wrapped, font_id, font_size)
        w = max(min_w, MIN_NODE_W, grow_container(tw, kind))
        h = max(min_h, MIN_NODE_H, grow_container(th, kind))

        shape = self.base(path, kind, x, y, w, h, stroke=stroke, bg=bg, roundness=roundness,
                          stroke_style=stroke_style)
        ox, oy = label_offsets(kind, w, h)
        max_w = max_text_width(kind, w, font_size)
        max_h = (round(h / 2 * math.sqrt(2)) - BOUND_TEXT_PADDING * 2 if kind == "ellipse"
                 else round(h / 2) - BOUND_TEXT_PADDING * 2 if kind == "diamond"
                 else h - BOUND_TEXT_PADDING * 2)
        tx = shape["x"] + ox + (max_w / 2 - tw / 2)
        ty = shape["y"] + oy + (max_h / 2 - th / 2)
        text = self.text_element(path + "/label", wrapped, tx, ty, font_size, font_id,
                                 stroke=stroke, align="center", vertical="middle",
                                 container_id=shape["id"], width=tw, height=th)
        # Reciprocal, both directions. One side alone renders correctly and then detaches.
        shape["boundElements"] = [{"type": "text", "id": text["id"]}]
        return shape, text

    # -- arrow -----------------------------------------------------------------

    def arrow(self, path: str, src: dict, dst: dict, label: str = "",
              stroke: str = NEUTRAL_STROKE, stroke_style: str = "solid",
              end_head: str | None = "arrow", detour: float = 0.0) -> list[dict]:
        """A bound arrow, with the endpoint geometry the binding implies.

        Both halves of every binding are written: the arrow follows a moved shape via the SHAPE's
        boundElements, while the endpoint comes from the ARROW's binding. Different mechanisms,
        both required, and `restore` repairs neither. This is the single most-reported defect in
        every agent-written Excalidraw file.
        """
        sx, sy, ex, ey, sfp, efp = self._edge_points(src, dst)
        points = [[0, 0], [round(ex - sx, 2), round(ey - sy, 2)]]
        if detour:
            # A SKIP EDGE GETS A BEND, because a straight one crosses whatever sits between its
            # ends. Excalidraw's own routing rules say the path must avoid connected shapes along
            # its whole length, and our linter flags it when it does not. Measured on the first
            # example built with this tool: api -> db skipped one rank and ran straight through the
            # cache node. Three points, out and back, which is what real diagrams do.
            mid_x, mid_y = round((ex - sx) / 2, 2), round((ey - sy) / 2, 2)
            if abs(ex - sx) >= abs(ey - sy):
                points = [[0, 0], [mid_x, detour], [round(ex - sx, 2), round(ey - sy, 2)]]
            else:
                points = [[0, 0], [detour, mid_y], [round(ex - sx, 2), round(ey - sy, 2)]]
        span_x = max(p[0] for p in points) - min(p[0] for p in points)
        span_y = max(p[1] for p in points) - min(p[1] for p in points)
        el = self.base(path, "arrow", sx, sy, span_x, span_y,
                       stroke=stroke, roundness=ROUNDNESS_PROPORTIONAL,
                       stroke_style=stroke_style)
        el.update({
            "points": points,                    # points[0] MUST be [0,0] or restore shifts x/y
            "lastCommittedPoint": None,
            "startArrowhead": None,
            "endArrowhead": end_head,            # explicit: undefined defaults to "arrow"
            "elbowed": False,
            "startBinding": {"elementId": src["id"], "fixedPoint": sfp, "mode": "orbit"},
            "endBinding": {"elementId": dst["id"], "fixedPoint": efp, "mode": "orbit"},
        })
        for shape in (src, dst):
            shape.setdefault("boundElements", []).append({"type": "arrow", "id": el["id"]})
        out = [el]
        if label:
            font_id = self.style["font"]
            size = FONT_SIZES["sm"]
            allowance = max_text_width("arrow", max(abs(ex - sx), abs(ey - sy)), size)
            wrapped = "\n".join(self.m.wrap(label, font_id, size, allowance))
            tw, th = self.m.measure(wrapped, font_id, size)
            # Place the label on the PATH, not on the straight line between the endpoints. A
            # detoured arrow has a bend, and centring on the chord put the label on top of the
            # node the detour exists to avoid. Seen on the first example built with this tool.
            if len(points) >= 3:
                anchor_x, anchor_y = sx + points[1][0], sy + points[1][1]
            else:
                anchor_x, anchor_y = (sx + ex) / 2, (sy + ey) / 2
            mid_x = anchor_x - tw / 2
            mid_y = anchor_y - th / 2
            if len(points) >= 3:
                mid_y += th * 0.6 + 2       # below the bend, clear of the shaft
            elif abs(ex - sx) >= abs(ey - sy):
                mid_y -= th * 0.75 + 2      # above a horizontal shaft
            else:
                mid_x += tw * 0.25 + 6      # beside a vertical shaft
            # Bound to the arrow, which is how the label gets its opaque backing for free: the
            # renderer punches a hole of label+10px through the arrow behind a BOUND label. Free
            # text laid over an arrow gets no such treatment and reads as a collision.
            text = self.text_element(path + "/label", wrapped, mid_x, mid_y, size, font_id,
                                     stroke=stroke, align="center", vertical="middle",
                                     container_id=el["id"], width=tw, height=th)
            text["labelPosition"] = 0.5
            el["boundElements"] = [{"type": "text", "id": text["id"]}]
            out.append(text)
        return out

    def _edge_points(self, a: dict, b: dict):
        """Endpoints just outside each outline, plus the proportional fixedPoint for each.

        `fixedPoint` is a ratio in the target's unrotated box: [0,0] top-left, [1,1] bottom-right.
        Ratios legally exceed 0..1, which is how an orbit point sits outside the outline. Neither
        coordinate may be exactly 0.5.
        """
        acx, acy = a["x"] + a["width"] / 2, a["y"] + a["height"] / 2
        bcx, bcy = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
        gap = BASE_BINDING_GAP + self.style["strokeWidth"] / 2
        if abs(bcx - acx) >= abs(bcy - acy):
            if bcx >= acx:                       # left to right
                return (a["x"] + a["width"] + gap, acy, b["x"] - gap, bcy,
                        [round(1 + gap / max(a["width"], 1), 4), HALF],
                        [round(-gap / max(b["width"], 1), 4), HALF])
            return (a["x"] - gap, acy, b["x"] + b["width"] + gap, bcy,
                    [round(-gap / max(a["width"], 1), 4), HALF],
                    [round(1 + gap / max(b["width"], 1), 4), HALF])
        if bcy >= acy:                           # top to bottom
            return (acx, a["y"] + a["height"] + gap, bcx, b["y"] - gap,
                    [HALF, round(1 + gap / max(a["height"], 1), 4)],
                    [HALF, round(-gap / max(b["height"], 1), 4)])
        return (acx, a["y"] - gap, bcx, b["y"] + b["height"] + gap,
                [HALF, round(-gap / max(a["height"], 1), 4)],
                [HALF, round(1 + gap / max(b["height"], 1), 4)])


class SpecError(Exception):
    pass


# ---------------------------------------------------------------- layouts

def layered_ranks(nodes: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Longest-path ranking with deterministic cycle breaking.

    Ties break on the node's position in the spec, never on set iteration order, so the same
    spec always produces the same ranks.
    """
    order = {n: i for i, n in enumerate(nodes)}
    outgoing: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        if a in outgoing and b in outgoing and a != b:
            outgoing[a].append(b)

    # Break cycles by dropping the back edge whose target appears earliest in the spec.
    colour: dict[str, int] = {}
    removed: set[tuple[str, str]] = set()

    def visit(node: str) -> None:
        colour[node] = 1
        for nxt in sorted(outgoing[node], key=lambda n: order[n]):
            if colour.get(nxt) == 1:
                removed.add((node, nxt))
            elif colour.get(nxt, 0) == 0:
                visit(nxt)
        colour[node] = 2

    for n in nodes:
        if colour.get(n, 0) == 0:
            visit(n)

    rank = {n: 0 for n in nodes}
    for _ in range(len(nodes)):
        changed = False
        for a, b in edges:
            if (a, b) in removed or a not in rank or b not in rank or a == b:
                continue
            if rank[b] < rank[a] + 1:
                rank[b] = rank[a] + 1
                changed = True
        if not changed:
            break
    return rank


def order_within_ranks(nodes: list[str], edges: list[tuple[str, str]],
                       rank: dict[str, int], sweeps: int = 4) -> dict[str, int]:
    """Barycentre ordering with a fixed sweep count, so the result cannot drift run to run."""
    order = {n: i for i, n in enumerate(nodes)}
    by_rank: dict[int, list[str]] = {}
    for n in nodes:
        by_rank.setdefault(rank[n], []).append(n)
    for r in by_rank:
        by_rank[r].sort(key=lambda n: order[n])

    incoming: dict[str, list[str]] = {n: [] for n in nodes}
    outgoing: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        if a in incoming and b in incoming:
            outgoing[a].append(b)
            incoming[b].append(a)

    for sweep in range(sweeps):
        ranks = sorted(by_rank) if sweep % 2 == 0 else sorted(by_rank, reverse=True)
        position = {n: i for r in by_rank for i, n in enumerate(by_rank[r])}
        for r in ranks:
            neighbours = incoming if sweep % 2 == 0 else outgoing
            def key(n: str) -> tuple[float, int]:
                near = [position[x] for x in neighbours[n] if x in position]
                return (sum(near) / len(near) if near else position[n], order[n])
            by_rank[r].sort(key=key)
    return {n: i for r in by_rank for i, n in enumerate(by_rank[r])}


def tree_positions(root: str, children: dict[str, list[str]], depth: int = 0,
                   cursor: list[int] | None = None,
                   out: dict[str, tuple[int, int]] | None = None):
    """Tidy tree: leaves take the next free column, a parent centres over its children."""
    cursor = [0] if cursor is None else cursor
    out = {} if out is None else out
    kids = children.get(root, [])
    if not kids:
        out[root] = (cursor[0], depth)
        cursor[0] += 1
        return out
    spans = []
    for kid in kids:
        tree_positions(kid, children, depth + 1, cursor, out)
        spans.append(out[kid][0])
    out[root] = ((spans[0] + spans[-1]) / 2, depth)
    return out


# ---------------------------------------------------------------- spec to scene

def load_spec(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if path.suffix.lower() in (".json",):
        return json.loads(text), raw
    try:
        import yaml  # noqa: PLC0415 - optional, and its absence must not be fatal
    except ImportError as exc:
        raise SpecError(
            "this spec is YAML and PyYAML is not importable. Either convert the spec to JSON "
            "(same keys) or install PyYAML. The builder itself needs nothing beyond the "
            "standard library; only YAML parsing does."
        ) from exc
    return yaml.safe_load(text), raw


def build(spec: dict, spec_bytes: bytes, stamp: int = FIXED_EPOCH) -> dict:
    metrics = Metrics()
    scene = Scene(spec, metrics, Ids(spec_bytes), stamp)
    kind = spec.get("type", "flow")
    nodes = spec.get("nodes") or []
    if not nodes:
        raise SpecError("a spec needs at least one entry under `nodes`")
    edges_raw = spec.get("edges") or []

    ids = [str(n["id"]) for n in nodes]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise SpecError(f"duplicate node id(s): {', '.join(dupes)}")
    by_id = {str(n["id"]): n for n in nodes}
    for e in edges_raw:
        for side in ("from", "to"):
            if str(e.get(side)) not in by_id:
                raise SpecError(f"edge {side} {e.get(side)!r} is not a node id")
    edges = [(str(e["from"]), str(e["to"])) for e in edges_raw]

    if kind in ("flow", "network", "c4", "sequence"):
        rank = layered_ranks(ids, edges)
        pos = order_within_ranks(ids, edges, rank)
        grid = {n: (pos[n], rank[n]) for n in ids}
        horizontal = spec.get("direction", "right") in ("right", "left")
    elif kind in ("tree", "org", "mindmap"):
        children: dict[str, list[str]] = {n: [] for n in ids}
        has_parent = set()
        for a, b in edges:
            children[a].append(b)
            has_parent.add(b)
        roots = [n for n in ids if n not in has_parent] or [ids[0]]
        grid = {}
        cursor = [0]
        for root in roots:
            grid.update(tree_positions(root, children, 0, cursor, {}))
        horizontal = False
    elif kind in ("grid", "kanban", "swimlane", "journey", "timeline", "gantt"):
        columns = int(spec.get("columns") or max(1, round(math.sqrt(len(ids)))))
        if kind in ("timeline", "gantt"):
            columns = len(ids)
        grid = {n: (i % columns, i // columns) for i, n in enumerate(ids)}
        horizontal = True
    else:
        raise SpecError(f"unknown type {kind!r}")

    # Build the shapes first so their measured sizes drive the spacing.
    shapes: dict[str, dict] = {}
    labels: dict[str, dict] = {}
    font_size = FONT_SIZES.get(str(spec.get("fontSize", "md")), DEFAULT_FONT_SIZE)
    for node in nodes:
        nid = str(node["id"])
        role = str(node.get("color") or ("ink" if not node.get("emphasis") else "blue"))
        colours = PALETTE.get(role, PALETTE["ink"])
        shape_kind = {"decision": "diamond", "start": "ellipse", "end": "ellipse",
                      "state": "ellipse"}.get(str(node.get("kind") or ""), "rectangle")
        stroke_style = "dashed" if node.get("boundary") else "solid"
        shape, label = scene.labelled_shape(
            f"node/{nid}", shape_kind, str(node.get("label") or nid), 0, 0,
            MIN_NODE_W * 3, MIN_NODE_H * 3,
            colours["stroke"] if role != "ink" else NEUTRAL_STROKE,
            colours["bg"], font_size, stroke_style)
        shapes[nid] = shape
        if label:
            labels[nid] = label

    col_w: dict[int, float] = {}
    row_h: dict[int, float] = {}
    for nid, (c, r) in grid.items():
        cc, rr = (c, r) if not horizontal else (r, c)
        col_w[cc] = max(col_w.get(cc, 0.0), shapes[nid]["width"])
        row_h[rr] = max(row_h.get(rr, 0.0), shapes[nid]["height"])

    x_at, acc = {}, float(ORIGIN)
    for c in sorted(col_w):
        x_at[c] = acc
        acc += col_w[c] + GUTTER_X
    y_at, acc = {}, float(ORIGIN)
    for r in sorted(row_h):
        y_at[r] = acc
        acc += row_h[r] + GUTTER_Y

    for nid, (c, r) in grid.items():
        cc, rr = (c, r) if not horizontal else (r, c)
        shape = shapes[nid]
        # Centre each node inside its column and row, which is what makes rows read as aligned.
        nx = x_at[cc] + (col_w[cc] - shape["width"]) / 2
        ny = y_at[rr] + (row_h[rr] - shape["height"]) / 2
        dx, dy = nx - shape["x"], ny - shape["y"]
        shape["x"], shape["y"] = round(nx, 2), round(ny, 2)
        if nid in labels:
            labels[nid]["x"] = round(labels[nid]["x"] + dx, 2)
            labels[nid]["y"] = round(labels[nid]["y"] + dy, 2)

    # Order matters: array order IS z-order, and a bound label must sit immediately after its
    # container or restore reorders the array and rewrites every index.
    out: list[dict] = []
    for nid in ids:
        out.append(shapes[nid])
        if nid in labels:
            out.append(labels[nid])

    def rank_of(nid: str) -> int:
        # rank is always the SECOND slot of the grid tuple. `horizontal` decides which screen axis
        # the rank maps to, not which tuple element holds it, and conflating the two silently
        # disabled every skip-edge detour.
        return int(grid[nid][1])

    for i, e in enumerate(edges_raw):
        a, b = str(e["from"]), str(e["to"])
        skipped = abs(rank_of(a) - rank_of(b))
        detour = 0.0 if skipped <= 1 else (GUTTER_Y * 0.9 + 20 * (skipped - 1))
        style = "dashed" if e.get("style") == "dashed" else "solid"
        head = None if e.get("arrow") is False else "arrow"
        colours = PALETTE.get(str(e.get("color") or "ink"), PALETTE["ink"])
        out.extend(scene.arrow(f"edge/{i}/{a}->{b}", shapes[a], shapes[b],
                               str(e.get("label") or ""),
                               colours["stroke"] if e.get("color") else NEUTRAL_STROKE,
                               style, head, detour))

    if spec.get("title"):
        min_x = min(el["x"] for el in out)
        min_y = min(el["y"] for el in out)
        title = scene.text_element("title", str(spec["title"]), min_x, min_y - 70,
                                   FONT_SIZES["xl"], scene.style["font"])
        out.insert(0, title)

    for note in spec.get("notes") or []:
        anchor = shapes.get(str(note.get("at")))
        if anchor is None:
            continue
        idx = len(out)
        out.append(scene.text_element(
            f"note/{note.get('at')}", str(note.get("text") or ""),
            anchor["x"], anchor["y"] + anchor["height"] + 8,
            FONT_SIZES["sm"], scene.style["font"], stroke=ANNOTATION_GREY))
        del idx

    for el in out:
        if el["type"] == "arrow":
            span = max(abs(el["width"]), abs(el["height"]))
            if span > MAX_LINEAR_PX:
                raise SpecError(f"arrow {el['id']} spans {span}px; above {MAX_LINEAR_PX} "
                                "Excalidraw deletes it and substitutes a stub")

    return {
        "type": "excalidraw",
        "version": 2,
        "source": "https://excalidraw.com",
        "elements": out,
        "appState": {"viewBackgroundColor": "#ffffff", "gridSize": 20},
        "files": {},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a .excalidraw file from a spec.")
    ap.add_argument("spec", type=Path, help="spec file (.yaml or .json)")
    ap.add_argument("-o", "--output", type=Path, help="output .excalidraw (default: alongside)")
    ap.add_argument("--stamp-now", action="store_true",
                    help="use the real clock for created/updated instead of a fixed epoch, "
                         "which gives up byte-for-byte reproducibility")
    args = ap.parse_args()
    try:
        spec, raw = load_spec(args.spec)
        stamp = FIXED_EPOCH
        if args.stamp_now:
            import time  # noqa: PLC0415 - only needed on this branch
            stamp = int(time.time() * 1000)
        scene = build(spec, raw, stamp)
    except SpecError as exc:
        print(f"spec error: {exc}", file=sys.stderr)
        return 2
    out = args.output or args.spec.with_suffix(".excalidraw")
    out.write_text(json.dumps(scene, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{out}  ({len(scene['elements'])} elements)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
