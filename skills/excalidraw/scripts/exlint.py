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

Exit 0 when there is no error, 1 when there is, 2 when the file cannot be read. `--strict` promotes
warnings to errors.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"

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
    """Does the segment cross the box interior? Sampled rather than solved: 64 points along the
    segment is enough for a 20 px inset and keeps this readable."""
    x0, y0 = box["x"] + inset, box["y"] + inset
    x1, y1 = box["x"] + box["width"] - inset, box["y"] + box["height"] - inset
    if x1 <= x0 or y1 <= y0:
        return False
    for i in range(1, 64):
        t = i / 64
        px = p1[0] + (p2[0] - p1[0]) * t
        py = p1[1] + (p2[1] - p1[1]) * t
        if x0 < px < x1 and y0 < py < y1:
            return True
    return False


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
                            "snapped to 0.5001 and can flip the arrow heading")
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


def self_test() -> int:
    """Falsification: seed each defect, assert the rule fires, remove it, assert it stops."""
    metrics = load_metrics()
    failures: list[str] = []

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

    def case(name: str, mutate, rule: str, want: bool = True) -> None:
        scene = base_scene()
        mutate(scene)
        fired = rule in rules(scene)
        if fired != want:
            # The verdict is hoisted out of the f-string on purpose. A nested quote inside an
            # f-string opens a span that a bounded static parser cannot close, which marks this
            # whole file partially inspected and turns every reference to it from SKILL.md into an
            # AE1 finding at HIGH severity. Found by bisecting this file, 2026-09-09.
            verdict = "did not fire" if want else "fired"
            failures.append(f"{name}: rule {rule} {verdict}")

    clean = rules(base_scene())
    for rule in ("node-overlap", "binding-one-sided", "points-origin", "roundness-class",
                 "text-too-narrow", "zero-size", "all-stacked", "id-duplicate"):
        if rule in clean:
            failures.append(f"baseline: {rule} fires on a clean scene (false positive)")

    case("unknown type", lambda s: s["elements"][0].__setitem__("type", "rectangel"),
         "type-unknown")
    case("duplicate id", lambda s: s["elements"][1].__setitem__("id", "box1"), "id-duplicate")
    case("zero coordinate", lambda s: s["elements"][0].__setitem__("x", 0), "zero-coordinate")
    case("zero size", lambda s: s["elements"][0].update(width=0, height=0), "zero-size")
    case("opacity scale", lambda s: s["elements"][0].__setitem__("opacity", 0.5), "opacity-range")
    case("rotated", lambda s: s["elements"][0].__setitem__("angle", 0.4), "rotated")
    case("roundness class", lambda s: s["elements"][0].__setitem__("roundness", {"type": 2}),
         "roundness-class")
    case("seed default", lambda s: s["elements"][0].__setitem__("seed", 1), "seed-default")
    case("legacy key", lambda s: s["elements"][0].__setitem__("gap", 4), "legacy-key")
    case("index written", lambda s: s["elements"][0].__setitem__("index", "a0"), "index-written")
    case("overlap", lambda s: s["elements"][1].update(x=120, y=110), "node-overlap")
    case("all stacked", lambda s: s["elements"][1].update(x=100, y=100), "all-stacked")
    case("tiny node", lambda s: s["elements"][0].update(width=20, height=10), "node-too-small")
    case("many roughness", lambda s: s["elements"][1].__setitem__("roughness", 2),
         "many-roughness")
    case("many fills", lambda s: [s["elements"].append(
        dict(s["elements"][0], id=f"f{i}", x=1000 + i * 200, backgroundColor=c, seed=i + 50))
        for i, c in enumerate(["#ffc9c9", "#b2f2bb", "#a5d8ff", "#ffec99", "#d0bfff", "#e9ecef"])],
        "many-fills")

    def add_text(scene, **over):
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
    case("one-sided text", with_text(break_reciprocal=True), "binding-one-sided")
    case("no lineHeight", with_text({"lineHeight": None}), "lineheight-missing")
    case("narrow text", with_text({"text": "Authentication Service", "width": 40}),
         "text-too-narrow")
    case("fractional size", with_text({"fontSize": 22.87, "width": 45.7, "height": 28.5875}),
         "fontsize-fractional")
    case("text height wrong", with_text({"height": 40}), "text-height")

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
    case("arrow one-sided", with_arrow(drop_reciprocal=True), "binding-one-sided")
    case("points origin", with_arrow({"points": [[5, 5], [138, 0]]}), "points-origin")
    case("fixedPoint half", with_arrow(
        {"startBinding": {"elementId": "box1", "fixedPoint": [0.5, 0.5], "mode": "orbit"}}),
        "fixedpoint-half")
    case("dangling binding", with_arrow(
        {"endBinding": {"elementId": "nope", "fixedPoint": [0, 0.5001], "mode": "orbit"}}),
        "binding-dangling")
    case("implicit arrowhead", lambda s: s["elements"].append(
        {k: v for k, v in add_arrow(s).items() if k != "endArrowhead"}), "arrowhead-implicit")
    case("huge arrow", with_arrow({"width": 90000, "points": [[0, 0], [90000, 0]]}),
         "linear-too-large")

    for failure in failures:
        print(f"FAIL {failure}")
    total = 30
    # Verdict hoisted out of the f-string: a nested quote inside one opens a span a bounded
    # static parser cannot close, which marks the file partially inspected. See the note in case().
    verdict = "FAIL" if failures else "PASS"
    print(f"{verdict}: exlint self-test, ~{total} falsification cases, "
          f"{len(failures)} failure(s)")
    return 1 if failures else 0


def main() -> int:
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
    print(f"\n{total} elements: {counts['error']} error, {counts['warn']} warn, "
          f"{counts['note']} note")
    if counts["error"] or (args.strict and counts["warn"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
