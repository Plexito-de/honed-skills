#!/usr/bin/env python3
"""Lint a .excalidraw file before anybody looks at it. Standard library only.

WHY LINT BEFORE RENDERING
-------------------------
Almost every way to get this format wrong produces a file that OPENS and looks fine. A one-sided
binding renders correctly and detaches on the first drag. A wrong text width clips glyphs silently.
A hand-written `index` blanks the canvas with the error swallowed. Eyeballing a render cannot catch
any of those, and the one published A/B on seeded defects found geometric checks caught 12 of 12
with no false positives against 11 of 12 with one false positive for looking at renders.

So: lint, then look. Both, in that order.

SEVERITY MEANS SOMETHING
------------------------
error   the file is wrong: it will look broken, break on interaction, or drop an element
warn    the file is valid but breaks a rule that measurement associates with bad diagrams
note    worth a glance, commonly fine

Exit 0 when there is no error, 1 when there is, 2 when the file cannot be read or the linter
itself fails. `--strict` promotes
warnings to errors.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import math
import sys
from pathlib import Path
from unittest.mock import patch

ASSETS = Path(__file__).resolve().parent.parent / "assets"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

VALID_TYPES = {"rectangle", "diamond", "ellipse", "text", "line", "arrow", "freedraw",
               "image", "frame", "magicframe", "iframe", "embeddable"}
LINEAR = {"line", "arrow", "freedraw"}
ADAPTIVE = {"rectangle", "image", "iframe", "embeddable"}
PROPORTIONAL = {"line", "arrow", "diamond"}
FORBIDDEN_KEYS = {"focus", "gap", "strokeSharpness", "boundElementIds"}
FONT_LADDER = {16, 20, 28, 36}
MAX_LINEAR_PX = 75_000
MIN_NODE_W, MIN_NODE_H = 50, 20
BOUND_TEXT_PADDING = 5


class Finding:
    __slots__ = ("severity", "rule", "element", "message")

    def __init__(self, severity: str, rule: str, element: str, message: str):
        self.severity, self.rule, self.element, self.message = severity, rule, element, message


def load_metrics() -> dict | None:
    try:
        data = json.loads((ASSETS / "font-metrics.json").read_text(encoding="utf-8"))
    except OSError:
        return None
    return {int(f["id"]): f for f in data["families"].values()}


def measure(metrics: dict, text: str, font_id: int, font_size: float) -> tuple[float, float]:
    fam = metrics.get(int(font_id)) or next(iter(metrics.values()))
    widths, kern, fallback = fam["widths"], fam["kern"], fam["fallbackWidth"]
    best = 0.0
    for line in text.split("\n"):
        total, previous = 0.0, ""
        for ch in line:
            total += widths.get(ch, fallback)
            if previous:
                total += kern.get(previous + ch, 0.0)
            previous = ch
        best = max(best, total * font_size)
    height = font_size * fam["lineHeight"] * max(1, len(text.split("\n")))
    return best, height


def finite(*values) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def boxes_overlap(a: dict, b: dict, slack: float = 0.0) -> bool:
    return not (a["x"] + a["width"] <= b["x"] + slack or b["x"] + b["width"] <= a["x"] + slack
                or a["y"] + a["height"] <= b["y"] + slack
                or b["y"] + b["height"] <= a["y"] + slack)


def segment_hits_box(p1, p2, box, inset: float = 2.0) -> bool:
    """Does the segment cross the box interior, shrunk by `inset`? Solved exactly by
    segment_hits_rect, which replaced a 64-point sample that could step over a thin box."""
    return segment_hits_rect(p1, p2, box["x"] + inset, box["y"] + inset,
                             box["x"] + box["width"] - inset, box["y"] + box["height"] - inset)
    for i in range(1, 64):
        t = i / 64
        px = p1[0] + (p2[0] - p1[0]) * t
        py = p1[1] + (p2[1] - p1[1]) * t
        if x0 < px < x1 and y0 < py < y1:
            return True
    return False


# ---------------------------------------------------------------- strokes, solved exactly
#
# Crossings are the one layout aesthetic with measured weight: Purchase 1997 found them significant
# for both errors and response time and different from every other aesthetic tested, so they are
# a warn. Bends were significant for errors only and have no measured threshold, so there is no
# bend rule. A multi-point arrow, rounded or not, is tested as its polyline: the rounding is drawn
# within a few px of the corner and does not move a crossing.

EPS = 0.5      # px: two points closer than this are one point
RING = 8.0     # px: a crossing this close to an end that both strokes bind to one shape is a fan-out
STROKES = {"arrow", "line"}


def orient(a, b, c) -> float:
    """Twice the signed area of triangle abc: > 0 counter-clockwise, < 0 clockwise."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def distance(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def point_to_segment(pt, a, b) -> float:
    """Distance from a point to the closed segment ab."""
    length2 = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2
    if length2 < 1e-18:
        return distance(pt, a)
    t = ((pt[0] - a[0]) * (b[0] - a[0]) + (pt[1] - a[1]) * (b[1] - a[1])) / length2
    t = max(0.0, min(1.0, t))
    return distance(pt, (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))


def segment_intersection(p1, p2, q1, q2) -> tuple[str, list[tuple[float, float]]]:
    """Exact segment-segment intersection by orientation tests.

    Returns ("none", []), ("point", [p]) for a crossing or a touch, or ("overlap", [a, b]) when the
    two are collinear and share a piece longer than EPS. Collinearity is judged by distance to the
    other line, so a stroke 0.3 px off a trunk still counts as on it.
    """
    lp, lq = distance(p1, p2), distance(q1, q2)
    if lp < 1e-9 or lq < 1e-9:
        return "none", []
    d1, d2 = orient(q1, q2, p1), orient(q1, q2, p2)
    d3, d4 = orient(p1, p2, q1), orient(p1, p2, q2)
    near = (abs(d1) / lq < EPS, abs(d2) / lq < EPS, abs(d3) / lp < EPS, abs(d4) / lp < EPS)
    if all(near):
        ux, uy = (p2[0] - p1[0]) / lp, (p2[1] - p1[1]) / lp
        def along(pt) -> float:
            return (pt[0] - p1[0]) * ux + (pt[1] - p1[1]) * uy
        lo = max(0.0, min(along(q1), along(q2)))
        hi = min(lp, max(along(q1), along(q2)))
        if hi - lo > EPS:
            return "overlap", [(p1[0] + ux * lo, p1[1] + uy * lo),
                               (p1[0] + ux * hi, p1[1] + uy * hi)]
        if hi - lo >= -EPS:
            mid = (lo + hi) / 2
            return "point", [(p1[0] + ux * mid, p1[1] + uy * mid)]
        return "none", []
    # An endpoint lying on the other segment is a touch, reported as a point.
    for flag, pt, a, b in ((near[0], p1, q1, q2), (near[1], p2, q1, q2),
                           (near[2], q1, p1, p2), (near[3], q2, p1, p2)):
        if flag and min(a[0], b[0]) - EPS <= pt[0] <= max(a[0], b[0]) + EPS \
                and min(a[1], b[1]) - EPS <= pt[1] <= max(a[1], b[1]) + EPS:
            return "point", [pt]
    if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0):
        t = d1 / (d1 - d2)
        return "point", [(p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t)]
    return "none", []


def segment_hits_rect(p1, p2, x0: float, y0: float, x1: float, y1: float) -> bool:
    """Liang-Barsky: does the segment enter the open rectangle? Exact, unlike the sampled test."""
    if x1 <= x0 or y1 <= y0:
        return False
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    lo, hi = 0.0, 1.0
    for p, q in ((-dx, p1[0] - x0), (dx, x1 - p1[0]), (-dy, p1[1] - y0), (dy, y1 - p1[1])):
        if abs(p) < 1e-12:
            if q <= 0:
                return False
            continue
        r = q / p
        if p < 0:
            lo = max(lo, r)
        else:
            hi = min(hi, r)
        if lo >= hi:
            return False
    return True


def stroke_list(elements: list[dict]) -> list[dict]:
    """Every arrow and line as an absolute polyline plus the shape each end is bound to."""
    out = []
    for el in elements:
        pts = el.get("points") or []
        if el.get("type") not in STROKES or len(pts) < 2 or not finite(el.get("x"), el.get("y")):
            continue
        poly = [(el["x"] + p[0], el["y"] + p[1]) for p in pts
                if isinstance(p, list) and len(p) == 2 and finite(*p)]
        ends = []
        for side in ("startBinding", "endBinding"):
            binding = el.get(side)
            ends.append(binding.get("elementId") if isinstance(binding, dict) else None)
        if len(poly) < 2:
            continue                     # malformed points: points-count and points-origin say so
        out.append({"id": el.get("id", "?"), "poly": poly, "start": ends[0], "end": ends[1]})
    return out


