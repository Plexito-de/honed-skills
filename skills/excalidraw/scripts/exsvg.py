#!/usr/bin/env python3
"""Render a .excalidraw file to a deterministic SVG you can screenshot and read.

WHAT THIS IS AND IS NOT
-----------------------
Geometry is exact: every box, arrow endpoint, label position and text width is drawn from the file's
own numbers, so if a label overflows its container or an arrow lands in the wrong place, you will
see it here.

Stroke texture is an APPROXIMATION. Excalidraw draws through roughjs, and this does not embed
roughjs; it perturbs each path with a seeded PRNG so the preview reads as hand-drawn at roughly the
right amplitude. Hachure and zigzag fills render as solid. For a pixel-faithful image, open the file
in Excalidraw and export from there.

Fonts: pass --fonts DIR pointing at a directory of Excalidraw's .woff2 files and the preview uses
the real faces, which makes text width visually correct as well as numerically correct. Without it
the browser substitutes, and the drawn box is still right.

Deterministic: the same input file gives a byte-identical SVG. The jitter comes from each element's
own `seed`, never from a clock or an unseeded RNG.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

FAMILY_BY_ID = {1: "Virgil", 2: "Helvetica", 3: "Cascadia", 5: "Excalifont", 6: "Nunito",
                7: "Lilita One", 8: "Comic Shanns", 9: "Liberation Sans", 10: "Assistant"}
FALLBACK = "Segoe UI Emoji, sans-serif"
PAD = 40


class Rng:
    """A small deterministic PRNG. Seeded per element, so the wobble is reproducible."""

    __slots__ = ("state",)

    def __init__(self, seed: int):
        self.state = (int(seed) or 1) & 0xFFFFFFFF

    def next(self) -> float:
        # xorshift32: short, no imports, and identical on every platform.
        x = self.state
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= x >> 17
        x ^= (x << 5) & 0xFFFFFFFF
        self.state = x & 0xFFFFFFFF
        return self.state / 0xFFFFFFFF

    def jitter(self, amount: float) -> float:
        return (self.next() - 0.5) * 2 * amount


def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def rough_polyline(points, rng: Rng, roughness: float, closed: bool) -> list[str]:
    """Two passes with independent jitter, which is what makes a roughjs stroke read as drawn."""
    if roughness <= 0:
        d = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in points)
        return [d + (" Z" if closed else "")]
    amp = 1.2 * roughness
    paths = []
    for _ in range(2):
        parts = []
        for i, (x, y) in enumerate(points):
            jx, jy = x + rng.jitter(amp), y + rng.jitter(amp)
            parts.append(f"{'M' if i == 0 else 'L'} {jx:.2f} {jy:.2f}")
        if closed:
            parts.append("Z")
        paths.append(" ".join(parts))
    return paths


def ellipse_points(cx, cy, rx, ry, steps=40):
    return [(cx + rx * math.cos(2 * math.pi * i / steps),
             cy + ry * math.sin(2 * math.pi * i / steps)) for i in range(steps + 1)]


def rounded_rect_points(x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    if r <= 0.5:
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    pts = []
    corners = [(x + w - r, y + r, -90, 0), (x + w - r, y + h - r, 0, 90),
               (x + r, y + h - r, 90, 180), (x + r, y + r, 180, 270)]
    for cx, cy, a0, a1 in corners:
        for i in range(6):
            a = math.radians(a0 + (a1 - a0) * i / 5)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def dash_for(style: str, stroke_width: float) -> str:
    if style == "dashed":
        return f'stroke-dasharray="{8} {8 + stroke_width}" '
    if style == "dotted":
        return f'stroke-dasharray="{1.5} {6 + stroke_width}" '
    return ""


def corner_radius(el: dict) -> float:
    roundness = el.get("roundness")
    if not isinstance(roundness, dict):
        return 0.0
    w, h = abs(el.get("width", 0)), abs(el.get("height", 0))
    smaller = min(w, h)
    if roundness.get("type") == 3:
        r = roundness.get("value") or 32
        return smaller * 0.25 if smaller <= r / 0.25 else float(r)
    return smaller * 0.25


def render(scene: dict, font_dir: Path | None) -> str:
    elements = [e for e in scene.get("elements") or [] if not e.get("isDeleted")]
    if not elements:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'

    xs, ys = [], []
    for el in elements:
        xs += [el.get("x", 0), el.get("x", 0) + el.get("width", 0)]
        ys += [el.get("y", 0), el.get("y", 0) + el.get("height", 0)]
    min_x, min_y = min(xs) - PAD, min(ys) - PAD
    width = max(xs) - min_x + PAD
    height = max(ys) - min_y + PAD
    background = (scene.get("appState") or {}).get("viewBackgroundColor") or "#ffffff"

    faces = ""
    if font_dir and font_dir.is_dir():
        rules = []
        for family, prefix in (("Excalifont", "Excalifont__"), ("Nunito", "Nunito__"),
                               ("Comic Shanns", "ComicShanns__"), ("Cascadia", "Cascadia__"),
                               ("Lilita One", "Lilita__"), ("Virgil", "Virgil__"),
                               ("Liberation Sans", "Liberation__"),
                               ("Assistant", "Assistant__Assistant-Regular")):
            for f in sorted(font_dir.glob(f"{prefix}*.woff2")):
                rules.append(f"@font-face{{font-family:'{family}';"
                             f"src:url('{f.resolve().as_uri()}') format('woff2');}}")
        faces = "\n".join(rules)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
           f'viewBox="0 0 {width:.0f} {height:.0f}">',
           f'<style>{faces}</style>' if faces else "<style></style>",
           f'<rect width="100%" height="100%" fill="{background}"/>',
           f'<g transform="translate({-min_x:.2f},{-min_y:.2f})">']

    for el in elements:
        kind = el.get("type")
        stroke = el.get("strokeColor", "#1e1e1e")
        fill = el.get("backgroundColor", "transparent")
        sw = el.get("strokeWidth", 2)
        roughness = el.get("roughness", 1)
        rng = Rng(el.get("seed") or 1)
        opacity = (el.get("opacity", 100) or 0) / 100
        dash = dash_for(el.get("strokeStyle", "solid"), sw)
        x, y = el.get("x", 0), el.get("y", 0)
        w, h = el.get("width", 0), el.get("height", 0)
        rotate = ""
        if el.get("angle"):
            deg = math.degrees(el["angle"])
            rotate = f' transform="rotate({deg:.3f} {x + w / 2:.2f} {y + h / 2:.2f})"'

        if kind in ("rectangle", "image", "iframe", "embeddable"):
            pts = rounded_rect_points(x, y, w, h, corner_radius(el))
            closed = True
        elif kind == "diamond":
            pts = [(x + w / 2, y), (x + w, y + h / 2), (x + w / 2, y + h), (x, y + h / 2)]
            closed = True
        elif kind == "ellipse":
            pts = ellipse_points(x + w / 2, y + h / 2, w / 2, h / 2)
            closed = True
        elif kind in ("line", "arrow", "freedraw"):
            pts = [(x + p[0], y + p[1]) for p in (el.get("points") or []) if len(p) == 2]
            closed = bool(el.get("polygon"))
        elif kind == "frame":
            pts = rounded_rect_points(x, y, w, h, 8)
            closed = True
            stroke, sw, roughness, dash = "#bbb", 2, 0, ""
        else:
            pts = None
            closed = False

        if kind == "text":
            font_id = el.get("fontFamily", 5)
            family = FAMILY_BY_ID.get(font_id, "Excalifont")
            size = el.get("fontSize", 20)
            line_height = el.get("lineHeight", 1.25)
            anchor = {"left": "start", "center": "middle", "right": "end"}.get(
                el.get("textAlign", "left"), "start")
            tx = x if anchor == "start" else x + w / 2 if anchor == "middle" else x + w
            lines = str(el.get("text", "")).split("\n")
            out.append(f'<g{rotate} opacity="{opacity:.2f}">')
            for i, line in enumerate(lines):
                # Baseline: Excalidraw centres the glyph box in the line box, so the baseline sits
                # about 0.79 of the font size below the line top for these faces.
                by = y + size * line_height * i + size * 0.79
                out.append(f'<text x="{tx:.2f}" y="{by:.2f}" font-family="{family}, {FALLBACK}" '
                           f'font-size="{size}" fill="{stroke}" text-anchor="{anchor}" '
                           f'xml:space="preserve">{esc(line)}</text>')
            out.append("</g>")
            continue

        if pts is None or len(pts) < 2:
            continue

        out.append(f'<g{rotate} opacity="{opacity:.2f}">')
        if fill and fill != "transparent" and closed:
            d = "M " + " L ".join(f"{px:.2f} {py:.2f}" for px, py in pts) + " Z"
            out.append(f'<path d="{d}" fill="{fill}" stroke="none"/>')
        for d in rough_polyline(pts, rng, roughness, closed):
            out.append(f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{sw}" '
                       f'{dash}stroke-linecap="round" stroke-linejoin="round"/>')
        if kind == "arrow" and len(pts) >= 2:
            for head, (tip, prev) in (("startArrowhead", (pts[0], pts[1])),
                                      ("endArrowhead", (pts[-1], pts[-2]))):
                if not el.get(head):
                    continue
                ang = math.atan2(tip[1] - prev[1], tip[0] - prev[0])
                size = max(10.0, sw * 4)
                for delta in (math.pi * 0.85, -math.pi * 0.85):
                    ex = tip[0] + size * math.cos(ang + delta)
                    ey = tip[1] + size * math.sin(ang + delta)
                    out.append(f'<path d="M {tip[0]:.2f} {tip[1]:.2f} L {ex:.2f} {ey:.2f}" '
                               f'fill="none" stroke="{stroke}" stroke-width="{sw}" '
                               'stroke-linecap="round"/>')
        out.append("</g>")

    out.append("</g></svg>")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a .excalidraw file to SVG.")
    ap.add_argument("file", type=Path)
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--fonts", type=Path,
                    default=Path.home() / ".cache/excalidraw-fonts",
                    help="directory of Excalidraw .woff2 files, for a faithful preview")
    args = ap.parse_args()
    try:
        scene = json.loads(args.file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    svg = render(scene, args.fonts)
    out = args.output or args.file.with_suffix(".svg")
    out.write_text(svg + "\n", encoding="utf-8")
    fonts = "real fonts" if args.fonts and args.fonts.is_dir() else "substituted fonts"
    print(f"{out}  ({fonts}; geometry exact, stroke texture approximate)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
