#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# The copyright holder is named in the LICENSE file of this skill.
"""The offline selftest for comb-pdf-form-filling: attribute resolution, comb geometry and assertions.

Purpose: prove inherited attribute resolution up parent chains (including explicit /Ff of 0),
    qualified dotted names, checkbox /AP /N state detection, comb layout math, and refusal
    on string length or boundary overflow.
Usage:
    python3 fill_example.py --selftest      (or: python3 selftest.py)
Exit codes: 0 all checks pass, 4 a check failed.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fill_example  # noqa: E402

if fill_example.stringWidth is None:
    fill_example.stringWidth = (
        lambda text, fontName="Helvetica", fontSize=11.0: len(text) * fontSize * 0.55
    )

CHECKS = 0


class IndirectMock:
    """Simulates a pypdf IndirectObject whose .get_object() returns the target dict."""

    def __init__(self, target: dict | IndirectMock | None):
        self._target = target

    def get_object(self):
        return self._target


class MockCanvas:
    """Mock ReportLab canvas that records drawn strings, fonts, and geometry calls."""

    def __init__(self):
        self.fonts: list[tuple[str, float]] = []
        self.strings: list[tuple[float, float, str]] = []
        self.lines: list[tuple[float, float, float, float]] = []

    def setFont(self, font: str, size: float):
        self.fonts.append((font, size))

    def drawString(self, x: float, y: float, text: str):
        self.strings.append((round(float(x), 4), round(float(y), 4), text))

    def line(self, x0: float, y0: float, x1: float, y1: float):
        self.lines.append((float(x0), float(y0), float(x1), float(y1)))


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    if not condition:
        message = f"FAIL {name}: {detail}" if detail else f"FAIL {name}"
        print(message, file=sys.stderr)
        sys.exit(4)
    CHECKS += 1


def raises(exc_type: type[Exception], fn) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


def run() -> int:
    global CHECKS
    CHECKS = 0

    # 1. FF_COMB constant
    check("FF_COMB constant is 1 << 24", fill_example.FF_COMB == (1 << 24))

    # 2. inherited(): direct key presence
    leaf = {"/FT": "/Tx", "/MaxLen": 10}
    check("inherited direct key /FT", fill_example.inherited(leaf, "/FT") == "/Tx")
    check("inherited direct key /MaxLen", fill_example.inherited(leaf, "/MaxLen") == 10)
    check("inherited missing key", fill_example.inherited(leaf, "/NonExistent") is None)

    # 3. inherited(): resolution through direct dict /Parent
    parent = {"/FT": "/Btn", "/MaxLen": 20}
    child = {"/Parent": parent, "/T": "ChildField"}
    check("inherited from parent /FT", fill_example.inherited(child, "/FT") == "/Btn")
    check("inherited from parent /MaxLen", fill_example.inherited(child, "/MaxLen") == 20)
    check("inherited child overrides parent", fill_example.inherited({"/Parent": parent, "/MaxLen": 5}, "/MaxLen") == 5)

    # 4. inherited(): resolution through IndirectMock (simulating pypdf IndirectObject)
    gp = {"/FT": "/Tx", "/Ff": fill_example.FF_COMB}
    p_ind = IndirectMock({"/Parent": IndirectMock(gp), "/MaxLen": 34})
    c = {"/Parent": p_ind}
    check("inherited indirect 2-hop /FT", fill_example.inherited(c, "/FT") == "/Tx")
    check("inherited indirect 1-hop /MaxLen", fill_example.inherited(c, "/MaxLen") == 34)

    # 5. inherited(): CRITICAL FALSY TEST: explicit local /Ff of 0 must NOT fall through to parent
    # In PDF forms, /Ff: 0 is falsy in Python (`0 or parent`), which would falsely restore parent's flags.
    p_comb = {"/Ff": fill_example.FF_COMB}
    c_plain = {"/Parent": p_comb, "/Ff": 0}
    check("inherited explicit 0 does not fall through", fill_example.inherited(c_plain, "/Ff") == 0)

    # 6. inherited(): cycle detection depth cap (terminates within 32 steps)
    cyclic_a: dict = {"/MaxLen": None}
    cyclic_b: dict = {}
    cyclic_a["/Parent"] = cyclic_b
    cyclic_b["/Parent"] = cyclic_a
    check("inherited handles cycle cleanly", fill_example.inherited(cyclic_a, "/FT") is None)

    # 7. qualified_name(): leaf, hierarchy, unnamed, cycle
    check("qualified_name leaf only", fill_example.qualified_name({"/T": "Leaf"}) == "Leaf")
    p_name = {"/T": "Account"}
    c_name = {"/Parent": p_name, "/T": "IBAN"}
    check("qualified_name parent.child", fill_example.qualified_name(c_name) == "Account.IBAN")
    gp_name = {"/T": "Form"}
    p_name2 = {"/Parent": gp_name, "/T": "Section"}
    c_name2 = {"/Parent": p_name2, "/T": "BIC"}
    check("qualified_name 3-tier", fill_example.qualified_name(c_name2) == "Form.Section.BIC")
    p_skip = {"/Parent": gp_name}  # no /T
    c_skip = {"/Parent": p_skip, "/T": "Postcode"}
    check("qualified_name skip parent without /T", fill_example.qualified_name(c_skip) == "Form.Postcode")
    check("qualified_name unnamed", fill_example.qualified_name({}) == "(unnamed)")
    cyclic_t1: dict = {"/T": "A"}
    cyclic_t2: dict = {"/T": "B", "/Parent": cyclic_t1}
    cyclic_t1["/Parent"] = cyclic_t2
    q_cycle = fill_example.qualified_name(cyclic_t1)
    check("qualified_name cyclic terminates", isinstance(q_cycle, str) and len(q_cycle) > 0)

    # 8. checkbox_on_state(): /AP /N parsing
    check("checkbox_on_state no /AP", fill_example.checkbox_on_state({}) is None)
    check("checkbox_on_state no /N", fill_example.checkbox_on_state({"/AP": {}}) is None)
    check(
        "checkbox_on_state /Yes",
        fill_example.checkbox_on_state({"/AP": {"/N": {"/Off": None, "/Yes": None}}}) == "/Yes",
    )
    check(
        "checkbox_on_state /1",
        fill_example.checkbox_on_state({"/AP": {"/N": {"/Off": None, "/1": None}}}) == "/1",
    )
    check(
        "checkbox_on_state /Ja",
        fill_example.checkbox_on_state({"/AP": {"/N": {"/Off": None, "/Ja": None}}}) == "/Ja",
    )
    check(
        "checkbox_on_state only /Off yields None",
        fill_example.checkbox_on_state({"/AP": {"/N": {"/Off": None}}}) is None,
    )
    check(
        "checkbox_on_state indirect /AP and /N",
        fill_example.checkbox_on_state(
            {"/AP": IndirectMock({"/N": IndirectMock({"/Off": 0, "/Checked": 1})})}
        ) == "/Checked",
    )

    # 9. per_cell(): refusal on character overflow
    mock_c = MockCanvas()
    check(
        "per_cell refuses len(s) > n_cells",
        raises(ValueError, lambda: fill_example.per_cell(mock_c, 0, 0, 100, 20, 3, "ABCD")),
    )

    # 10. per_cell(): geometry and font calls
    mock_c = MockCanvas()
    fill_example.per_cell(mock_c, 10.0, 20.0, 110.0, 40.0, 5, "AB", fs=10.0)
    check("per_cell sets Helvetica font", mock_c.fonts == [("Helvetica", 10.0)])
    cw = (110.0 - 10.0) / 5.0
    base = 20.0 + (20.0 - 10.0 * 0.70) / 2.0
    w_a = fill_example.stringWidth("A", "Helvetica", 10.0)
    w_b = fill_example.stringWidth("B", "Helvetica", 10.0)
    expected_a = (round(10.0 + 0.5 * cw - w_a / 2.0, 4), round(base, 4), "A")
    expected_b = (round(10.0 + 1.5 * cw - w_b / 2.0, 4), round(base, 4), "B")
    check("per_cell draws centered glyphs", mock_c.strings == [expected_a, expected_b])

    # 11. continuous(): left-aligned placement
    mock_c = MockCanvas()
    fill_example.continuous(mock_c, 10.0, 20.0, 110.0, 40.0, "Hello", fs=10.0)
    check("continuous sets font", mock_c.fonts == [("Helvetica", 10.0)])
    check("continuous draws at x0+2", mock_c.strings == [(12.0, round(base, 4), "Hello")])

    # 12. per_boundaries(): refusal and geometry
    mock_c = MockCanvas()
    check(
        "per_boundaries refuses too few boundaries",
        raises(ValueError, lambda: fill_example.per_boundaries(mock_c, [10.0, 20.0], "AB", 25.0)),
    )
    mock_c = MockCanvas()
    boundaries = [10.0, 30.0, 60.0]
    fill_example.per_boundaries(mock_c, boundaries, "XY", baseline=25.0, fs=10.0)
    w_x = fill_example.stringWidth("X", "Helvetica", 10.0)
    w_y = fill_example.stringWidth("Y", "Helvetica", 10.0)
    cx_x = (10.0 + 30.0) / 2.0
    cx_y = (30.0 + 60.0) / 2.0
    expected_x = (round(cx_x - w_x / 2.0, 4), 25.0, "X")
    expected_y = (round(cx_y - w_y / 2.0, 4), 25.0, "Y")
    check("per_boundaries draws centered at boundary midpoints", mock_c.strings == [expected_x, expected_y])

    print(f"selftest: {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