def stroke_conflicts(elements: list[dict]):
    """(crossings, overlaps): crossings as (a, b, points), overlaps as (a, b).

    Exempt, each because the picture reads as intended rather than tangled:
      - a touch: the point is an END of either polyline (a bus spur meeting its trunk);
      - a fan-out or fan-in: the point is within RING of an end of each stroke, and those two ends
        bind to the same shape;
      - a bus trunk: a collinear overlap of two strokes that share exactly ONE bound end, the
        start (a trunk that later splits) or the end (spurs merging into one target). Two strokes
        that share both ends are a duplicate drawn on top of itself, and are reported;
      - any point on an overlap piece, which is reported once as the overlap, not again.
    """
    strokes = stroke_list(elements)
    crossings, overlaps = [], []
    for i, a in enumerate(strokes):
        for b in strokes[i + 1:]:
            pieces, points, bad_overlap = [], [], False
            hits = []
            for p1, p2 in zip(a["poly"], a["poly"][1:]):
                for q1, q2 in zip(b["poly"], b["poly"][1:]):
                    kind, where = segment_intersection(p1, p2, q1, q2)
                    if kind == "overlap":
                        pieces.append(where)
                        same_start = bool(a["start"]) and a["start"] == b["start"]
                        same_end = bool(a["end"]) and a["end"] == b["end"]
                        if same_start == same_end:
                            bad_overlap = True
                    elif kind == "point":
                        hits.append(where[0])
            if bad_overlap:
                overlaps.append((a["id"], b["id"]))
            ends_a = ((a["poly"][0], a["start"]), (a["poly"][-1], a["end"]))
            ends_b = ((b["poly"][0], b["start"]), (b["poly"][-1], b["end"]))
            for pt in hits:
                if any(distance(pt, e) < EPS for e, _ in ends_a + ends_b):
                    continue
                if any(ta and ta == tb and distance(pt, ea) <= RING and distance(pt, eb) <= RING
                       for ea, ta in ends_a for eb, tb in ends_b):
                    continue
                if any(point_to_segment(pt, s, e) < EPS for s, e in pieces):
                    continue
                if not any(distance(pt, seen) < EPS for seen in points):
                    points.append(pt)
            if points:
                crossings.append((a["id"], b["id"], points))
    return crossings, overlaps


def count_crossings(scene: dict) -> int:
    elements = scene.get("elements") if isinstance(scene.get("elements"), list) else []
    return sum(len(pts) for _, _, pts in stroke_conflicts(elements)[0])


# ---------------------------------------------------------------- C4 and provenance

C4_TAG = "c4model checklist"
C4_TOO_MANY = 20
# A relationship label that says nothing about intent. Compared against the WHOLE label after the
# bracketed technology line and punctuation are stripped, so "uses the cache" passes.
VAGUE_EDGES = {"uses", "calls", "connects to", "talks to", "depends on", "interacts with"}
HEX = set("0123456789abcdefABCDEF")


def custom(el: dict) -> dict:
    data = el.get("customData")
    return data if isinstance(data, dict) else {}


def plain_label(text: str) -> str:
    """Lower case, the [technology] line removed, punctuation and whitespace collapsed."""
    out, depth = [], 0
    for ch in text:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif not depth:
            out.append(ch.lower() if ch.isalnum() else " ")
    return " ".join("".join(out).split())


