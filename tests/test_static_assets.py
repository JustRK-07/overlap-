#!/usr/bin/env python3
"""test_static_assets.py — the front end must match the markup.

``static/app.js`` looks every element up by id (``$("findings-tbody")``).  A
typo in an id, or an element that was renamed in ``index.html`` without
updating the JS, is invisible until a user clicks the button — the panel
simply does nothing.

These tests parse the two files and check they agree.  They also pin the
findings panel's contract (PLAN_MISMATCH §4): the columns the plan lists,
and the fact that the drawing-derived strings are escaped before they reach
``innerHTML``.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

INDEX = ROOT / "static" / "index.html"
APPJS = ROOT / "static" / "app.js"
STYLES = ROOT / "static" / "styles.css"


def _ids_in_html() -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', INDEX.read_text()))


def _ids_used_in_js() -> set[str]:
    src = APPJS.read_text()
    return set(re.findall(r'\$\("([^"]+)"\)', src))


def test_every_id_the_js_looks_up_exists_in_the_markup():
    """A missing id means a silently dead control."""
    html_ids = _ids_in_html()
    js_ids = _ids_used_in_js()
    missing = sorted(js_ids - html_ids)
    assert not missing, (
        f"app.js looks up ids that index.html does not define: {missing}")


def test_the_findings_panel_has_the_columns_the_plan_lists():
    """PLAN_MISMATCH §4: id, kind, severity, x, y, offset, |local|, tol,
    ratio, MECH ref, STR ref, message."""
    html = INDEX.read_text()
    table = re.search(r'<table id="findings-table">.*?</table>', html, re.S)
    assert table, "the findings table is missing"
    headers = re.findall(r"<th>(.*?)</th>", table.group(0), re.S)
    headers = [re.sub(r"<[^>]+>", "", h).strip() for h in headers]
    assert len(headers) == 12, headers
    for expected in ("ID", "Kind", "Severity", "X (MECH)", "Y (MECH)",
                     "Offset (mm)", "|local| (mm)", "Tol (mm)", "Ratio",
                     "MECH ref", "STR ref", "Message"):
        assert expected in headers, f"the findings table is missing {expected!r}"


def test_the_findings_panel_has_its_controls():
    """The filters and the two buttons the plan lists."""
    html_ids = _ids_in_html()
    for element_id in ("findings-count", "findings-kind-select",
                       "findings-include-info", "findings-include-systematic",
                       "findings-coverage", "findings-tbody",
                       "findings-show-more", "findings-preview",
                       "findings-overlay", "findings-reset-zoom",
                       "findings-copy-coords"):
        assert element_id in html_ids, f"#{element_id} is missing"


def test_drawing_derived_strings_are_escaped_before_they_reach_innerhtml():
    """A DXF label is untrusted input.

    The suggestions list interpolates the drawing's own label text into
    ``innerHTML``.  A DXF containing ``<img src=x onerror=…>`` would execute
    in the reviewer's browser, so every interpolation must go through the
    ``_esc`` helper.
    """
    src = APPJS.read_text()
    offenders = []
    # every `innerHTML =` assignment and the template literals inside it
    for match in re.finditer(r"\.innerHTML\s*=\s*", src):
        start = match.end()
        # take the statement up to the next `;` at the same nesting depth
        depth = 0
        end = start
        while end < len(src):
            ch = src[end]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif ch == ";" and depth == 0:
                break
            end += 1
        block = src[start:end]
        for interp in re.finditer(r"\$\{([^}]*)\}", block):
            expr = interp.group(1)
            # a literal, a number, or a helper call is fine; a bare
            # property access that could carry drawing text is not
            if re.fullmatch(r"[\w.]+", expr) and not re.fullmatch(
                    r"(?:s|i|e|p|f|b|c|t|n|d|o|r)\.[\w.]+", expr):
                continue
            if re.search(r"\.(prefix|samples|text|message|note|block|layer|"
                         r"handle|kind|id|type|severity)\b", expr):
                if "_esc(" not in expr:
                    offenders.append(expr.strip())
    assert not offenders, (
        "these values are interpolated into innerHTML without _esc(): "
        f"{offenders}")


def test_the_stylesheet_defines_the_findings_panel():
    css = STYLES.read_text()
    for selector in (".findings-coverage", ".findings-preview-wrap",
                     ".findings-overlay", "#findings-table"):
        assert selector in css, f"{selector} is not styled"


def test_the_step_rail_grid_fits_the_eleven_steps():
    """The rail has 11 rows; the grid must not strand two of them."""
    html = INDEX.read_text()
    rail = re.search(r'<ol[^>]*id="step-rail"[^>]*>(.*?)</ol>', html, re.S)
    assert rail, "the step rail is missing"
    n_steps = len(re.findall(r"<li\b", rail.group(1)))
    assert n_steps == 11, f"the rail has {n_steps} rows, not 11"
    css = STYLES.read_text()
    block = re.search(r"\.step-rail\s*\{(.*?)\}", css, re.S)
    assert block, "the step rail is not styled"
    assert "repeat(9," not in block.group(1), (
        "the step rail is still a fixed 9-column grid for 11 steps")