def lint(scene: dict, metrics: dict | None) -> list[Finding]:
    out: list[Finding] = []

    def add(sev, rule, el, msg):
        out.append(Finding(sev, rule, el, msg))

    if scene.get("type") != "excalidraw":
        add("error", "file-type", "-",
            f"top-level type is {scene.get('type')!r}; Excalidraw rejects anything but "
            "'excalidraw' with 'Error: invalid file'")
    if not isinstance(scene.get("elements"), list):
        add("error", "file-elements", "-", "elements is not an array, which the loader rejects")
        return out

    elements = scene["elements"]
    by_id: dict[str, dict] = {}
    for el in elements:
        eid = el.get("id")
        if not eid:
            add("error", "id-missing", "-",
                f"a {el.get('type')} element has no id: a missing id crashes hit-testing and "
                "blanks the whole canvas")
            continue
        if eid in by_id:
            add("error", "id-duplicate", eid,
                "duplicate id: the second copy is re-randomised on load, so every containerId, "
                "boundElements entry, frameId and binding pointing at it dangles")
        by_id[eid] = el

    # ---- per element ----
    for el in elements:
        eid = el.get("id", "?")
        kind = el.get("type")
        if kind not in VALID_TYPES:
            add("error", "type-unknown", eid,
                f"type {kind!r} is not a known element type; restoreElement has no default case "
                "so it is dropped silently with no error anywhere")
            continue
        for key in FORBIDDEN_KEYS:
            if key in el:
                add("error", "legacy-key", eid,
                    f"{key!r} is a legacy field; it is migrated then deleted, and writing it "
                    "means the file was built against an old schema")
        if not finite(el.get("x"), el.get("y"), el.get("width"), el.get("height")):
            add("error", "non-finite", eid, "x, y, width or height is NaN, Infinity or missing")
            continue
        if el["x"] == 0 or el["y"] == 0:
            add("warn", "zero-coordinate", eid,
                "a coordinate is exactly 0. Three upstream falsy-zero bugs are open on this: "
                "`frame?.x || minX`, `updates.x || element.x` and the arrow-endpoint default all "
                "treat 0 as absent")
        if kind not in LINEAR and el["width"] == 0 and el["height"] == 0:
            add("error", "zero-size", eid,
                "width and height are both 0, so isInvisiblySmallElement marks it deleted")
        opacity = el.get("opacity")
        if opacity is not None and (not finite(opacity) or not 0 <= opacity <= 100):
            add("error", "opacity-range", eid,
                f"opacity is {opacity}, outside the 0 to 100 range")
        elif isinstance(opacity, float) and 0 < opacity < 1:
            # The nastiest of the three, because the value is VALID: 0.5 is a legal opacity that
            # means half a percent, so the element renders essentially invisible and nothing
            # complains. A fraction under 1 is always a 0-to-1 scale mistake in practice.
            add("error", "opacity-range", eid,
                f"opacity is {opacity}, which is legal but means {opacity}% and renders the "
                "element invisible. The scale is 0 to 100, not 0 to 1")
        elif opacity not in (None, 100):
            add("warn", "opacity-not-100", eid,
                f"opacity {opacity}: 19 of 20 real published scenes use 100 throughout. Get "
                "hierarchy from colour, size and whitespace, or dim deliberately and say so")
        if el.get("angle"):
            add("warn", "rotated", eid,
                "angle is non-zero. 14 of 20 real scenes have zero rotated elements; the "
                "hand-drawn feel comes from roughness, not tilt")
        roundness = el.get("roundness")
        if roundness is not None:
            rtype = roundness.get("type") if isinstance(roundness, dict) else None
            if kind in ADAPTIVE and rtype != 3:
                add("error", "roundness-class", eid,
                    f"{kind} needs roundness type 3; type {rtype} yields radius 0 silently")
            elif kind in PROPORTIONAL and rtype != 2:
                add("error", "roundness-class", eid,
                    f"{kind} needs roundness type 2; type {rtype} yields radius 0 silently")
            elif kind in ("ellipse", "text"):
                add("error", "roundness-class", eid,
                    f"{kind} must have roundness null, not {roundness}")
        if el.get("seed") in (None, 0, 1):
            add("warn", "seed-default", eid,
                "seed is missing, 0 or 1. Every element then shares seed 1 and the hand-drawn "
                "strokes wobble in lockstep, which reads as synthetic")
        if "index" in el and el["index"] not in (None,):
            add("warn", "index-written", eid,
                "index is set. Omit it: a partially-invalid set throws 'invalid order key', the "
                "throw is swallowed, and the canvas renders blank")

        if kind in LINEAR:
            points = el.get("points")
            if not isinstance(points, list) or len(points) < 2:
                add("error", "points-count", eid,
                    "a linear element needs at least two points or it is marked deleted")
            else:
                first = points[0]
                if not (isinstance(first, list) and len(first) == 2
                        and abs(first[0]) < 1e-9 and abs(first[1]) < 1e-9):
                    add("error", "points-origin", eid,
                        f"points[0] is {first}, not [0, 0]. Restore subtracts the first point "
                        "from every point and adds it to x/y, so the element visibly jumps")
                span = max(abs(el["width"]), abs(el["height"]))
                if span > MAX_LINEAR_PX:
                    add("error", "linear-too-large", eid,
                        f"spans {span:.0f}px; above {MAX_LINEAR_PX} Excalidraw deletes it and "
                        "substitutes a 100x100 stub")
        if kind == "arrow" and "endArrowhead" not in el:
            add("error", "arrowhead-implicit", eid,
                "endArrowhead is absent, which defaults to 'arrow'. Write it explicitly, null "
                "included, so the file says what it means")
        if kind == "freedraw" and "simulatePressure" not in el:
            add("warn", "freedraw-pressure", eid,
                "simulatePressure has no restore default, so it stays undefined and the stroke "
                "renders without taper")

        if kind == "text":
            if el.get("lineHeight") in (None, 0):
                add("error", "lineheight-missing", eid,
                    "lineHeight is missing. detectLineHeight then back-derives it from height, "
                    "permanently distorting multi-line spacing")
            if el.get("originalText") is None:
                add("warn", "originaltext-missing", eid,
                    "originalText is absent; the label reflows the moment somebody edits it")
            size = el.get("fontSize")
            if isinstance(size, float) and abs(size - round(size)) > 1e-9:
                add("warn", "fontsize-fractional", eid,
                    f"fontSize {size} is fractional, which comes from dragging a text box. It is "
                    "the most reliable machine-detectable tell of an unmade diagram")
            elif size not in FONT_LADDER:
                add("note", "fontsize-off-ladder", eid,
                    f"fontSize {size} is off the 16/20/28/36 ladder")
            if metrics and el.get("text") is not None and finite(size):
                mw, mh = measure(metrics, el["text"], el.get("fontFamily", 5), size)
                if el.get("autoResize", True):
                    if el["width"] + 0.6 < mw:
                        add("error", "text-too-narrow", eid,
                            f"stored width {el['width']:.2f} is under the measured advance "
                            f"{mw:.2f}. Excalidraw never re-measures on open, so the glyphs "
                            "overflow and exports clip them")
                    elif el["width"] > mw + 2:
                        add("warn", "text-too-wide", eid,
                            f"stored width {el['width']:.2f} exceeds the measured {mw:.2f}, so "
                            "centred and right-aligned text sits off-centre")
                if abs(el["height"] - mh) > 0.6:
                    add("error", "text-height", eid,
                        f"height {el['height']:.2f} is not fontSize * lineHeight * lineCount "
                        f"({mh:.2f})")

    # ---- reciprocal links ----
    for el in elements:
        eid = el.get("id", "?")
        container_id = el.get("containerId")
        if container_id:
            container = by_id.get(container_id)
            if container is None:
                add("error", "container-dangling", eid,
                    f"containerId {container_id} does not exist, so the label becomes free "
                    "floating text at its literal x/y")
            elif not any(b.get("id") == eid and b.get("type") == "text"
                         for b in container.get("boundElements") or []):
                add("error", "binding-one-sided", eid,
                    f"this text names container {container_id}, but that container does not list "
                    "it in boundElements. Restore repairs this one, but write both sides")
        for side in ("startBinding", "endBinding"):
            binding = el.get(side)
            if not binding:
                continue
            if el.get("type") != "arrow":
                add("error", "binding-non-arrow", eid,
                    f"{side} on a {el.get('type')}; only an arrow can bind, and restore forces "
                    "it to null")
                continue
            target = by_id.get(binding.get("elementId"))
            if target is None:
                add("error", "binding-dangling", eid,
                    f"{side} points at {binding.get('elementId')}, which does not exist. The "
                    "binding is silently nulled and the arrow free-floats")
                continue
            if not any(b.get("id") == eid and b.get("type") == "arrow"
                       for b in target.get("boundElements") or []):
                add("error", "binding-one-sided", eid,
                    f"{side} names {target['id']}, but that shape does not list this arrow in "
                    "boundElements. The file opens fine and the arrow DETACHES the first time "
                    "the shape is dragged, because updateBoundElements walks the boundElements list on the shape")
            fp = binding.get("fixedPoint")
            if not (isinstance(fp, list) and len(fp) == 2 and finite(*fp)):
                add("error", "fixedpoint-shape", eid,
                    f"{side}.fixedPoint is {fp}; it must be two finite numbers or the endpoint "
                    "jumps to the shape centre")
            else:
                for v in fp:
                    if abs(v - 0.5) < 1e-4:
                        add("error", "fixedpoint-half", eid,
                            f"{side}.fixedPoint has a coordinate at exactly 0.5, which is "
                            "snapped off centre on load and can flip the arrow heading. Write "
                            "0.5002: 0.5001 is still inside the 1e-4 guard in IEEE 754")
                    if not -10 <= v <= 10:
                        add("error", "fixedpoint-range", eid,
                            f"{side}.fixedPoint {v} is outside the clamp of -10 to 10")
            if binding.get("mode") not in ("orbit", "inside", "skip"):
                add("warn", "binding-mode", eid,
                    f"{side}.mode is {binding.get('mode')!r}; a falsy mode sends restore down the "
                    "legacy migration path, which discards your numbers")
        for entry in el.get("boundElements") or []:
            if entry.get("id") not in by_id:
                add("error", "boundelements-dangling", eid,
                    f"boundElements names {entry.get('id')}, which does not exist")
        if el.get("frameId") and el["frameId"] not in by_id:
            add("error", "frame-dangling", eid, f"frameId {el['frameId']} does not exist")

    # bound text must sit immediately after its container, or restore reorders and re-indexes
    order = {el.get("id"): i for i, el in enumerate(elements)}
    for el in elements:
        if el.get("type") == "text" and el.get("containerId") in order:
            if order[el["id"]] != order[el["containerId"]] + 1:
                add("warn", "bound-text-order", el.get("id", "?"),
                    "bound text is not immediately after its container; restore reorders the "
                    "array and rewrites every index, so any order you wrote is lost")

    # ---- geometry ----
    shapes = [e for e in elements
              if e.get("type") in ("rectangle", "diamond", "ellipse", "image")
              and not e.get("containerId") and finite(e.get("x"), e.get("y"))]
    for i, a in enumerate(shapes):
        for b in shapes[i + 1:]:
            if boxes_overlap(a, b, slack=1.0):
                # A boundary deliberately contains its children, so only flag a partial overlap.
                contained = (a["x"] <= b["x"] and a["y"] <= b["y"]
                             and a["x"] + a["width"] >= b["x"] + b["width"]
                             and a["y"] + a["height"] >= b["y"] + b["height"])
                reverse = (b["x"] <= a["x"] and b["y"] <= a["y"]
                           and b["x"] + b["width"] >= a["x"] + a["width"]
                           and b["y"] + b["height"] >= a["y"] + a["height"])
                if not contained and not reverse:
                    add("error", "node-overlap", f"{a['id']}/{b['id']}",
                        "two nodes overlap without one containing the other")
    if len(shapes) > 1:
        xs = [s["x"] for s in shapes]
        ys = [s["y"] for s in shapes]
        if max(xs) - min(xs) < 1 and max(ys) - min(ys) < 1:
            add("error", "all-stacked", "-",
                "every shape sits at the same coordinate: the layout did not run")
    for a in shapes:
        if a["width"] < MIN_NODE_W or a["height"] < MIN_NODE_H:
            add("warn", "node-too-small", a["id"],
                f"{a['width']:.0f}x{a['height']:.0f} is below {MIN_NODE_W}x{MIN_NODE_H}, so "
                "adjustRoughness() halves its roughness and it renders smoother than its "
                "neighbours")
    for arrow in (e for e in elements if e.get("type") == "arrow"):
        pts = arrow.get("points") or []
        if len(pts) < 2 or not finite(arrow.get("x"), arrow.get("y")):
            continue
        bound = {arrow.get(s, {}).get("elementId") for s in ("startBinding", "endBinding")
                 if isinstance(arrow.get(s), dict)}
        absolute = [(arrow["x"] + p[0], arrow["y"] + p[1]) for p in pts if len(p) == 2]
        for p1, p2 in zip(absolute, absolute[1:]):
            for shape in shapes:
                if shape["id"] in bound:
                    continue
                if segment_hits_box(p1, p2, shape):
                    add("warn", "arrow-through-node", arrow["id"],
                        f"passes through {shape['id']}, which it is not bound to. Excalidraw's "
                        "own routing rule is that the path must avoid connected shapes along its "
                        "whole length")
                    break

    # ---- strokes against strokes ----
    crossings, overlaps = stroke_conflicts(elements)
    for a_id, b_id, points in crossings:
        first = points[0]
        add("warn", "arrow-crossing", f"{a_id}/{b_id}",
            f"{len(points)} crossing(s), first at ({first[0]:.0f}, {first[1]:.0f}). Crossings are "
            "the layout aesthetic with the strongest measured effect on reading errors and time "
            "(Purchase 1997). Reorder, reroute, or split the diagram")
    for a_id, b_id in overlaps:
        add("warn", "arrow-overlap", f"{a_id}/{b_id}",
            "two strokes run along one line and do not end at the same shape, so nobody can "
            "trace which is which. That is worse than a crossing. Offset one, or merge them into "
            "a bus that ends at one target")
    stroke_ids = {s["id"] for s in stroke_list(elements)}
    arrow_labels = [e for e in elements if e.get("type") == "text"
                    and e.get("containerId") in stroke_ids
                    and finite(e.get("x"), e.get("y"), e.get("width"), e.get("height"))]
    for stroke in stroke_list(elements):
        for label in arrow_labels:
            if label["containerId"] == stroke["id"]:
                continue
            x0, y0 = label["x"] + 1, label["y"] + 1
            x1, y1 = label["x"] + label["width"] - 1, label["y"] + label["height"] - 1
            if any(segment_hits_rect(p1, p2, x0, y0, x1, y1)
                   for p1, p2 in zip(stroke["poly"], stroke["poly"][1:])):
                owner = label["containerId"]
                add("warn", "arrow-through-label", stroke["id"],
                    f"passes through the label of {owner}. The label backing punches a hole only "
                    "in its own arrow, so this stroke is drawn over the text")

    # ---- C4, only when the scene says it is C4 ----
    if any(custom(e).get("c4") for e in elements):
        bound_text = {e.get("containerId"): str(e.get("text") or "") for e in elements
                      if e.get("type") == "text" and e.get("containerId")}

        def contains(outer: dict, inner: dict) -> bool:
            return (outer is not inner and outer["x"] <= inner["x"] and outer["y"] <= inner["y"]
                    and outer["x"] + outer["width"] >= inner["x"] + inner["width"]
                    and outer["y"] + outer["height"] >= inner["y"] + inner["height"])

        containers = 0
        for s in shapes:
            c4 = custom(s).get("c4")
            boundary = c4 == "boundary" or any(contains(s, other) for other in shapes)
            if not c4 and not boundary:
                add("note", "c4-untyped", s["id"],
                    f"{C4_TAG}: this shape has no C4 type. Say whether it is a person, a "
                    "software system, a container or a component")
            if c4 in ("container", "component") and not custom(s).get("tech"):
                add("note", "c4-no-tech", s["id"],
                    f"{C4_TAG}: a {c4} with no technology. Name it, for example Postgres or "
                    "a Python web app")
            if c4 == "container":
                containers += 1
        for arrow in (e for e in elements if e.get("type") == "arrow"):
            text = bound_text.get(arrow.get("id"), "")
            said = plain_label(text)
            if not said:
                add("note", "c4-vague-edge", arrow.get("id", "?"),
                    f"{C4_TAG}: a relationship with no label. Say what flows and why")
            elif said in VAGUE_EDGES:
                add("note", "c4-vague-edge", arrow.get("id", "?"),
                    f"{C4_TAG}: the label {said!r} names no intent. Say what is read, written "
                    "or sent")
        titled = any(custom(e).get("role") == "title" for e in elements)
        if not titled and shapes:
            top = min(s["y"] for s in shapes)
            titled = any(e.get("type") == "text" and not e.get("containerId")
                         and finite(e.get("y"), e.get("height"))
                         and e["y"] + e["height"] <= top for e in elements)
        if not titled:
            add("note", "c4-no-title", "-",
                f"{C4_TAG}: no title. Name the diagram type and its scope above the shapes")
        if containers >= C4_TOO_MANY:
            add("note", "c4-too-many", "-",
                f"{C4_TAG}: {containers} containers. At {C4_TOO_MANY} or more, split the "
                "diagram by zoom level or by subsystem")

    # ---- provenance, only when the scene declares a source ----
    source = next((custom(e).get("provenance") for e in elements
                   if custom(e).get("role") == "title"
                   and isinstance(custom(e).get("provenance"), dict)), None)
    if source is not None:
        commit = str(source.get("commit") or "")
        if not 7 <= len(commit) <= 64 or not set(commit) <= HEX:
            add("error", "provenance-commit", "-",
                f"source commit {commit!r} is not 7 to 64 hex characters, so no reader can "
                "check out what this diagram claims")
        for el in shapes + [e for e in elements if e.get("type") == "arrow"]:
            prov = custom(el).get("provenance")
            if not (isinstance(prov, dict) and prov.get("at")):
                add("warn", "provenance-missing", el.get("id", "?"),
                    "the diagram declares a source, but this element cites no `at`. Point it at "
                    "path:lines, or write `at: external` for a box outside the code")

    # text overflowing its container
    for el in elements:
        if el.get("type") != "text" or not el.get("containerId"):
            continue
        container = by_id.get(el["containerId"])
        if container is None or not finite(container.get("width")):
            continue
        ctype = container.get("type")
        if ctype == "ellipse":
            allowance = round(container["width"] / 2 * math.sqrt(2)) - BOUND_TEXT_PADDING * 2
        elif ctype == "diamond":
            allowance = round(container["width"] / 2) - BOUND_TEXT_PADDING * 2
        elif ctype == "arrow":
            continue
        else:
            allowance = container["width"] - BOUND_TEXT_PADDING * 2
        if el.get("width", 0) > allowance + 1:
            add("error", "text-overflows-container", el["id"],
                f"text is {el['width']:.1f} wide but the {ctype} allows {allowance:.1f}. For an "
                "ellipse or diamond the allowance is the inscribed box, not the full width")

    # ---- style discipline ----
    drawn = [e for e in elements if e.get("type") != "text"]
    if drawn:
        for prop, rule, limit, why in (
            ("roughness", "many-roughness", 1,
             "20 of 20 real published scenes use exactly one roughness value, and every drawing "
             "that mixes them is in the ugly pile"),
            ("fillStyle", "many-fillstyle", 1,
             "17 of 20 real scenes use exactly one fill style"),
            ("strokeWidth", "many-strokewidth", 2,
             "16 of 20 real scenes use exactly one stroke width; body content at 1 or 2 and at "
             "most one emphasis step"),
        ):
            values = {e.get(prop) for e in drawn if e.get(prop) is not None}
            if len(values) > limit:
                add("warn", rule, "-",
                    f"{len(values)} distinct {prop} values {sorted(values)}: {why}")
        strokes = {e.get("strokeColor") for e in drawn if e.get("strokeColor")}
        if len(strokes) > 2:
            add("warn", "many-stroke-colours", "-",
                f"{len(strokes)} stroke colours. Median across 20 real scenes is 1.5, and 10 of "
                "20 use exactly one; the two ugliest had 14 and 15")
        fills = {e.get("backgroundColor") for e in drawn
                 if e.get("backgroundColor") not in (None, "transparent")}
        if len(fills) > 5:
            add("warn", "many-fills", "-",
                f"{len(fills)} background colours against a budget of 3 to 5, each with a stated "
                "job. Past five, ship a legend")
        dashed = [e for e in drawn if e.get("strokeStyle") in ("dashed", "dotted")]
        if len(dashed) > 3:
            add("note", "many-dashed", "-",
                f"{len(dashed)} dashed or dotted elements. They are 1.4% of strokes in the "
                "published corpus, so when almost everything is dashed, dashed means nothing")
    sizes = {e.get("fontSize") for e in elements if e.get("type") == "text"}
    if len(sizes) > 3:
        add("warn", "many-fontsizes", "-",
            f"{len(sizes)} text sizes {sorted(s for s in sizes if s)}: median in real scenes is 2")
    if len(shapes) > 15:
        add("note", "too-many-nodes", "-",
            f"{len(shapes)} primary nodes. The clean real scenes run 6 to 12; above about 15, "
            "split the diagram or ship graded detail levels")
    if scene.get("appState", {}).get("viewBackgroundColor", "#ffffff") != "#ffffff":
        add("note", "canvas-not-white", "-",
            "canvas is not white; 20 of 20 real published scenes are")
    return out


# The two shipped examples, as JSON-able dicts, so the clean cases run on a bare python3 with no
# PyYAML. When PyYAML is present the self-test asserts these still equal examples/*.yaml.
EXAMPLE_SPECS = {
    "request-path": {
        "type": "flow", "title": "Request path", "style": "architecture", "fontSize": "md",
        "nodes": [{"id": "browser", "label": "Browser"}, {"id": "cdn", "label": "CDN"},
                  {"id": "lb", "label": "Load balancer"},
                  {"id": "api", "label": "API", "color": "blue"},
                  {"id": "cache", "label": "Redis"}, {"id": "db", "label": "Postgres"}],
        "edges": [{"from": "browser", "to": "cdn"}, {"from": "cdn", "to": "lb"},
                  {"from": "lb", "to": "api"},
                  {"from": "api", "to": "cache", "label": "read"},
                  {"from": "api", "to": "db", "label": "write"},
                  {"from": "cache", "to": "db", "label": "miss", "style": "dashed"}],
        "notes": [{"at": "api", "text": "rate-limited, 100 rps"}],
    },
    "c4": {
        "type": "c4", "title": "Online shop, containers", "style": "architecture",
        "fontSize": "md",
        "source": {"repo": "example.org/acme/shop",
                   "commit": "3f9a2c1d8e7b6a5f4c3d2e1f0a9b8c7d6e5f4a3b"},
        "nodes": [
            {"id": "customer", "label": "Customer", "c4": "person", "at": "external"},
            {"id": "web", "label": "Web shop", "c4": "container", "tech": "TypeScript, React",
             "at": "web/src/App.tsx:1-80"},
            {"id": "api", "label": "Orders API", "c4": "container", "tech": "Python, FastAPI",
             "at": "api/orders/routes.py:12-140"},
            {"id": "db", "label": "Orders database", "c4": "container", "tech": "Postgres",
             "shape": "database", "at": "api/orders/models.py:1-60"},
            {"id": "pay", "label": "Payment provider", "c4": "system", "external": True,
             "at": "external"}],
        "edges": [
            {"from": "customer", "to": "web", "label": "Places orders", "tech": "HTTPS",
             "at": "external"},
            {"from": "web", "to": "api", "label": "Submits orders", "tech": "JSON over HTTPS",
             "at": "web/src/api/client.ts:20-44"},
            {"from": "api", "to": "db", "label": "Reads and writes orders", "tech": "SQL",
             "at": "api/orders/repository.py:8-90"},
            {"from": "api", "to": "pay", "label": "Charges cards", "tech": "HTTPS",
             "at": "api/payments/client.py:15-52"}],
    },
}


def stroke_cases(case, base_scene, text_el, failures: list[str], checked: list[int]) -> None:
    """arrow-crossing, arrow-overlap and arrow-through-label: each fires on its defect and stays
    silent on the layouts that look like it and are fine (fan-out, fan-in bus, merging trunk)."""
    template = base_scene()["elements"][0]

    def box(eid: str, x: float, y: float) -> dict:
        return dict(template, id=eid, x=x, y=y, seed=sum(map(ord, eid)) + 10, boundElements=[])

    def stroke(scene: dict, eid: str, points, src=None, dst=None, kind="arrow") -> dict:
        x0, y0 = points[0]
        rel = [[round(px - x0, 2), round(py - y0, 2)] for px, py in points]
        xs, ys = [p[0] for p in rel], [p[1] for p in rel]
        el = dict(template, id=eid, type=kind, x=x0, y=y0, width=max(xs) - min(xs),
                  height=max(ys) - min(ys), roundness={"type": 2}, seed=sum(map(ord, eid)) + 20,
                  boundElements=[], points=rel, startArrowhead=None,
                  endArrowhead="arrow" if kind == "arrow" else None,
                  startBinding=None, endBinding=None)
        by_id = {e["id"]: e for e in scene["elements"]}
        for side, target in (("startBinding", src), ("endBinding", dst)):
            if target:
                el[side] = {"elementId": target, "fixedPoint": [0.25, 0.25], "mode": "orbit"}
                by_id[target]["boundElements"] = by_id[target]["boundElements"] + [
                    {"type": "arrow", "id": eid}]
        scene["elements"].append(el)
        return el

    def boxes(scene: dict, *spec) -> None:
        scene["elements"] = [box(*b) for b in spec]

    def x_crossing(scene):
        boxes(scene, ("A", 100, 100), ("B", 500, 100), ("C", 100, 400), ("D", 500, 400))
        stroke(scene, "ad", [(256, 130), (494, 430)], "A", "D")
        stroke(scene, "cb", [(256, 430), (494, 130)], "C", "B")

    def parallel(scene):
        boxes(scene, ("A", 100, 100), ("B", 500, 100), ("C", 100, 400), ("D", 500, 400))
        stroke(scene, "ab", [(256, 130), (494, 130)], "A", "B")
        stroke(scene, "cd", [(256, 430), (494, 430)], "C", "D")

    def fan_out(shared: bool):
        # Two arrows leave A a few px apart and cross 4 to 7 px from their start: within the ring
        # of an end both bind to A, so it is a fan-out. Unbind one start and it is a crossing.
        def inner(scene):
            boxes(scene, ("A", 100, 100), ("B", 400, 100), ("C", 400, 300))
            stroke(scene, "ab", [(256, 132), (394, 130)], "A" if shared else None, "B")
            stroke(scene, "ac", [(256, 126), (394, 330)], "A", "C")
        return inner

    def fan_in_bus(scene):
        boxes(scene, ("S1", 100, 100), ("S2", 100, 250), ("S3", 100, 400), ("H", 600, 250))
        stroke(scene, "spur1", [(256, 130), (450, 130)], kind="line")
        stroke(scene, "spur2", [(256, 280), (450, 280)], kind="line")
        stroke(scene, "spur3", [(256, 430), (450, 430)], kind="line")
        stroke(scene, "trunk", [(450, 130), (450, 430)], kind="line")
        stroke(scene, "tohub", [(450, 280), (594, 280)], None, "H")

    def merging_trunk(scene):
        boxes(scene, ("S1", 100, 100), ("S3", 100, 400), ("H", 600, 250))
        stroke(scene, "s1h", [(256, 130), (450, 130), (450, 280), (594, 280)], "S1", "H")
        stroke(scene, "s3h", [(256, 430), (450, 430), (450, 280), (594, 280)], "S3", "H")

    def reverse_pair(scene):
        boxes(scene, ("box1", 100, 100), ("box2", 400, 100))
        stroke(scene, "ab", [(256, 130), (394, 130)], "box1", "box2")
        stroke(scene, "ba", [(394, 130), (256, 130)], "box2", "box1")

    def duplicate(scene):
        boxes(scene, ("box1", 100, 100), ("box2", 400, 100))
        stroke(scene, "ab", [(256, 130), (394, 130)], "box1", "box2")
        stroke(scene, "ab2", [(256, 130), (394, 130)], "box1", "box2")

    def split_trunk(scene):
        boxes(scene, ("A", 100, 250), ("B", 600, 100), ("C", 600, 400))
        stroke(scene, "ab", [(256, 280), (400, 280), (400, 130), (594, 130)], "A", "B")
        stroke(scene, "ac", [(256, 280), (400, 280), (400, 430), (594, 430)], "A", "C")

    def three_d(scene):
        boxes(scene, ("box1", 100, 100), ("box2", 400, 100))
        stroke(scene, "ab", [(256, 130), (394, 130)], "box1", "box2")
        bad = stroke(scene, "bad", [(256, 150), (394, 150)])
        bad["points"] = [[0, 0, 0], [138, 0, 0]]

    def labelled(cut: bool):
        def inner(scene):
            boxes(scene, ("box1", 100, 100), ("box2", 400, 100))
            arrow = stroke(scene, "ab", [(256, 130), (394, 130)], "box1", "box2")
            scene["elements"].append(text_el(id="lab", x=300, y=100, width=50, height=20,
                                             fontSize=16, containerId="ab"))
            arrow["boundElements"] = [{"type": "text", "id": "lab"}]
            if cut:
                stroke(scene, "cut", [(320, 60), (320, 125)], kind="line")
        return inner

    case("X crossing", x_crossing, "arrow-crossing")
    case("parallel arrows", parallel, "arrow-crossing", want=False)
    case("fan-out from one shape", fan_out(True), "arrow-crossing", want=False)
    case("near-start crossing, different shapes", fan_out(False), "arrow-crossing")
    case("fan-in bus of lines", fan_in_bus, "arrow-crossing", want=False)
    case("fan-in bus of lines", fan_in_bus, "arrow-overlap", want=False)
    case("merging trunk into one target", merging_trunk, "arrow-crossing", want=False)
    case("merging trunk into one target", merging_trunk, "arrow-overlap", want=False)
    case("reverse pair on one line", reverse_pair, "arrow-overlap")
    case("duplicate drawn on itself", duplicate, "arrow-overlap")
    case("trunk that splits after a shared start", split_trunk, "arrow-overlap", want=False)
    case("3-coordinate points, no crash", three_d, "points-origin")
    case("reverse pair on one line", reverse_pair, "arrow-crossing", want=False)
    case("stroke through an arrow label", labelled(True), "arrow-through-label")
    case("arrow and its own label", labelled(False), "arrow-through-label", want=False)

    checked[0] += 1
    probe = base_scene()
    x_crossing(probe)
    if count_crossings(probe) != 1:
        failures.append(f"count_crossings on one X is {count_crossings(probe)}, not 1")


def built_cases(case, rules, failures: list[str], checked: list[int], metrics) -> None:
    """Clean cases on scenes the builder produced, so a builder change that breaks one shows here,
    plus the C4 and provenance rules fired by editing a built scene."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    # No bytecode cache: a __pycache__ written next to the scripts ships with the skill, and a
    # scanner rightly treats bytecode nobody reviewed as a supply-chain finding.
    sys.dont_write_bytecode = True
    try:
        import exdraw  # noqa: PLC0415 - the builder is the system under test for these cases
    except ImportError as exc:
        failures.append(f"cannot import exdraw for the built cases: {exc}")
        return
    try:
        import yaml  # noqa: PLC0415 - optional: only checks the dicts above against the files
    except ImportError:
        yaml = None
    if yaml is not None:
        for name, spec in EXAMPLE_SPECS.items():
            checked[0] += 1
            path = EXAMPLES / f"{name}.yaml"
            if yaml.safe_load(path.read_text(encoding="utf-8")) != spec:
                failures.append(f"EXAMPLE_SPECS[{name!r}] has drifted from {path.name}")

    def built(spec: dict) -> dict:
        return exdraw.build(copy.deepcopy(spec))

    for name in EXAMPLE_SPECS:
        checked[0] += 1
        scene = built(EXAMPLE_SPECS[name])
        loud = sorted({f.rule for f in lint(scene, metrics) if f.severity != "note"})
        quiet = sorted({f.rule for f in lint(scene, metrics) if f.rule.startswith("c4-")})
        if loud or quiet:
            failures.append(f"built example {name} is not clean: {loud + quiet}")

    checked[0] += 1
    pair = built({"type": "flow", "nodes": [{"id": "a"}, {"id": "b"}],
                  "edges": [{"from": "a", "to": "b", "label": "request"},
                            {"from": "b", "to": "a", "label": "reply"}]})
    pair_rules = {f.rule for f in lint(pair, metrics)}
    for rule in ("arrow-overlap", "arrow-crossing", "arrow-through-label"):
        if rule in pair_rules:
            failures.append(f"built reverse pair: {rule} fired, so the offset is missing")
    checked[0] += 1
    twice = built({"type": "flow", "nodes": [{"id": "a"}, {"id": "b"}],
                   "edges": [{"from": "a", "to": "b", "label": "reads"},
                             {"from": "a", "to": "b", "label": "writes"}]})
    twice_rules = {f.rule for f in lint(twice, metrics)}
    for rule in ("arrow-overlap", "arrow-through-label"):
        if rule in twice_rules:
            failures.append(f"built duplicate edge: {rule} fired, so the offset is missing")

    def refused(name: str, spec: dict, needle: str = "") -> None:
        checked[0] += 1
        try:
            exdraw.build(copy.deepcopy(spec))
        except exdraw.SpecError as exc:
            if needle and needle not in str(exc):
                failures.append(f"spec error for {name} does not say {needle!r}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - a crash is the failure being tested for
            kind = type(exc).__name__
            failures.append(f"{name}: crashed with {kind} instead of a spec error")
            return
        failures.append(f"{name}: built, but must be a spec error")

    flow = {"type": "flow", "title": "t", "nodes": [{"id": "a"}]}
    for bad_at in ("/etc/x.py", "../x.py", "C:/x.py", "c:x.py", "file:///x.py:1",
                   "https://example.org/x.py", "%2e%2e/x.py", "a//b.py", ":/x.py", "x\n../y.py",
                   None, 12):
        refused(f"at {bad_at!r}", dict(flow, nodes=[{"id": "a", "at": bad_at}]))
    for bad_repo in ("https://example.org/acme/shop", "file:///x", "../x", "a//b", 7):
        refused(f"repo {bad_repo!r}",
                dict(flow, source={"repo": bad_repo, "commit": "abcdef1"}))
    refused("commit as a number", dict(flow, source={"repo": "r", "commit": 1234567}), "quote")
    refused("tree with a cycle", {"type": "tree", "nodes": [{"id": "a"}, {"id": "b"}],
                                  "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]})
    refused("node that is not a mapping", {"type": "flow", "nodes": ["a"]})
    refused("edge that is not a mapping", {"type": "flow", "nodes": [{"id": "a"}],
                                           "edges": ["x"]})

    c4 = built(EXAMPLE_SPECS["c4"])

    def edit(change):
        def inner(scene):
            scene.clear()
            scene.update(copy.deepcopy(c4))
            change(scene["elements"])
        return inner

    def by_tech(elements, tech):
        return next(e for e in elements if custom(e).get("tech") == tech
                    and e.get("type") != "arrow")

    def arrow_text(elements, starts):
        return next(e for e in elements if e.get("type") == "text"
                    and str(e.get("text", "")).startswith(starts))

    def relabel(text):
        return lambda els: arrow_text(els, "Submits").update(text=text, originalText=text)

    def drop_title(els):
        els.remove(next(e for e in els if custom(e).get("role") == "title"))

    def unmark_title(els):
        custom(next(e for e in els if custom(e).get("role") == "title")).pop("role")

    def containers(n):
        def inner(els):
            for i in range(n):
                els.append(dict(by_tech(els, "Postgres"), id=f"extra{i}", type="rectangle",
                                x=3000 + 200 * i, y=100, boundElements=[],
                                roundness={"type": 3},
                                customData={"c4": "container", "tech": "Go", "provenance":
                                            {"at": "external"}}))
        return inner

    def boundary(geometric: bool):
        def inner(els):
            inner_box = dict(by_tech(els, "Postgres"), id="inside", type="rectangle", x=5040,
                             y=140, width=150, height=60, boundElements=[],
                             roundness={"type": 3})
            outer = dict(inner_box, id="scope", x=5000, y=100, width=300, height=200,
                         customData={} if geometric else {"c4": "boundary"})
            els.extend([outer, inner_box])
        return inner

    def set_commit(value):
        return lambda els: custom(next(e for e in els if custom(e).get("role") == "title"))[
            "provenance"].update(commit=value)

    case("c4 shape with no type", edit(lambda els: custom(by_tech(els, "Postgres")).pop("c4")),
         "c4-untyped")
    case("c4 boundary marked", edit(boundary(False)), "c4-untyped", want=False)
    case("c4 boundary by containment", edit(boundary(True)), "c4-untyped", want=False)
    case("c4 container with no tech",
         edit(lambda els: custom(by_tech(els, "Postgres")).pop("tech")), "c4-no-tech")
    case("c4 vague edge", edit(relabel("Uses\n[JSON over HTTPS]")), "c4-vague-edge")
    case("c4 vague edge, punctuated", edit(relabel("Talks to...")), "c4-vague-edge")
    case("c4 unlabelled edge", edit(relabel("")), "c4-vague-edge")
    case("c4 edge with intent", edit(relabel("Uses the cache\n[RESP]")), "c4-vague-edge",
         want=False)
    case("c4 no title", edit(drop_title), "c4-no-title")
    case("c4 title found by geometry", edit(unmark_title), "c4-no-title", want=False)
    case("c4 20 containers", edit(containers(17)), "c4-too-many")
    case("c4 19 containers", edit(containers(16)), "c4-too-many", want=False)
    case("provenance missing", edit(
        lambda els: custom(by_tech(els, "Python, FastAPI")).pop("provenance")),
        "provenance-missing")
    case("provenance bad commit", edit(set_commit("nothex!")), "provenance-commit")
    case("provenance short sha", edit(set_commit("3f9a2c1")), "provenance-commit", want=False)
    case("no source, no provenance rule",
         edit(lambda els: custom(next(e for e in els if custom(e).get("role") == "title"))
              .pop("provenance")), "provenance-missing", want=False)

    # Determinism: pin BLAKE2b digests of exdraw.build() for each shipped spec
    example_digests = {
        "request-path": "dea3cfa59050b01ce221db053c4da5cb851cf95dabfaa7311aaf2de473a31634a6ec6c7f9782746efe3df50ff3880b52e78ecc15a61c9ae621e3cb6f9cfb4392",
        "c4": "7a43a4bd210af2f22fb9df7b4d7525945455f094f5a5dd14d4b35d0a15046b58e596e011eda62e321f75f7e08972c71486e0a6e80f879021764989dada71532f",
    }
    for name, expected_hash in example_digests.items():
        checked[0] += 1
        scene1 = built(EXAMPLE_SPECS[name])
        scene2 = built(EXAMPLE_SPECS[name])
        b1 = json.dumps(scene1, indent=2).encode("utf-8")
        b2 = json.dumps(scene2, indent=2).encode("utf-8")
        if b1 != b2:
            failures.append(f"determinism: {name} build is not byte-identical across runs")
        actual_hash = hashlib.blake2b(b1).hexdigest()
        if actual_hash != expected_hash:
            failures.append(f"determinism: {name} digest mismatch: {actual_hash}")

    # Exact bytes of CLI default output path (mocking file write and clock)
    checked[0] += 1
    dummy_spec_path = Path("test_spec.json")
    spec_json = json.dumps(EXAMPLE_SPECS["request-path"])
    with patch("sys.argv", ["exdraw.py", str(dummy_spec_path)]), \
         patch.object(Path, "read_bytes", return_value=spec_json.encode("utf-8")), \
         patch.object(Path, "write_text") as mock_write, \
         contextlib.redirect_stdout(io.StringIO()):
        ret = exdraw.main()
        if ret != 0:
            failures.append(f"CLI main() exited with {ret}")
        else:
            args, _ = mock_write.call_args
            written_bytes = args[0].encode("utf-8")
            written_digest = hashlib.blake2b(written_bytes).hexdigest()
            expected_cli_digest = "c5b879a514c03b0bb3139434ecd6afd6dc3b00de253c3be3848d6bc60595f8c1a3ec8f6e6409685b6e5303e351239d7e8de2968289ff20cfaac8a26ec79425ec"
            if written_digest != expected_cli_digest:
                failures.append(f"CLI default output digest mismatch: {written_digest}")

    # Provenance edits: changing source or node anchor preserves IDs; changing label re-rolls
    checked[0] += 1
    base_ids = [(e["id"], e.get("seed"), e.get("versionNonce")) for e in c4["elements"]]
    spec_src = copy.deepcopy(EXAMPLE_SPECS["c4"])
    spec_src["source"]["commit"] = "0000000000000000000000000000000000000000"
    res_src = built(spec_src)
    src_ids = [(e["id"], e.get("seed"), e.get("versionNonce")) for e in res_src["elements"]]
    if src_ids != base_ids:
        failures.append("provenance: changing source commit did not preserve element IDs/seeds")

    checked[0] += 1
    spec_anchor = copy.deepcopy(EXAMPLE_SPECS["c4"])
    spec_anchor["nodes"][1]["at"] = "web/src/Other.tsx:1-50"
    res_anchor = built(spec_anchor)
    anchor_ids = [(e["id"], e.get("seed"), e.get("versionNonce")) for e in res_anchor["elements"]]
    if anchor_ids != base_ids:
        failures.append("provenance: changing node anchor did not preserve element IDs/seeds")

    checked[0] += 1
    spec_label = copy.deepcopy(EXAMPLE_SPECS["c4"])
    spec_label["nodes"][0]["label"] = "End User"
    res_label = built(spec_label)
    base_eids = {e["id"] for e in c4["elements"]}
    label_eids = {e["id"] for e in res_label["elements"]}
    if base_eids & label_eids:
        failures.append("provenance: changing node label did not re-roll all element IDs")

    # Bindings: every arrow in EXAMPLE_SPECS has bidirectional bindings with fixedPoint != 0.5
    for name in EXAMPLE_SPECS:
        checked[0] += 1
        scene = built(EXAMPLE_SPECS[name])
        by_id = {e["id"]: e for e in scene["elements"]}
        arrows = [e for e in scene["elements"] if e.get("type") == "arrow"]
        for a in arrows:
            aid = a["id"]
            sb = a.get("startBinding")
            eb = a.get("endBinding")
            if not sb or not eb:
                failures.append(f"bindings: arrow {aid} in {name} is missing endpoint bindings")
                continue
            for b in (sb, eb):
                fp = b.get("fixedPoint")
                if fp == [0.5, 0.5]:
                    failures.append(f"bindings: arrow {aid} in {name} has un-normalized fixedPoint 0.5")
                tid = b.get("elementId")
                target = by_id.get(tid)
                if not target:
                    failures.append(f"bindings: arrow {aid} target {tid} not found")
                    continue
                bound_ids = [be["id"] for be in (target.get("boundElements") or [])]
                if aid not in bound_ids:
                    failures.append(f"bindings: target {tid} does not list arrow {aid} in boundElements")

    # Assert removing reciprocal arrow binding fails exlint.lint() with binding-one-sided
    checked[0] += 1
    broken_scene = built(EXAMPLE_SPECS["request-path"])
    first_shape = next(e for e in broken_scene["elements"] if e.get("type") == "rectangle")
    first_shape["boundElements"] = []
    broken_rules = {f.rule for f in lint(broken_scene, metrics)}
    if "binding-one-sided" not in broken_rules:
        failures.append("bindings: removing reciprocal arrow binding did not fire binding-one-sided")

    # Literal text metrics against expected widths and unknown fallback glyphs
    checked[0] += 1
    w_ellipse = exdraw.max_text_width("ellipse", 200, 16)
    if w_ellipse != 131:
        failures.append(f"metrics: ellipse text width for w=200 is {w_ellipse}, expected 131")

    checked[0] += 1
    w_diamond = exdraw.max_text_width("diamond", 200, 16)
    if w_diamond != 90:
        failures.append(f"metrics: diamond text width for w=200 is {w_diamond}, expected 90")

    checked[0] += 1
    m_calc = exdraw.Metrics()
    w_font5 = round(m_calc.line_width("Hello world", 5, 20), 2)
    if w_font5 != 101.90:
        failures.append(f"metrics: font 5 width = {w_font5}, expected 101.90")

    checked[0] += 1
    w_font6 = round(m_calc.line_width("Hello world", 6, 20), 2)
    if w_font6 != 107.40:
        failures.append(f"metrics: font 6 width = {w_font6}, expected 107.40")

    checked[0] += 1
    w_unk5 = round(m_calc.family(5)["fallbackWidth"] * 20, 2)
    if w_unk5 != 10.43:
        failures.append(f"metrics: font 5 unknown char width = {w_unk5}, expected 10.43")

    checked[0] += 1
    w_unk6 = round(m_calc.family(6)["fallbackWidth"] * 20, 2)
    if w_unk6 != 10.24:
        failures.append(f"metrics: font 6 unknown char width = {w_unk6}, expected 10.24")


def self_test() -> int:
    """Falsification: seed each defect, assert the rule fires, remove it, assert it stops."""
    metrics = load_metrics()
    failures: list[str] = []
    checked = [0]   # a real count of the falsification cases run, printed in the summary

    def base_scene() -> dict:
        box = {"id": "box1", "type": "rectangle", "x": 100, "y": 100, "width": 150, "height": 60,
               "angle": 0, "strokeColor": "#1e1e1e", "backgroundColor": "transparent",
               "fillStyle": "solid", "strokeWidth": 2, "strokeStyle": "solid", "roughness": 1,
               "opacity": 100, "roundness": {"type": 3}, "seed": 12345, "version": 1,
               "versionNonce": 1, "isDeleted": False, "groupIds": [], "frameId": None,
               "boundElements": [], "updated": 1, "created": 1, "link": None, "locked": False}
        box2 = dict(box, id="box2", x=400, y=100, seed=999)
        return {"type": "excalidraw", "version": 2, "source": "x",
                "elements": [box, box2], "appState": {}, "files": {}}

    def rules(scene) -> set[str]:
        return {f.rule for f in lint(scene, metrics)}

    def case(name: str, mutate, rule: str, want: bool = True, severity: str | None = None) -> None:
        checked[0] += 1
        scene = base_scene()
        mutate(scene)
        findings = lint(scene, metrics)
        matched = [f for f in findings if f.rule == rule]
        fired = len(matched) > 0
        if fired != want:
            # The verdict is hoisted out of the f-string on purpose. A nested quote inside an
            # f-string opens a span that a bounded static parser cannot close, which marks this
            # whole file partially inspected and turns every reference to it from SKILL.md into an
            # AE1 finding at HIGH severity. Found by bisecting this file, 2026-09-09.
            verdict = "did not fire" if want else "fired"
            failures.append(f"{name}: rule {rule} {verdict}")
        elif want and severity is not None:
            actual = matched[0].severity
            if actual != severity:
                failures.append(f"{name}: rule {rule} severity is {actual}, expected {severity}")

    clean = rules(base_scene())
    for rule in ("node-overlap", "binding-one-sided", "points-origin", "roundness-class",
                 "text-too-narrow", "zero-size", "all-stacked", "id-duplicate"):
        checked[0] += 1
        if rule in clean:
            failures.append(f"baseline: {rule} fires on a clean scene (false positive)")

    case("unknown type", lambda s: s["elements"][0].__setitem__("type", "rectangel"),
         "type-unknown", severity="error")
    case("duplicate id", lambda s: s["elements"][1].__setitem__("id", "box1"),
         "id-duplicate", severity="error")
    case("zero coordinate", lambda s: s["elements"][0].__setitem__("x", 0),
         "zero-coordinate", severity="warn")
    case("zero size", lambda s: s["elements"][0].update(width=0, height=0),
         "zero-size", severity="error")
    case("opacity scale", lambda s: s["elements"][0].__setitem__("opacity", 0.5),
         "opacity-range", severity="error")
    case("rotated", lambda s: s["elements"][0].__setitem__("angle", 0.4),
         "rotated", severity="warn")
    case("roundness class", lambda s: s["elements"][0].__setitem__("roundness", {"type": 2}),
         "roundness-class", severity="error")
    case("seed default", lambda s: s["elements"][0].__setitem__("seed", 1),
         "seed-default", severity="warn")
    case("legacy key", lambda s: s["elements"][0].__setitem__("gap", 4),
         "legacy-key", severity="error")
    case("index written", lambda s: s["elements"][0].__setitem__("index", "a0"),
         "index-written", severity="warn")
    case("overlap", lambda s: s["elements"][1].update(x=120, y=110),
         "node-overlap", severity="error")
    case("all stacked", lambda s: s["elements"][1].update(x=100, y=100),
         "all-stacked", severity="error")
    case("tiny node", lambda s: s["elements"][0].update(width=20, height=10),
         "node-too-small", severity="warn")
    case("many roughness", lambda s: s["elements"][1].__setitem__("roughness", 2),
         "many-roughness", severity="warn")
    case("many fills", lambda s: [s["elements"].append(
        dict(s["elements"][0], id=f"f{i}", x=1000 + i * 200, backgroundColor=c, seed=i + 50))
        for i, c in enumerate(["#ffc9c9", "#b2f2bb", "#a5d8ff", "#ffec99", "#d0bfff", "#e9ecef"])],
        "many-fills", severity="warn")

    def text_el(**over) -> dict:
        text = {"id": "t1", "type": "text", "x": 105, "y": 117, "width": 40, "height": 25,
                "angle": 0, "strokeColor": "#1e1e1e", "backgroundColor": "transparent",
                "fillStyle": "solid", "strokeWidth": 2, "strokeStyle": "solid", "roughness": 1,
                "opacity": 100, "roundness": None, "seed": 777, "version": 1, "versionNonce": 1,
                "isDeleted": False, "groupIds": [], "frameId": None, "boundElements": [],
                "updated": 1, "created": 1, "link": None, "locked": False,
                "text": "Hi", "originalText": "Hi", "fontSize": 20, "fontFamily": 5,
                "lineHeight": 1.25, "textAlign": "center", "verticalAlign": "middle",
                "containerId": "box1", "autoResize": True, "labelPosition": None}
        text.update(over)
        return text

    def add_text(scene, **over):
        text = text_el(**over)
        scene["elements"].insert(1, text)
        scene["elements"][0]["boundElements"] = [{"type": "text", "id": text["id"]}]
        return text

    def with_text(mutate_text=None, break_reciprocal=False):
        def inner(scene):
            over = mutate_text or {}
            add_text(scene, **over)
            if break_reciprocal:
                scene["elements"][0]["boundElements"] = []
        return inner

    case("text ok", with_text(), "binding-one-sided", want=False)
    case("one-sided text", with_text(break_reciprocal=True), "binding-one-sided", severity="error")
    case("no lineHeight", with_text({"lineHeight": None}), "lineheight-missing", severity="error")
    case("narrow text", with_text({"text": "Authentication Service", "width": 40}),
         "text-too-narrow", severity="error")
    case("fractional size", with_text({"fontSize": 22.87, "width": 45.7, "height": 28.5875}),
         "fontsize-fractional", severity="warn")
    case("text height wrong", with_text({"height": 40}), "text-height", severity="error")

    def add_arrow(scene, **over):
        arrow = {"id": "a1", "type": "arrow", "x": 256, "y": 130, "width": 138, "height": 0,
                 "angle": 0, "strokeColor": "#1e1e1e", "backgroundColor": "transparent",
                 "fillStyle": "solid", "strokeWidth": 2, "strokeStyle": "solid", "roughness": 1,
                 "opacity": 100, "roundness": {"type": 2}, "seed": 555, "version": 1,
                 "versionNonce": 1, "isDeleted": False, "groupIds": [], "frameId": None,
                 "boundElements": [], "updated": 1, "created": 1, "link": None, "locked": False,
                 "points": [[0, 0], [138, 0]], "startArrowhead": None, "endArrowhead": "arrow",
                 "elbowed": False,
                 "startBinding": {"elementId": "box1", "fixedPoint": [1.04, 0.5001],
                                  "mode": "orbit"},
                 "endBinding": {"elementId": "box2", "fixedPoint": [-0.04, 0.5001],
                                "mode": "orbit"}}
        arrow.update(over)
        scene["elements"].append(arrow)
        for shape in (scene["elements"][0], scene["elements"][1]):
            shape["boundElements"] = list(shape.get("boundElements") or []) + [
                {"type": "arrow", "id": arrow["id"]}]
        return arrow

    def with_arrow(over=None, drop_reciprocal=False):
        def inner(scene):
            add_arrow(scene, **(over or {}))
            if drop_reciprocal:
                scene["elements"][0]["boundElements"] = []
        return inner

    case("arrow ok", with_arrow(), "binding-one-sided", want=False)
    case("arrow one-sided", with_arrow(drop_reciprocal=True), "binding-one-sided", severity="error")
    case("points origin", with_arrow({"points": [[5, 5], [138, 0]]}), "points-origin", severity="error")
    case("fixedPoint half", with_arrow(
        {"startBinding": {"elementId": "box1", "fixedPoint": [0.5, 0.5], "mode": "orbit"}}),
        "fixedpoint-half", severity="error")
    case("dangling binding", with_arrow(
        {"endBinding": {"elementId": "nope", "fixedPoint": [0, 0.5001], "mode": "orbit"}}),
        "binding-dangling", severity="error")
    case("implicit arrowhead", lambda s: s["elements"].append(
        {k: v for k, v in add_arrow(s).items() if k != "endArrowhead"}),
        "arrowhead-implicit", severity="error")
    case("huge arrow", with_arrow({"width": 90000, "points": [[0, 0], [90000, 0]]}),
         "linear-too-large", severity="error")

    stroke_cases(case, base_scene, text_el, failures, checked)
    built_cases(case, rules, failures, checked, metrics)

    for failure in failures:
        print(f"FAIL {failure}")
    total = checked[0]
    # Verdict hoisted out of the f-string: a nested quote inside one opens a span a bounded
    # static parser cannot close, which marks the file partially inspected. See the note in case().
    verdict = "FAIL" if failures else "PASS"
    print(f"{verdict}: exlint self-test, {total} falsification cases, "
          f"{len(failures)} failure(s)")
    return 1 if failures else 0


def main() -> int:
    """Exit 1 means the file has an error, so a crash may not reach it: any crash is exit 2."""
    try:
        return checked_main()
    except Exception as exc:  # noqa: BLE001 - the exit code contract, see the docstring
        kind = type(exc).__name__
        print(f"internal error: {kind}: {exc}", file=sys.stderr)
        return 2


def checked_main() -> int:
    ap = argparse.ArgumentParser(description="Lint a .excalidraw file.")
    ap.add_argument("file", nargs="?", type=Path)
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.file:
        ap.error("a file is required")
    try:
        scene = json.loads(args.file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 2

    findings = lint(scene, load_metrics())
    rank = {"error": 0, "warn": 1, "note": 2}
    findings.sort(key=lambda f: (rank[f.severity], f.rule, f.element))
    counts = {"error": 0, "warn": 0, "note": 0}
    for f in findings:
        counts[f.severity] += 1
        print(f"{f.severity:<5} {f.rule:<24} {f.element:<24} {f.message}")
    total = len(scene.get("elements") or [])
    errors, warns, notes = counts["error"], counts["warn"], counts["note"]
    print(f"\n{total} elements: {errors} error, {warns} warn, {notes} note, "
          f"crossings: {count_crossings(scene)}")
    if counts["error"] or (args.strict and counts["warn"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
