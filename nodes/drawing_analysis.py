"""
Drawing Analysis Node - ConflictScan / RFI Automation
======================================================
Builds the As-Drawn Set for a construction drawing, returned as a
DrawingProfile (state.py) so conflict_scan.py handles it like any other profile.

Input protocol
--------------
PDF only   - vision pass over every page: geometry, annotations, dimensions,
             tags, hatches, title block, revision clouds, schedule callouts.
DXF only   - the DXF is rendered for the same vision pass, and its entities
             (layers, LINE/POLYLINE/TEXT/INSERT/DIMENSION, block attributes,
             coordinates) are extracted directly - no model involved.
PDF + DXF  - DXF drives quantitative extraction (block counts, grid spacings,
             block attributes); the PDF is what the vision pass reads for
             visual and annotation context.
Neither    - rejected by receive_input before this module is reached.

Baseline-driven extraction
--------------------------
1. build_baseline_set() turns the OTHER document's profile, under the review
   focus, into a Baseline Requirement Set.
2. build_drawing_profile() reads the drawing with the baseline's
   element/attribute checklist in every tile prompt. Baseline VALUES are
   withheld so the vision model reports what is drawn, not what it expects.
3. conflict_scan.py diffs the two sets under the review-focus lens.

Every as-drawn item carries an analysis_framework category and cites
"Sheet | Grid or Zone | Detail".
"""

import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nodes.analysis_framework import FRAMEWORK_TEXT, category_rank, normalise_category
from nodes.file_ingest import decode_symbol_font
from nodes.run_paths import debug_dir

# ---------------------------------------------------------------------------
# Lazy-loaded clients (avoid import cost when no drawing is ever profiled)
# ---------------------------------------------------------------------------
_vision_client_instance = None
_llm_client_instance = None


def _get_vision_llm(role: str = "drawing_reader"):
    """The AI fleet's vision role for drawings (config/fleet.toml)."""
    from fleet import llm
    return llm(role)


def _get_llm():
    """The AI fleet's extractor role (baseline requirement sets from documents)."""
    from fleet import llm
    return llm("extractor")


# ---------------------------------------------------------------------------
# Rendering / tiling tuning
#
# ezdxf's Frontend.draw_layout(finalize=True) resizes the matplotlib figure
# to fit the layout's own content aspect ratio (overriding any fixed figsize
# set beforehand), so a fixed DPI alone doesn't give a predictable pixel
# resolution - a wide drawing and a tall one would come out very different
# sizes. Instead we target a fixed LONG SIDE in pixels and compute the DPI
# needed to hit it from whatever figure size ezdxf settles on (DXF path) or
# from the PDF page's physical size in points (PDF path).
# ---------------------------------------------------------------------------
RENDER_TARGET_LONG_PX = 4000   # long-side pixel target for a full sheet render
MIN_RENDER_DPI = 72
MAX_RENDER_DPI = 1200

TILE_SIZE = 1600            # px - kept under vision-model effective input resolution
MIN_TILE_OVERLAP = 250      # px - floor, used when a sheet's text scale can't be measured
MAX_TILE_OVERLAP_FRACTION = 0.4   # never more than this fraction of TILE_SIZE
OVERLAP_TEXT_HEIGHT_FACTOR = 18   # overlap = median annotation text height (px) x this

MAX_TEXT_LINES_PER_TILE = 120     # PDF text-layer strings handed to one tile prompt

BASELINE_BATCH_SIZE = 40          # reference statements per baseline-building LLM call
MAX_CHECKLIST_ITEMS = 60          # baseline items shown to the vision model (in-focus first)

MAX_DXF_ATTRIBUTE_GROUPS = 150
DIMENSION_OVERRIDE_TOLERANCE = 0.01   # 1% - same tolerance conflict_scan uses for quantities

# Every build_drawing_profile() call writes one JSON dump here - render/tile
# PNGs are deleted after the scan (see api.py), so this is the only lasting
# record of what was actually read.


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in (name or "")) or "sheet"


def _text(value: Any) -> str:
    """Model output -> clean string; placeholder nulls ('null', 'n/a', ...) become ''."""
    if isinstance(value, list):
        return ", ".join(t for t in (_text(v) for v in value) if t)
    s = str(value if value is not None else "").strip()
    return "" if s.lower() in ("", "null", "none", "n/a", "na", "-") else s


def _text_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [s for s in (_text(v) for v in value) if s]


def _fmt_num(value: float) -> str:
    if abs(value - round(value)) < 1e-6 * max(1.0, abs(value)):
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _cite(source: str, grid: str = "", zone: str = "", detail: str = "") -> str:
    """Traceable reference: sheet | grid (or zone when no grid is legible) | detail."""
    location = f"Grid {grid}" if grid else f"Zone {zone or 'unspecified'}"
    return f"{source} | {location} | Detail {detail or 'n/a'}"


def _run_json(client, **kwargs) -> Dict[str, Any]:
    result = client.run(**kwargs)
    return result if isinstance(result, dict) else {}


def _adaptive_overlap(text_height_px: Optional[float]) -> int:
    """
    Size tile overlap from a sheet's own measured annotation-text scale, so it
    scales with whatever units/zoom/print-scale the source uses, instead of a
    fixed pixel value with no relationship to the drawing's actual scale.

    Coverage argument (heuristic, not a hard guarantee): a leader line + its
    label + the geometry it points to rarely spans much more than a handful
    of text-heights, so an overlap many times the median text height makes it
    very likely any such local cluster is fully contained in at least one
    tile.
    """
    max_overlap = int(TILE_SIZE * MAX_TILE_OVERLAP_FRACTION)
    if not text_height_px or text_height_px <= 0:
        return MIN_TILE_OVERLAP
    overlap = text_height_px * OVERLAP_TEXT_HEIGHT_FACTOR
    return int(max(MIN_TILE_OVERLAP, min(overlap, max_overlap)))


def _in_focus(text: str, review_focus: str) -> bool:
    if not review_focus:
        return True
    from nodes.conflict_scan import _tokens
    return bool(_tokens(text) & _tokens(review_focus))


# ---------------------------------------------------------------------------
# Step 1 - Baseline Requirement Set, built from the reference document
# ---------------------------------------------------------------------------

_BASELINE_SYSTEM = f"""\
You are a senior multi-discipline engineer (structural, architectural, MEP)
preparing to check construction drawings against a REFERENCE document. You are given statements already extracted from
that reference document. Turn them into the Baseline Requirement Set: the
specific things a drawing reviewer must find on the drawing and compare.

Classify every checkable item into one category of this framework:
{FRAMEWORK_TEXT}

Return a JSON object:
{{
  "items": [
    {{
      "statement_index": <integer index of the source statement>,
      "category": "<one category key from the list above>",
      "element": "<the specific element the requirement applies to, as named in the statement>",
      "attribute": "<what must be checked on the drawing for that element>",
      "baseline_value": "<the value the reference document requires, exactly as written, else null>",
      "in_focus": <true|false>
    }}
  ]
}}

Rules:
- Return ONLY valid JSON. No markdown fences, no preamble.
- Skip statements that can never appear on a drawing (payment, insurance,
  programme, submittal procedure...).
- One statement may yield several items when it sets several checkable values.
- Never invent a value that is not in the statement.
- in_focus: true when the item falls within the REVIEW FOCUS; when no review
  focus is given, true for every item.
- Use correct construction terminology (clear cover, development length,
  lap length, shear wall boundary element...).
"""


def _fallback_baseline_items(batch: List[Dict[str, Any]], start: int, review_focus: str) -> List[Dict[str, Any]]:
    items = []
    for offset, req in enumerate(batch):
        topic = _text(req.get("topic")) or _text(req.get("statement"))[:80]
        if not topic:
            continue
        items.append({
            "source_kind": "requirement",
            "source_index": start + offset,
            "source_uid": req.get("uid"),
            "category": "",
            "element": topic,
            "attribute": "as stated",
            "baseline_value": req.get("value"),
            "baseline_reference": req.get("reference") or "",
            "scopes": list(req.get("scopes") or []),
            "in_focus": _in_focus(f"{topic} {req.get('statement') or ''}", review_focus),
        })
    return items


def build_baseline_set(reference_profile: Dict[str, Any], reference_slot: str, review_focus: str = "") -> Dict[str, Any]:
    """
    Step 1 of the drawing protocol: parse the reference document's profile into
    a Baseline Requirement Set. Each item keeps a source_index back into the
    reference profile, so conflict_scan can pair drawing items with the exact
    statement they are checked against.
    """
    review_focus = (review_focus or "").strip()
    label = reference_profile.get("label") or "reference document"
    reqs = reference_profile.get("requirements") or []
    qtys = reference_profile.get("quantities") or []

    items: List[Dict[str, Any]] = []
    fallback_batches = 0

    for start in range(0, len(reqs), BASELINE_BATCH_SIZE):
        batch = reqs[start:start + BASELINE_BATCH_SIZE]
        lines = [
            f"REFERENCE DOCUMENT: {label}",
            f"REVIEW FOCUS: {review_focus or '(none - full-scope technical comparison)'}",
            "",
        ]
        for i, req in enumerate(batch, start=start):
            lines += [
                f"--- STATEMENT {i} ---",
                f"topic     : {req.get('topic') or '-'}",
                f"statement : {req.get('statement') or '-'}",
                f"value     : {req.get('value') if req.get('value') is not None else '-'}",
                "",
            ]
        print(f"[drawing_analysis] baseline: classifying statements {start}-{start + len(batch) - 1} of '{label}'")

        try:
            result = _run_json(_get_llm(), system=_BASELINE_SYSTEM, user="\n".join(lines), temperature=0.1)
        except Exception as exc:
            print(f"[drawing_analysis] baseline batch at {start} failed ({exc}) - using statement topics as-is")
            fallback_batches += 1
            items.extend(_fallback_baseline_items(batch, start, review_focus))
            continue

        for raw in result.get("items") or []:
            if not isinstance(raw, dict):
                continue
            idx = raw.get("statement_index")
            if not isinstance(idx, int) or not start <= idx < start + len(batch):
                continue
            src = reqs[idx]
            element = _text(raw.get("element")) or _text(src.get("topic"))
            if not element:
                continue
            items.append({
                "source_kind": "requirement",
                "source_index": idx,
                "source_uid": src.get("uid"),
                "category": normalise_category(raw.get("category")),
                "element": element,
                "attribute": _text(raw.get("attribute")) or "as stated",
                "baseline_value": _text(raw.get("baseline_value")) or src.get("value"),
                "baseline_reference": src.get("reference") or "",
                "scopes": list(src.get("scopes") or []),
                "in_focus": bool(raw.get("in_focus", True)) if review_focus else True,
            })

    for idx, qty in enumerate(qtys):
        description = _text(qty.get("description"))
        if not description:
            continue
        amount_unit = " ".join(str(v) for v in (qty.get("amount"), qty.get("unit")) if v not in (None, ""))
        items.append({
            "source_kind": "quantity",
            "source_index": idx,
            "category": "quantities",
            "element": description,
            "attribute": "quantity",
            "baseline_value": amount_unit or None,
            "baseline_reference": qty.get("reference") or "",
            "in_focus": _in_focus(description, review_focus),
        })

    items.sort(key=lambda it: (not it["in_focus"], category_rank(it["category"])))
    for n, item in enumerate(items, start=1):
        item["id"] = f"B-{n:03d}"

    print(
        f"[drawing_analysis] baseline from '{label}': {len(items)} item(s), "
        f"{sum(1 for it in items if it['in_focus'])} in focus"
    )
    return {
        "reference_slot": reference_slot,
        "reference_label": label,
        "review_focus": review_focus,
        "items": items,
        "fallback_batches": fallback_batches,
    }


def _scoped_baseline(baseline: Optional[Dict[str, Any]], source_name: str) -> Optional[Dict[str, Any]]:
    """Baseline items whose structure can appear on this drawing (by its file name); all when unknown."""
    if not baseline or not baseline.get("items"):
        return baseline
    from nodes.scope import structures, scopes_compatible
    shown = structures(source_name)
    items = [it for it in baseline["items"] if scopes_compatible(it.get("scopes"), shown)]
    return {**baseline, "items": items or baseline["items"]}


def _checklist(baseline: Optional[Dict[str, Any]]) -> Tuple[str, set]:
    """The baseline as the vision model sees it: ids, categories, elements, attributes - never values."""
    if not baseline or not baseline.get("items"):
        return (
            "No reference baseline was supplied for this drawing - extract everything "
            "legible across all framework categories (full-scope technical comparison).",
            set(),
        )
    shown = baseline["items"][:MAX_CHECKLIST_ITEMS]
    lines = [
        f"BASELINE CHECKLIST from '{baseline['reference_label']}' - what this drawing is expected "
        f"to show. Look for each of these specifically. Put an id in \"baseline_ids\" only on an item "
        f"that IS that element and attribute (at most 2 ids). You are NOT told the required values: "
        f"report only what is drawn.",
    ]
    for it in shown:
        lines.append(f"{it['id']} [{it['category'] or 'unclassified'}] {it['element']} - {it['attribute']}")
    return "\n".join(lines), {it["id"] for it in shown}


# ---------------------------------------------------------------------------
# DXF - read, extract entities, infer grids
# ---------------------------------------------------------------------------

_DXF_UNITS = {0: "drawing units", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}
_GRID_RE = re.compile(r"grid|axis|axes", re.IGNORECASE)
_GRID_LABEL_RE = re.compile(r"^(?:[A-Z]{1,2}'?|\d{1,3}'?|[A-Z]?\d{1,2}\.\d|[A-Z]\.\d)$", re.IGNORECASE)
_TITLE_BLOCK_RE = re.compile(r"title|border|tblock|frame|sheet", re.IGNORECASE)
_DIMTYPES = {0: "linear", 1: "aligned", 2: "angular", 3: "diameter", 4: "radius", 5: "angular 3-point", 6: "ordinate"}

_CATEGORY_HINTS = [
    (re.compile(r"rebar|reinf|stirrup|\blink|\bbar", re.IGNORECASE), "reinforcement"),
    (re.compile(r"col|beam|slab|wall|footing|ftg|pile|found|girder|joist|brace", re.IGNORECASE), "members"),
    (re.compile(r"open|sleeve|penet|embed|anchor|duct|pipe", re.IGNORECASE), "interfaces"),
    (re.compile(r"grid|axis|level|lvl", re.IGNORECASE), "grid_geometry"),
    (re.compile(r"conc|steel|matl|material", re.IGNORECASE), "materials"),
]


def _hint_category(*names: str) -> str:
    joined = " ".join(n for n in names if n)
    if _TITLE_BLOCK_RE.search(joined):
        return "annotations_notes"
    for pattern, category in _CATEGORY_HINTS:
        if pattern.search(joined):
            return category
    return "annotations_notes"


def _read_dxf(path: str):
    import ezdxf
    from ezdxf import recover
    try:
        return ezdxf.readfile(path)
    except Exception:
        # recover tolerates minor structural errors common in third-party exports
        doc, _auditor = recover.readfile(path)
        return doc


def _plain_text(entity) -> str:
    try:
        return entity.plain_text().strip()
    except Exception:
        return str(entity.dxf.get("text", "") or "").strip()


def _text_anchor(entity):
    """Visual anchor of a TEXT/MTEXT - aligned TEXT stores its true position in align_point."""
    if entity.dxftype() == "TEXT" and (entity.dxf.get("halign", 0) or entity.dxf.get("valign", 0)):
        return entity.dxf.get("align_point") or entity.dxf.insert
    return entity.dxf.insert


def _unique_positions(values: List[float], eps: float) -> List[float]:
    out: List[float] = []
    for v in sorted(values):
        if not out or v - out[-1] > eps:
            out.append(v)
    return out


def _infer_grids(msp) -> Dict[str, List[Tuple[float, str]]]:
    """
    Grid lines = axis-parallel LINE/2-point LWPOLYLINE on a GRID/AXIS layer;
    each grid bubble label is snapped to the line it sits on the extension of.
    Returns {"x": [(x, label)...] for vertical grids, "y": [(y, label)...]}.
    """
    v_lines: List[float] = []
    h_lines: List[float] = []
    longest = 0.0
    for e in msp.query("LINE LWPOLYLINE"):
        if not _GRID_RE.search(e.dxf.layer or ""):
            continue
        if e.dxftype() == "LINE":
            x1, y1, x2, y2 = e.dxf.start.x, e.dxf.start.y, e.dxf.end.x, e.dxf.end.y
        else:
            pts = list(e.get_points("xy"))
            if len(pts) != 2:
                continue
            (x1, y1), (x2, y2) = pts
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        longest = max(longest, dx, dy)
        if dy > 0 and dx <= 0.01 * dy:
            v_lines.append((x1 + x2) / 2)
        elif dx > 0 and dy <= 0.01 * dx:
            h_lines.append((y1 + y2) / 2)

    if not v_lines and not h_lines:
        return {"x": [], "y": []}

    eps = max(longest, 1.0) * 1e-6
    v_unique = _unique_positions(v_lines, eps)
    h_unique = _unique_positions(h_lines, eps)

    def tolerance(positions: List[float]) -> float:
        gaps = [b - a for a, b in zip(positions, positions[1:])]
        return 0.25 * min(gaps) if gaps else 0.05 * max(longest, 1.0)

    tol_x, tol_y = tolerance(v_unique), tolerance(h_unique)

    labels: List[Tuple[str, float, float]] = []
    for e in msp.query("TEXT MTEXT INSERT"):
        layer = e.dxf.layer or ""
        if e.dxftype() == "INSERT":
            if not (_GRID_RE.search(layer) or _GRID_RE.search(e.dxf.name or "")):
                continue
            p = e.dxf.insert
            labels.extend((_plain_text(a), p.x, p.y) for a in e.attribs)
        elif _GRID_RE.search(layer):
            p = _text_anchor(e)
            labels.append((_plain_text(e), p.x, p.y))

    snapped: Dict[str, Dict[str, List[float]]] = {"x": {}, "y": {}}
    for label, px, py in labels:
        label = label.strip()
        if not _GRID_LABEL_RE.match(label):
            continue
        options = []
        if v_unique:
            dist, pos = min((abs(px - x), x) for x in v_unique)
            if dist <= tol_x:
                options.append((dist / tol_x, "x", pos))
        if h_unique:
            dist, pos = min((abs(py - y), y) for y in h_unique)
            if dist <= tol_y:
                options.append((dist / tol_y, "y", pos))
        if options:
            _score, axis, pos = min(options)
            snapped[axis].setdefault(label.upper(), []).append(pos)

    return {
        axis: sorted((sum(ps) / len(ps), lbl) for lbl, ps in by_label.items())
        for axis, by_label in snapped.items()
    }


def _axis_label(value: float, axis: List[Tuple[float, str]]) -> str:
    if not axis:
        return ""
    if len(axis) == 1:
        return axis[0][1]
    first_span = axis[1][0] - axis[0][0]
    last_span = axis[-1][0] - axis[-2][0]
    if value < axis[0][0]:
        return axis[0][1] if axis[0][0] - value <= 0.1 * first_span else f"outside {axis[0][1]}"
    if value > axis[-1][0]:
        return axis[-1][1] if value - axis[-1][0] <= 0.1 * last_span else f"outside {axis[-1][1]}"
    for (lo, lo_label), (hi, hi_label) in zip(axis, axis[1:]):
        if lo <= value <= hi:
            span = hi - lo
            if value - lo <= 0.1 * span:
                return lo_label
            if hi - value <= 0.1 * span:
                return hi_label
            return f"{lo_label}-{hi_label}"
    return ""


def _grid_zone(x: float, y: float, grids: Dict[str, List[Tuple[float, str]]]) -> str:
    parts = [_axis_label(x, grids.get("x") or []), _axis_label(y, grids.get("y") or [])]
    return "/".join(p for p in parts if p)


def _dimension_record(e, layout_name: str) -> Dict[str, Any]:
    try:
        measurement = e.get_measurement()
    except Exception:
        measurement = e.dxf.get("actual_measurement")
    if not isinstance(measurement, (int, float)):
        measurement = None   # ordinate dims return a vector

    try:
        lfac = float(e.override().get("dimlfac", 1.0) or 1.0)
    except Exception:
        lfac = 1.0

    raw = str(e.dxf.get("text", "") or "")
    try:
        from ezdxf.tools.text import plain_mtext
        override = plain_mtext(raw).strip() if raw else ""
    except Exception:
        override = raw.strip()

    shown = measurement * lfac if measurement is not None else None
    if not override or override == "<>":
        displayed = _fmt_num(shown) if shown is not None else ""
    elif "<>" in override:
        displayed = override.replace("<>", _fmt_num(shown) if shown is not None else "<>")
    else:
        displayed = override

    point = e.dxf.get("text_midpoint") or e.dxf.get("defpoint")
    return {
        "layout": layout_name,
        "layer": e.dxf.layer,
        "dimtype": _DIMTYPES.get(e.dxf.get("dimtype", 0) & 7, "unknown"),
        "measurement": measurement,
        "dimlfac": lfac,
        "scaled_measurement": shown,
        "displayed": displayed,
        "literal_override": bool(override) and "<>" not in override and override.strip() != "",
        "x": point.x if point is not None else None,
        "y": point.y if point is not None else None,
    }


def _override_mismatch(dim: Dict[str, Any]) -> bool:
    """
    A literal text override matching neither the geometry nor its DIMLFAC-scaled
    value, under any power-of-ten unit change (so '5.00 m' over 5000 mm passes).
    """
    if not dim["literal_override"] or not dim["measurement"]:
        return False
    match = re.search(r"-?\d+(?:[.,]\d+)?", dim["displayed"])
    if not match:
        return False
    written = float(match.group(0).replace(",", "."))
    for expected in {dim["measurement"], dim["scaled_measurement"]}:
        for factor in (1, 10, 100, 1000, 0.1, 0.01, 0.001):
            target = expected * factor
            if abs(written - target) <= DIMENSION_OVERRIDE_TOLERANCE * max(abs(target), 1e-9):
                return False
    return True


def _layout_display_name(layout) -> str:
    return "Model space" if layout.name.upper() == "MODEL" else layout.name


def _extract_dxf_entities(doc) -> Dict[str, Any]:
    """Deterministic entity-level read of the whole DXF - layers, entity types, texts, blocks, dimensions."""
    units = _DXF_UNITS.get(doc.header.get("$INSUNITS", 0), "drawing units")
    msp = doc.modelspace()
    grids = _infer_grids(msp)

    entity_types: Counter = Counter()
    layer_counts: Counter = Counter()
    texts: List[Dict[str, Any]] = []
    inserts: List[Dict[str, Any]] = []
    dimensions: List[Dict[str, Any]] = []
    layouts: List[str] = []

    for layout in doc.layouts:
        if len(layout) == 0:
            continue
        name = _layout_display_name(layout)
        layouts.append(name)
        for e in layout:
            dxftype = e.dxftype()
            entity_types[dxftype] += 1
            layer = e.dxf.get("layer", "0")
            layer_counts[layer] += 1

            if dxftype in ("TEXT", "MTEXT"):
                text = _plain_text(e)
                if text:
                    p = _text_anchor(e)
                    texts.append({"layout": name, "layer": layer, "text": text, "x": p.x, "y": p.y})
            elif dxftype == "INSERT":
                p = e.dxf.insert
                attribs = {}
                for a in e.attribs:
                    value = _plain_text(a)
                    if value:
                        attribs[a.dxf.tag] = value
                inserts.append({
                    "layout": name, "layer": layer, "block": e.dxf.name,
                    "x": p.x, "y": p.y, "rotation": e.dxf.get("rotation", 0.0), "attribs": attribs,
                })
            elif dxftype == "DIMENSION":
                try:
                    dimensions.append(_dimension_record(e, name))
                except Exception as exc:
                    print(f"[drawing_analysis] skipped unreadable DIMENSION on layer {layer}: {exc}")

    for record in (*texts, *inserts, *dimensions):
        in_model = record["layout"] == "Model space"
        has_xy = record.get("x") is not None
        record["grid_zone"] = _grid_zone(record["x"], record["y"], grids) if (in_model and has_xy) else ""

    return {
        "units": units,
        "layouts": layouts,
        "layers": [{"name": n, "entity_count": c} for n, c in layer_counts.most_common()],
        "entity_types": dict(entity_types.most_common()),
        "grids": grids,
        "texts": texts,
        "inserts": inserts,
        "dimensions": dimensions,
    }


def _dxf_location(record: Dict[str, Any], units: str) -> Tuple[str, str]:
    """(grid, zone) for _cite - coordinates stand in for a zone when no grid was inferred."""
    if record.get("grid_zone"):
        return record["grid_zone"], ""
    if record.get("x") is not None:
        return "", f"coords ({_fmt_num(record['x'])}, {_fmt_num(record['y'])}) {units}"
    return "", ""


def _dxf_profile_items(dxf: Dict[str, Any]) -> Dict[str, Any]:
    """DXF entities -> requirements/quantities in DocumentProfile shape, plus review warnings."""
    units = dxf["units"]
    requirements: List[Dict[str, Any]] = []
    quantities: List[Dict[str, Any]] = []
    warnings: List[str] = []

    def req(topic, statement, value, reference, category, view="plan"):
        return {
            "topic": topic, "statement": statement, "value": value, "reference": reference,
            "discipline": "structural", "category": category, "view": view,
            "legibility": "clear", "in_revision_cloud": False, "baseline_ids": [], "source": "dxf",
            "quote": str(value or ""),
        }

    # Grid spacings - measurement-level geometry straight from coordinates
    for axis_name, axis in (("x", dxf["grids"]["x"]), ("y", dxf["grids"]["y"])):
        for (p1, l1), (p2, l2) in zip(axis, axis[1:]):
            spacing = _fmt_num(abs(p2 - p1))
            requirements.append(req(
                f"Grid spacing {l1}-{l2}",
                f"Centre-to-centre spacing between grid lines {l1} and {l2}, measured from DXF "
                f"model-space coordinates: {spacing} {units}.",
                f"{spacing} {units}",
                _cite("DXF Model space", grid=f"{l1}-{l2}"),
                "grid_geometry",
            ))

    # Block attributes - grouped so a mark repeated at 24 columns is one item with 24 locations
    grid_blocks = {
        ins["block"] for ins in dxf["inserts"]
        if _GRID_RE.search(ins["layer"] or "") or _GRID_RE.search(ins["block"] or "")
    }
    groups: Dict[Tuple, List[Dict[str, Any]]] = {}
    for ins in dxf["inserts"]:
        if ins["attribs"] and ins["block"] not in grid_blocks:
            key = (ins["block"], ins["layer"], tuple(sorted(ins["attribs"].items())))
            groups.setdefault(key, []).append(ins)

    for (block, layer, attribs), members in list(groups.items())[:MAX_DXF_ATTRIBUTE_GROUPS]:
        first = members[0]
        attr_text = "; ".join(f"{tag}={val}" for tag, val in attribs)
        grid, zone = _dxf_location(first, units)
        locations = []
        for m in members:
            g, z = _dxf_location(m, units)
            loc = f"{m['layout']} {('grid ' + g) if g else z}".strip()
            if loc not in locations:
                locations.append(loc)
        more = f" (+{len(members) - 1} more)" if len(members) > 1 else ""
        requirements.append(req(
            f"Block {block}: " + ", ".join(f"{t}={v}" for t, v in attribs[:2]),
            f"Block '{block}' on layer {layer} carries attributes {attr_text}; "
            f"{len(members)} reference(s) at: {', '.join(locations[:6])}"
            + (" ..." if len(locations) > 6 else "") + ".",
            attr_text,
            _cite(f"DXF {first['layout']}", grid=grid, zone=zone) + more,
            _hint_category(block, layer),
            view="title_block" if _TITLE_BLOCK_RE.search(f"{block} {layer}") else "plan",
        ))
    if len(groups) > MAX_DXF_ATTRIBUTE_GROUPS:
        warnings.append(
            f"Only the first {MAX_DXF_ATTRIBUTE_GROUPS} of {len(groups)} distinct attributed block "
            f"groups in the DXF were carried into the comparison."
        )

    # Block counts - the one quantity a DXF gives exactly (model space only)
    counts: Counter = Counter()
    count_layers: Dict[str, set] = {}
    anonymous = 0
    for ins in dxf["inserts"]:
        if ins["layout"] != "Model space" or ins["block"] in grid_blocks:
            continue
        if ins["block"].startswith("*"):
            anonymous += 1
            continue
        if _TITLE_BLOCK_RE.search(ins["block"]):
            continue
        counts[ins["block"]] += 1
        count_layers.setdefault(ins["block"], set()).add(ins["layer"])
    for block, n in counts.most_common():
        layers = ", ".join(sorted(count_layers[block])[:3])
        quantities.append({
            "description": f"{block} ({layers})",
            "amount": n,
            "unit": "nr",
            "reference": "DXF Model space | block reference count",
        })
    if anonymous:
        warnings.append(
            f"{anonymous} anonymous/dynamic block reference(s) in DXF model space could not be "
            f"identified by name and were not counted - verify those quantities manually."
        )

    # Dimension text overrides that contradict the geometry they dimension
    mismatches = [d for d in dxf["dimensions"] if _override_mismatch(d)]
    if mismatches:
        shown = []
        for d in mismatches[:15]:
            grid, zone = _dxf_location(d, units)
            scale = f", DIMLFAC {_fmt_num(d['dimlfac'])}" if d["dimlfac"] != 1 else ""
            shown.append(
                f"displays '{d['displayed']}' but the dimensioned geometry measures "
                f"{_fmt_num(d['measurement'])} {units}{scale} "
                f"({_cite('DXF ' + d['layout'], grid=grid, zone=zone)}, layer {d['layer']})"
            )
        more = f"; and {len(mismatches) - 15} more" if len(mismatches) > 15 else ""
        warnings.append(
            f"{len(mismatches)} dimension(s) in the DXF have overridden text that does not match the "
            f"drawn geometry - engineer review required: " + "; ".join(shown) + more
        )

    if not dxf["grids"]["x"] and not dxf["grids"]["y"]:
        warnings.append(
            "No grid lines could be inferred from the DXF (no axis-parallel lines with grid labels on a "
            "GRID/AXIS layer) - DXF items are cited by coordinates instead of grid references."
        )
    if units == "drawing units":
        warnings.append(
            "The DXF has no drawing units set ($INSUNITS) - DXF measurements are reported in "
            "unitless drawing units."
        )

    grid_entities = [f"Grid {lbl}" for axis in ("x", "y") for _p, lbl in dxf["grids"][axis]]
    return {
        "requirements": requirements,
        "quantities": quantities,
        "standards": [],
        "entities": grid_entities,
        "notes": "",
        "warnings": warnings,
    }


def _dxf_summary(dxf: Dict[str, Any]) -> Dict[str, Any]:
    block_counts = Counter(ins["block"] for ins in dxf["inserts"])
    return {
        "units": dxf["units"],
        "layouts": dxf["layouts"],
        "layer_count": len(dxf["layers"]),
        "layers": dxf["layers"][:60],
        "entity_types": dxf["entity_types"],
        "block_counts": dict(block_counts.most_common(40)),
        "grid_labels": {axis: [lbl for _p, lbl in dxf["grids"][axis]] for axis in ("x", "y")},
        "text_count": len(dxf["texts"]),
        "dimension_count": len(dxf["dimensions"]),
    }


def _dxf_context(dxf: Dict[str, Any]) -> str:
    """Short CAD context for the sheet-overview prompt - layer/block names hint at the structural system."""
    layers = ", ".join(layer["name"] for layer in dxf["layers"][:40])
    blocks = ", ".join(b for b, _n in Counter(i["block"] for i in dxf["inserts"]).most_common(20) if not b.startswith("*"))
    return f"units {dxf['units']}; layers: {layers or 'none'}; blocks: {blocks or 'none'}"


# ---------------------------------------------------------------------------
# DXF sheet rendering (visual source only when no PDF was supplied)
# ---------------------------------------------------------------------------

def _render_dxf_layout(doc, layout, out_path: Path) -> None:
    """
    Rasterize one DXF layout (model space or a paper-space sheet) to PNG.

    Forces a white background with black linework regardless of the
    drawing's own layer/entity colors (many CAD drawings assume a black
    viewport background, where ACI color 7 renders as white - left at
    defaults, those entities become invisible once saved onto a white PNG).
    A vision model also reads plain black-on-white line art more reliably
    than arbitrary CAD colors.

    The initial figsize passed to plt.figure() is a placeholder only -
    finalize=True below overrides it to match the layout's content aspect
    ratio, so the actual DPI used is computed AFTER that from the resulting
    figure size, to hit RENDER_TARGET_LONG_PX on the long side regardless of
    how wide/tall/square this particular layout turns out to be.
    """
    import matplotlib
    matplotlib.use("Agg")   # headless - no display needed
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import RenderContext, Frontend
    from ezdxf.addons.drawing.config import Configuration, BackgroundPolicy, ColorPolicy
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    fig = plt.figure(figsize=(10, 10))
    ax = fig.add_axes([0, 0, 1, 1])
    ctx = RenderContext(doc)
    backend = MatplotlibBackend(ax)
    cfg = Configuration(
        background_policy=BackgroundPolicy.WHITE,
        color_policy=ColorPolicy.BLACK,
    )
    Frontend(ctx, backend, config=cfg).draw_layout(layout, finalize=True)
    ax.set_axis_off()

    width_in, height_in = fig.get_size_inches()
    long_side_in = max(width_in, height_in, 0.1)
    dpi = max(MIN_RENDER_DPI, min(RENDER_TARGET_LONG_PX / long_side_in, MAX_RENDER_DPI))

    fig.savefig(str(out_path), dpi=dpi, facecolor="white")
    plt.close(fig)


def _select_layouts(doc) -> List[Any]:
    """Every non-empty paper-space sheet, or model space when no sheets are set up."""
    paper_layouts = [
        layout for layout in doc.layouts
        if layout.name.upper() != "MODEL" and len(layout) > 0
    ]
    if paper_layouts:
        return paper_layouts

    msp = doc.modelspace()
    return [msp] if len(msp) > 0 else []


def _measure_dxf_text_height(layout) -> Optional[float]:
    """Median TEXT/MTEXT character height in the layout, in DXF drawing units."""
    heights: List[float] = []
    for e in layout:
        dxftype = e.dxftype()
        h = None
        if dxftype == "TEXT":
            h = getattr(e.dxf, "height", None)
        elif dxftype == "MTEXT":
            h = getattr(e.dxf, "char_height", None)
        if h and h > 0:
            heights.append(h)
    if not heights:
        return None
    heights.sort()
    return heights[len(heights) // 2]


def _dxf_px_per_unit(layout, render_path: Path) -> Optional[float]:
    """
    World-units-to-render-pixels scale for a layout, measured empirically by
    comparing the ACTUAL saved render's pixel size against the layout's
    entity bounding box - robust to whatever aspect-fit sizing
    ezdxf's Frontend.finalize() applied when the figure was rendered (see
    _render_dxf_layout), rather than assuming a particular figsize/DPI.
    """
    try:
        import ezdxf.bbox as bbox_mod
        from PIL import Image

        box = bbox_mod.extents(layout, fast=True)
        if box is None or not box.has_data:
            return None
        size = box.size
        world_span = max(size.x, size.y)
        if world_span <= 0:
            return None

        img_w, img_h = Image.open(render_path).size
        px_span = max(img_w, img_h)
        return px_span / world_span
    except Exception:
        return None


def _dxf_sheets(doc, out_dir: Path) -> List[Dict[str, Any]]:
    sheets: List[Dict[str, Any]] = []
    for layout in _select_layouts(doc):
        name = _layout_display_name(layout)
        render_path = out_dir / f"{_safe_name(name)}_full.png"
        _render_dxf_layout(doc, layout, render_path)

        text_height_units = _measure_dxf_text_height(layout)
        px_per_unit = _dxf_px_per_unit(layout, render_path)
        sheets.append({
            "name": name,
            "render_path": render_path,
            "text_height_px": (text_height_units * px_per_unit) if (text_height_units and px_per_unit) else None,
            "text_lines": [],
        })
    return sheets


# ---------------------------------------------------------------------------
# PDF sheet rendering - the primary visual source
# ---------------------------------------------------------------------------

def _pdf_page_dpi(page) -> int:
    """DPI needed to hit RENDER_TARGET_LONG_PX on this page's long side (PyMuPDF requires an int)."""
    rect = page.rect
    long_in = max(rect.width, rect.height, 1.0) / 72.0
    dpi = max(MIN_RENDER_DPI, min(RENDER_TARGET_LONG_PX / long_in, MAX_RENDER_DPI))
    return int(round(dpi))


def _pdf_text_lines(page, dpi: int) -> List[Dict[str, Any]]:
    """
    Text-layer lines with their bbox in render pixels. Extraction coordinates
    are in the unrotated page, while the render is rotated - rotation_matrix
    maps one onto the other so text lands on the right tile.
    """
    import pymupdf

    scale = dpi / 72.0
    lines: List[Dict[str, Any]] = []
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:
        return lines
    for block in blocks:
        for line in block.get("lines", []):
            text = decode_symbol_font("".join(span.get("text", "") for span in line.get("spans", []))).strip()
            if not text:
                continue
            rect = pymupdf.Rect(line["bbox"]) * page.rotation_matrix
            sizes = [s.get("size") for s in line.get("spans", []) if s.get("size")]
            lines.append({
                "text": text,
                "bbox": (rect.x0 * scale, rect.y0 * scale, rect.x1 * scale, rect.y1 * scale),
                "size_px": (sorted(sizes)[len(sizes) // 2] * scale) if sizes else None,
            })
    return lines


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")


def _image_sheets(image_path: str, out_dir: Path) -> List[Dict[str, Any]]:
    """A scanned sketch / photo of a sheet: one raster sheet, rescaled to the render target."""
    from PIL import Image

    render_path = out_dir / "Image_1_full.png"
    with Image.open(image_path) as img:
        img = img.convert("RGB")
        scale = RENDER_TARGET_LONG_PX / max(img.size)
        if scale < 1 or scale > 1.5:
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
        img.save(render_path)
    return [{"name": "Image 1", "render_path": render_path, "text_height_px": None, "text_lines": []}]


def _pdf_sheets(pdf_path: str, out_dir: Path) -> List[Dict[str, Any]]:
    import pymupdf

    sheets: List[Dict[str, Any]] = []
    doc = pymupdf.open(pdf_path)
    try:
        for i, page in enumerate(doc):
            name = f"Page {i + 1}"
            render_path = out_dir / f"{_safe_name(name)}_full.png"
            dpi = _pdf_page_dpi(page)
            page.get_pixmap(dpi=dpi).save(str(render_path))

            text_lines = _pdf_text_lines(page, dpi)
            sizes = sorted(line["size_px"] for line in text_lines if line["size_px"])
            sheets.append({
                "name": name,
                "render_path": render_path,
                "text_height_px": sizes[len(sizes) // 2] if sizes else None,
                "text_lines": text_lines,
            })
    finally:
        doc.close()
    return sheets


# ---------------------------------------------------------------------------
# Tiling (format-agnostic - operates on a rendered PNG + an overlap value)
# ---------------------------------------------------------------------------

def _region_label(box: Tuple[int, int, int, int], width: int, height: int) -> str:
    cx = (box[0] + box[2]) / 2 / max(width, 1)
    cy = (box[1] + box[3]) / 2 / max(height, 1)
    horizontal = "left" if cx < 1 / 3 else ("right" if cx > 2 / 3 else "centre")
    vertical = "top" if cy < 1 / 3 else ("bottom" if cy > 2 / 3 else "middle")
    if horizontal == "centre" and vertical == "middle":
        return "sheet centre"
    return f"{vertical}-{horizontal} of sheet"


def _tile_image(png_path: Path, out_dir: Path, prefix: str, overlap: int) -> List[Dict[str, Any]]:
    """Slice a render into overlapping TILE_SIZE crops; each tile keeps its pixel box and sheet region."""
    from PIL import Image

    img = Image.open(png_path)
    width, height = img.size

    if width <= TILE_SIZE and height <= TILE_SIZE:
        return [{"path": png_path, "box": (0, 0, width, height), "region": "whole sheet"}]

    step = max(TILE_SIZE - overlap, 1)
    xs = list(range(0, max(width - TILE_SIZE, 0) + 1, step)) or [0]
    ys = list(range(0, max(height - TILE_SIZE, 0) + 1, step)) or [0]
    if xs[-1] + TILE_SIZE < width:
        xs.append(width - TILE_SIZE)
    if ys[-1] + TILE_SIZE < height:
        ys.append(height - TILE_SIZE)

    tiles: List[Dict[str, Any]] = []
    for row, y in enumerate(ys):
        for col, x in enumerate(xs):
            box = (x, y, min(x + TILE_SIZE, width), min(y + TILE_SIZE, height))
            tile_path = out_dir / f"{prefix}_r{row}_c{col}.png"
            img.crop(box).save(tile_path)
            tiles.append({"path": tile_path, "box": box, "region": _region_label(box, width, height)})
    return tiles


def _lines_in_tile(text_lines: List[Dict[str, Any]], box: Tuple[int, int, int, int]) -> List[str]:
    x0, y0, x1, y1 = box
    inside = []
    for line in text_lines:
        bx0, by0, bx1, by1 = line["bbox"]
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            inside.append(line["text"])
    return inside


# ---------------------------------------------------------------------------
# Sheet overview - one vision call on the full render, before tiling
# ---------------------------------------------------------------------------

_OVERVIEW_SYSTEM = """\
You are a senior multi-discipline engineer (structural, architectural, civil,
mechanical, electrical) looking at ONE FULL SHEET of a construction drawing or
sketch (the whole sheet, not a zoomed-in crop). Capture sheet-level context for
a later, zoomed-in pass. Read only what is visibly present.

Return a JSON object with exactly these keys:

{
  "sheet_number": "<drawing/sheet number from the title block, else null>",
  "sheet_title": "<drawing title from the title block, else null>",
  "revision": "<current revision from the title block or revision table, else null>",
  "revision_date": "<date of the current revision, else null>",
  "status": "<issue status stamp, e.g. for tender / for construction, else null>",
  "scale": "<scale(s) noted, else null>",
  "discipline": "<structural|architectural|civil|mechanical|electrical|plumbing|landscape|general>",
  "sheet_type": "<plan|section|elevation|detail|single_line_diagram|riser_diagram|schematic|schedule|site_plan|sketch|mixed>",
  "areas": ["<building / block / plot / zone / floor names this sheet covers, as written>"],
  "views": ["<title of every view on the sheet - section, detail, plan or diagram titles, e.g. 'SEC 01-01', 'DETAIL 3', 'TRANSFORMER SUBSTATION TR.2'>"],
  "structural_system": {
    "framing_type": "<framing type only if stated in notes or unambiguously drawn, else null>",
    "lateral_system": "<lateral load-resisting system only if stated or unambiguously drawn, else null>",
    "evidence": "<what on the sheet shows this, else null>"
  },
  "grid_labels": ["<every label inside a grid bubble at the end of a grid line>"],
  "level_datums": ["<every level value visible, as written>"],
  "schedules_present": ["<title of every schedule/table on the sheet>"],
  "revision_clouds": ["<for each revision cloud: its delta/revision tag and what it encloses>"],
  "legend": "<hatch/material legend - which pattern means which material - else null>"
}

Rules:
- Return ONLY a valid JSON object. No markdown fences, no preamble.
- null / [] for anything not visibly present. Never guess a sheet number,
  revision or structural system that is not legible or stated.
- structural_system applies to structural sheets only - null on MEP,
  architectural and site sheets.
- Many sheets (details, sections, schedules, diagrams) have no grid lines at
  all - return [] for grid_labels unless you can see grid bubbles at the ends
  of grid lines. Section titles, panel/bay marks and equipment tags are NOT
  grid labels - put view titles in "views".
- Red/coloured clouds and hand mark-ups on a sketch are the author's
  highlights of an issue - list each in revision_clouds with what it encloses.
- The sheet may be drawn sideways (text running vertically) - read the title
  block and labels in whatever direction they run.
- Text that is present but illegible at this zoom must be left out - the
  zoomed-in pass reads it.
"""

MAX_TEXT_LINES_OVERVIEW = 300


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _verify_against_text_layer(sheet: Dict[str, Any], overview: Dict[str, Any]) -> None:
    """
    On a vector PDF the text layer is ground truth: a grid label must exist as
    its own string and a level datum inside some string, or it was invented.
    """
    corpus = [_normalise_text(line["text"]) for line in sheet["text_lines"]]
    if not corpus:
        return
    standalone = set(corpus)
    overview["grid_labels"] = [g for g in overview["grid_labels"] if _normalise_text(g) in standalone]
    overview["level_datums"] = [
        d for d in overview["level_datums"]
        if re.search(r"\d", d) and any(_normalise_text(d) in line for line in corpus)
    ]


def _extract_sheet_overview(sheet: Dict[str, Any], doc_label: str, dxf_context: str) -> Dict[str, Any]:
    user = f"Sheet/layout name in the file: {sheet['name']}\n"
    if dxf_context:
        user += f"CAD context from the DXF: {dxf_context}\n"
    if sheet["text_lines"]:
        lines = [line["text"] for line in sheet["text_lines"][:MAX_TEXT_LINES_OVERVIEW]]
        user += (
            "\nTEXT-LAYER STRINGS ON THIS SHEET (exact spelling from the PDF's own text layer - "
            "read the title block values from these):\n" + "\n".join(f"- {t}" for t in lines) + "\n"
        )
    user += (
        "\nRead this sheet's title block, revision information, structural-system indications, "
        "grid labels, level datums, schedules and legend."
    )
    try:
        result = _run_json(
            _get_vision_llm("drawing_overview"), system=_OVERVIEW_SYSTEM, user=user, images=[sheet["render_path"]], temperature=0.1,
        )
    except Exception as exc:
        print(f"[drawing_analysis] {doc_label} sheet '{sheet['name']}' overview call failed: {exc}")
        result = {}

    system = result.get("structural_system") if isinstance(result.get("structural_system"), dict) else {}
    overview = {
        "sheet_number": _text(result.get("sheet_number")),
        "sheet_title": _text(result.get("sheet_title")),
        "revision": _text(result.get("revision")),
        "revision_date": _text(result.get("revision_date")),
        "status": _text(result.get("status")),
        "scale": _text(result.get("scale")),
        "discipline": _text(result.get("discipline")) or "general",
        "sheet_type": _text(result.get("sheet_type")).lower(),
        "areas": _text_list(result.get("areas")),
        "views": _text_list(result.get("views")),
        "framing_type": _text(system.get("framing_type")),
        "lateral_system": _text(system.get("lateral_system")),
        "system_evidence": _text(system.get("evidence")),
        "grid_labels": _text_list(result.get("grid_labels")),
        "level_datums": _text_list(result.get("level_datums")),
        "schedules_present": _text_list(result.get("schedules_present")),
        "revision_clouds": _text_list(result.get("revision_clouds")),
        "legend": _text(result.get("legend")),
    }
    _verify_against_text_layer(sheet, overview)
    return overview


def _overview_result(overview: Dict[str, Any], sheet_ref: str) -> Dict[str, Any]:
    """Sheet-level facts become items too - structural system is first in the framework order."""
    reference = _cite(f"Sheet {sheet_ref}", zone="title block / whole sheet")
    discipline = overview["discipline"]

    def req(topic, statement, value, category, view):
        return {
            "topic": topic, "statement": statement, "value": value, "reference": reference,
            "discipline": discipline, "category": category, "view": view, "legibility": "clear",
            "quote": str(value or ""),
            "in_revision_cloud": False, "baseline_ids": [], "source": "vision",
        }

    evidence = f" ({overview['system_evidence']})" if overview["system_evidence"] else ""
    requirements = []
    if overview["framing_type"]:
        requirements.append(req(
            "Structural system - framing type",
            f"Sheet {sheet_ref} indicates the framing type as {overview['framing_type']}{evidence}.",
            overview["framing_type"], "structural_system", "note",
        ))
    if overview["lateral_system"]:
        requirements.append(req(
            "Structural system - lateral system",
            f"Sheet {sheet_ref} indicates the lateral load-resisting system as {overview['lateral_system']}{evidence}.",
            overview["lateral_system"], "structural_system", "note",
        ))
    if overview["revision"]:
        dated = f" dated {overview['revision_date']}" if overview["revision_date"] else ""
        requirements.append(req(
            "Title block - current revision",
            f"Sheet {sheet_ref} title block shows revision {overview['revision']}{dated}.",
            overview["revision"], "annotations_notes", "title_block",
        ))
    if overview["status"]:
        requirements.append(req(
            "Title block - issue status",
            f"Sheet {sheet_ref} is stamped '{overview['status']}'.",
            overview["status"], "annotations_notes", "title_block",
        ))
    for cloud in overview["revision_clouds"]:
        requirements.append(req(
            "Revision cloud", f"Sheet {sheet_ref} carries a revision cloud: {cloud}.",
            None, "annotations_notes", "note",
        ))

    return {
        "requirements": requirements,
        "quantities": [],
        "standards": [],
        "entities": [f"Grid {g}" for g in overview["grid_labels"]] + overview.get("areas", []) + overview.get("views", []),
        "notes": "",
    }


# ---------------------------------------------------------------------------
# Per-tile extraction
# ---------------------------------------------------------------------------

_TILE_SYSTEM = f"""\
You are a senior multi-discipline engineer (structural, architectural, civil,
mechanical, electrical) reading ONE TILE (a zoomed-in crop) of a construction
drawing or sketch. You do not know what other documents say and must not
speculate about them.

A drawing's meaning lives in the RELATIONSHIP between an annotation and the
geometry it labels. For every dimension, leader, tag, note, hatch, schedule
row, level mark or callout:
1. Follow the leader/arrow to the geometry it points at, or take the geometry
   immediately adjacent to a tag.
2. Identify what that geometry IS - member type, material (from the hatch
   legend, if given), opening, embedded item, equipment, pipe, cable, room.
3. Report both together as ONE item. Never report a bare number or bare label.

Reading rules by annotation type:
- LEVEL MARKS: record what the level refers to as the attribute - top of
  concrete (T.O.C), finished level (F.L / F.F.L), ground level (G.L / N.G.L),
  inside / outside grade (IN / OUT), invert (I.L), top of wall (T.O.W), bottom
  of foundation - and the element it belongs to. A level without its meaning
  is useless.
- IDENTIFIERS: section titles ("SEC 01-01"), bay/panel marks between posts
  ("BET D-004 & D-005"), manhole, panel, transformer and equipment tags are
  LOCATIONS or element ids - put them in "section_ref" or "element_id", never
  treat them as grid lines.
- SINGLE-LINE / RISER / SCHEMATIC DIAGRAMS: one item per feeder or device -
  element = the load/panel fed, attribute = "fed from" (value = source), and
  separate items for breaker rating, cable size/type, equipment rating (kVA,
  kW, A) and device counts as written.
- SCHEDULES and TABLES: read row by row - one item per mark and attribute.
- CLOUDS / HAND MARK-UPS: set in_revision_cloud true for items inside them.
- ORIENTATION: sheets are often drawn sideways - text may run vertically or
  upside down. Read it in whatever direction it runs.

Classify every item into exactly one category, working through them in this order:
{FRAMEWORK_TEXT}

The category is what the item GOVERNS, not where it is written: a general
note setting concrete strength is "materials", a note setting cover or
development length is "reinforcement", a feeder's source is
"routing_connections", a transformer rating is "mep_systems". "view" records
where it is written. The same element may legitimately appear in plan,
section and schedule: report each as drawn, with its "view"; do not reconcile them.

Return a JSON object with exactly these keys:

{{
  "items": [
    {{"category": "<category key>", "element": "<the element as identified on the drawing>",
      "element_id": "<tag / mark / number as written, else null>",
      "attribute": "<what the annotation specifies about it>", "value": "<exactly as written, null if unreadable>",
      "view": "plan|section|elevation|detail|schedule|diagram|note|legend",
      "location": "<building / zone / room as written, else null>", "grid_zone": "<grid axes, else null>",
      "section_ref": "<section / detail title or bay mark, else null>", "baseline_ids": []}}
  ],
  "quantities": [
    {{"description": "<what is counted or measured>", "amount": <number or null>, "unit": "<unit or null>", "reference": "<schedule/table/row reference, else null>"}}
  ],
  "standards": ["<ASTM / BS / EN / ISO / ACI / NFPA / ECP reference exactly as written>"],
  "entities": ["<named element, grid line, zone or system visible>"],
  "notes": "<anything important about this tile, e.g. blank margin, title block only, detail cut off at an edge>"
}}

Rules:
- Return ONLY valid JSON. No markdown fences, no preamble. Keep it compact:
  one item per line, no extra fields, no explanations.
- Add "legibility": "partial" or "unreadable" ONLY when a value is not fully
  legible, and "in_revision_cloud": true ONLY for items inside a revision
  cloud - omit both otherwise.
- NEVER invent data. If an element is visible but its value cannot be read,
  report it with "value": null and "legibility": "unreadable". If only part is
  legible, transcribe only that part and mark "partial".
- Report each annotation ONCE. Never repeat an item.
- Report only what IS shown. Never add an item saying something is not
  visible or not present in this tile - use "notes" for that.
- Transcribe values exactly as written - bar marks, sizes, grades, levels,
  ratings. No unit conversion, no normalised notation.
- Quantities: only quantities explicitly WRITTEN on the drawing (schedule or
  table rows, notes). Never count symbols yourself.
- If an annotation's target is cut off at the tile edge, omit it (an
  overlapping tile covers it) or mark it "partial".
- "baseline_ids": at most 2 ids from the supplied checklist, and only when the
  item IS that checklist element and attribute; [] otherwise (most items).
- Every text value must come from what is visible in this tile - never copy
  wording from these instructions.
"""

_LEGIBILITY = {"clear", "partial", "unreadable"}


def _tile_prompt(
    sheet_ref: str, overview: Dict[str, Any], tile: Dict[str, Any],
    tile_no: int, tile_total: int, text_lines: List[str], checklist_text: str,
    review_focus: str, dxf_quantities: bool, text_source: str = "text layer",
) -> str:
    title = f" - {overview['sheet_title']}" if overview["sheet_title"] else ""
    # The engineer's document label is deliberately left out - models copy it
    # back as if it were title-block text.
    lines = [f"Sheet {sheet_ref}{title}; tile {tile_no} of {tile_total} ({tile['region']})", ""]
    context = [
        ("Sheet type", overview.get("sheet_type")),
        ("Areas covered by this sheet", ", ".join(overview.get("areas") or [])),
        ("Views on this sheet", ", ".join(overview.get("views") or [])),
        ("Material/hatch legend", overview["legend"]),
        ("Scale", overview["scale"]),
        ("Grid labels on this sheet", ", ".join(overview["grid_labels"])),
        ("Level datums on this sheet", ", ".join(overview["level_datums"])),
        ("Schedules on this sheet", ", ".join(overview["schedules_present"])),
        ("Structural system indicated", ", ".join(p for p in (overview["framing_type"], overview["lateral_system"]) if p)),
    ]
    lines += [f"{name}: {value}" for name, value in context if value]

    if text_lines:
        shown = text_lines[:MAX_TEXT_LINES_PER_TILE]
        heading = (
            "TEXT-LAYER STRINGS IN THIS TILE (exact spelling from the PDF's own text layer - use them to "
            "transcribe values exactly; they carry no geometry, so you must still bind each one to what it labels):"
            if text_source == "text layer" else
            "TEXT READ FROM THIS TILE BY OCR (the only text known to be on it - may contain small OCR errors; "
            "every value you report must come from here or be clearly legible in the image; bind each one to "
            "what it labels):"
        )
        lines += ["", heading, *[f"- {t}" for t in shown]]
        if len(text_lines) > len(shown):
            lines.append(f"(+{len(text_lines) - len(shown)} more strings not listed)")

    if dxf_quantities:
        lines += ["", "Counts and grid spacings are measured separately from the DXF - do not derive quantities from geometry."]
    if review_focus:
        lines += [
            "",
            f"ENGINEER'S REVIEW FOCUS: {review_focus}. Read items relevant to it with particular care, but "
            f"still report everything else legible - critical issues outside the focus must not be lost.",
        ]
    lines += ["", checklist_text, "", "Read this tile and extract what it shows, binding each annotation to the geometry it labels."]
    return "\n".join(lines)


# "Nothing to see here" items small vision models emit despite the prompt -
# they are not information about the project and must never be compared.
_NOT_VISIBLE_RE = re.compile(
    r"\b(?:no|not|none)\b[^.]{0,80}\b(?:visible|shown|present|indicated|identified|identifiable|legible|found)\b"
    r"|^\s*(?:none|n/?a)\s*$",
    re.IGNORECASE,
)


def _is_filler(element: str, attribute: str, statement: str, value: Optional[str], legibility: str = "clear") -> bool:
    """A clear-legibility item with no value that only says something is absent from the crop."""
    if value or legibility != "clear":
        return False     # an unreadable value is a review item, not filler
    return bool(_NOT_VISIBLE_RE.search(" ".join((element, attribute, statement))))


# Echoes: the model handing back the prompt instead of the drawing - the tile
# header ("tile 3 of 12"), the sheet's own view labels as "items", or a
# baseline checklist line restated with no value read.
_BASELINE_ID_RE = re.compile(r"^B-\d{3}$", re.IGNORECASE)
_PROMPT_ECHO_RE = re.compile(r"\btile \d+ of \d+\b|\bsheet page \d+\b", re.IGNORECASE)
_META_ATTR_RE = re.compile(
    r"\b(?:view|section|detail|sheet|tile|page)\s*(?:title|reference|label|number|location|name)\b"
    r"|\bviews on this sheet\b",
    re.IGNORECASE,
)


def _norm_phrase(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _checklist_phrases(baseline: Optional[Dict[str, Any]]) -> set:
    phrases = set()
    for it in (baseline or {}).get("items") or []:
        phrases.add(_norm_phrase(f"{it.get('element')} {it.get('attribute')}"))
        phrases.add(_norm_phrase(it.get("element")))
    phrases.discard("")
    return phrases


def _is_echo(element: str, attribute: str, statement: str, value: Optional[str],
             overview: Optional[Dict[str, Any]], sheet_ref: str, checklist: Optional[set]) -> bool:
    if _PROMPT_ECHO_RE.search(f"{value or ''} {statement}"):
        return True
    if _META_ATTR_RE.search(f"{element} {attribute}"):
        labels = {_norm_phrase(v) for v in (overview or {}).get("views") or []} | {_norm_phrase(sheet_ref)}
        if not value or _norm_phrase(value) in labels:
            return True
    if not value and checklist and (
        _norm_phrase(f"{element} {attribute}") in checklist or _norm_phrase(element) in checklist
    ):
        return True
    return False


def _item_to_requirement(item: Dict[str, Any], sheet_ref: str, region: str, valid_ids: set,
                         overview: Optional[Dict[str, Any]] = None,
                         checklist: Optional[set] = None) -> Optional[Dict[str, Any]]:
    element = _text(item.get("element"))
    attribute = _text(item.get("attribute"))
    statement = _text(item.get("statement")) or (
        " ".join(p for p in (element, attribute) if p)
        + (f": {_text(item.get('value'))}" if _text(item.get("value")) else ""))
    if not (element or statement):
        return None

    legibility = _text(item.get("legibility")).lower()
    legibility = legibility if legibility in _LEGIBILITY else "clear"
    if _is_filler(element, attribute, statement, _text(item.get("value")), legibility):
        return None
    if _is_echo(element, attribute, statement, _text(item.get("value")), overview, sheet_ref, checklist):
        return None
    if _BASELINE_ID_RE.match(_text(item.get("element_id"))):
        item = {**item, "element_id": None}      # a checklist id copied into the tag field
    value = None if legibility == "unreadable" else (_text(item.get("value")) or None)
    if legibility == "unreadable" and "unreadable" not in statement.lower():
        statement = f"{statement} (value unreadable on the drawing)".strip()

    if element and attribute:
        topic = f"{element} - {attribute}"
    else:
        topic = element or attribute or statement[:80]

    section_ref = _text(item.get("section_ref")) or _text(item.get("detail_ref"))
    location = _text(item.get("location"))
    if not location and overview and len(overview.get("areas") or []) == 1:
        location = overview["areas"][0]     # a sheet covering one area locates everything on it
    return {
        "topic": topic,
        "statement": statement or topic,
        "value": value,
        "reference": _cite(f"Sheet {sheet_ref}", grid=_text(item.get("grid_zone")), zone=region, detail=section_ref),
        "element_id": _text(item.get("element_id")),
        "location": location,
        "grid": _text(item.get("grid_zone")),
        "section_ref": section_ref,
        "sheet": sheet_ref,
        "sheet_region": region,
        "discipline": _text(item.get("discipline")).lower() or (overview or {}).get("discipline") or "general",
        "category": normalise_category(item.get("category")),
        "view": _text(item.get("view")).lower(),
        "legibility": legibility,
        "in_revision_cloud": bool(item.get("in_revision_cloud")),
        "baseline_ids": [bid for bid in (item.get("baseline_ids") or []) if bid in valid_ids],
        "source": "vision",
    }


def _value_tokens(value: str) -> List[str]:
    tokens = re.findall(r"[A-Z0-9]+(?:[./][A-Z0-9]+)*", str(value or "").upper())
    numeric = [t for t in tokens if any(c.isdigit() for c in t)]
    return numeric or [t for t in tokens if len(t) >= 3]


def _second_reading(requirements: List[Dict[str, Any]], prompt: str, tile: Dict[str, Any], strings: List[str]) -> int:
    """
    AI fleet second reader (drawing_second role, optional): an independent
    vision model reads the same tile. A value the sheet's text could not
    confirm is kept only as 'verify' unless the second reader saw it too.
    Returns how many values were newly flagged.
    """
    from fleet import profile
    pending = [r for r in requirements if r.get("value") and not r.get("value_unverified") and len(strings) < 3]
    pending += [r for r in requirements if r.get("value_unverified")]
    if not pending or not profile.feature("second_reader") or not profile.role("drawing_second"):
        return 0
    try:
        second = _run_json(_get_vision_llm("drawing_second"), system=_TILE_SYSTEM, user=prompt, images=[tile["path"]],
                           temperature=0.1)
    except Exception as exc:  # noqa: BLE001
        print(f"[drawing_analysis] second reading failed: {exc}")
        return 0
    seen = re.sub(r"\s+", "", " ".join(str(r.get("value") or "") for r in (second.get("requirements") or [])
                                       if isinstance(r, dict)).upper())
    flagged = 0
    for req in pending:
        tokens = _value_tokens(req["value"])
        agree = bool(tokens) and sum(1 for t in tokens if t in seen) / len(tokens) >= 0.5
        req["second_reading"] = "agrees" if agree else "differs"
        if not agree and not req.get("low_confidence"):
            req["legibility"] = "partial"
            req["low_confidence"] = True
            req["value_unverified"] = True
            flagged += 1
    return flagged


def _verify_values(requirements: List[Dict[str, Any]], strings: List[str]) -> int:
    """
    Hallucination guard. When the tile's real text is known (text layer or
    OCR), a value none of whose numbers/words appear in it was most likely
    invented by the vision model - small models produce plausible ratings and
    sizes on dense diagrams. Such values are kept for review but marked
    partial / verify, so they can never drive a critical finding.
    Returns how many values were flagged.
    """
    if len(strings) < 3:
        return 0
    corpus = re.sub(r"\s+", "", " ".join(strings).upper())
    flagged = 0
    for req in requirements:
        value = req.get("value")
        if not value:
            continue
        tokens = _value_tokens(value)
        if not tokens:
            continue
        found = sum(1 for t in tokens if t in corpus)
        if found / len(tokens) < 0.5:
            req["legibility"] = "partial"
            req["low_confidence"] = True
            req["value_unverified"] = True       # a flag only - never written into what users read
            flagged += 1
    return flagged


def _drawing_quote(req: Dict[str, Any], strings: List[str]) -> str:
    """
    What the drawing literally says for an item: the tile's own text strings
    (text layer or OCR) that carry its value; the value as transcribed when
    no string carries it; '' when nothing was read.
    """
    value = _text(req.get("value"))
    if not value:
        return ""
    want = re.sub(r"\s+", "", value).upper()
    hits = [s.strip() for s in strings if want and want in re.sub(r"\s+", "", s).upper()]
    if hits:
        return " | ".join(dict.fromkeys(hits))[:300]
    return value


TILE_BUDGET_BASE = 400            # output tokens for a tile with no known text
TILE_BUDGET_PER_STRING = 250      # ... plus this per text string the tile carries (an item is ~110 tokens)


def tile_output_budget(strings: Optional[List[str]]) -> int:
    """
    A tile can only hold as many facts as it has annotations. Bounding the
    reply by its text stops a model that starts permuting one callout into
    dozens of items (seen on real sheets) - complete items are still kept.
    Tiles whose text is unknown keep the full budget.
    """
    from nodes.llm_client import NUM_PREDICT
    if strings is None:
        return NUM_PREDICT
    return min(NUM_PREDICT, TILE_BUDGET_BASE + TILE_BUDGET_PER_STRING * len(strings))


def _extract_tile(prompt: str, tile: Dict[str, Any], sheet_ref: str, doc_label: str, valid_ids: set,
                  overview: Optional[Dict[str, Any]] = None, checklist: Optional[set] = None,
                  tile_strings: Optional[List[str]] = None, text_source: str = "") -> Dict[str, Any]:
    try:
        result = _run_json(_get_vision_llm(), system=_TILE_SYSTEM, user=prompt, images=[tile["path"]], temperature=0.1,
                           options={"num_predict": tile_output_budget(
                               tile_strings if text_source == "text layer" else None)})
    except Exception as exc:
        print(f"[drawing_analysis] {doc_label} sheet {sheet_ref} tile ({tile['region']}) vision call failed: {exc}")
        return {
            "requirements": [], "quantities": [], "standards": [], "entities": [],
            "notes": f"Vision LLM error on sheet {sheet_ref} ({tile['region']}): {exc}",
            "failed": True,
        }

    cut_off = bool(result.pop("_truncated", False))
    result.pop("_repeated", None)
    requirements = []
    seen_items = set()
    for item in result.get("items") or []:
        if isinstance(item, dict):
            sig = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
            if sig in seen_items:          # vision models sometimes repeat an item verbatim
                continue
            seen_items.add(sig)
            req = _item_to_requirement(item, sheet_ref, tile["region"], valid_ids, overview, checklist)
            if req:
                requirements.append(req)
    unverified = _verify_values(requirements, tile_strings or [])
    for req in requirements:
        req["quote"] = _drawing_quote(req, tile_strings or [])

    quantities = []
    for qty in result.get("quantities") or []:
        if not isinstance(qty, dict) or not _text(qty.get("description")):
            continue
        local_ref = _text(qty.get("reference"))
        quantities.append({
            **qty,
            "reference": f"Sheet {sheet_ref} | {local_ref}" if local_ref else _cite(f"Sheet {sheet_ref}", zone=tile["region"]),
        })

    return {
        "requirements": requirements,
        "quantities": quantities,
        "standards": _text_list(result.get("standards")),
        "entities": _text_list(result.get("entities")),
        "notes": _text(result.get("notes")),
        "unverified_values": unverified,
        "cut_off": cut_off,
    }


# ---------------------------------------------------------------------------
# Tile triage - skip blank crops, find the text each tile really carries
# ---------------------------------------------------------------------------

BLANK_TILE_INK = 0.003            # dark-pixel share below which a tile is empty margin
SHEET_TEXT_LAYER_MIN_CHARS = 200  # less than this and the "text layer" is just a stray label
DRAWING_OCR = os.environ.get("CONFLICTSCAN_DRAWING_OCR", "1") != "0"


def _ink_fraction(path: Path) -> float:
    from PIL import Image
    with Image.open(path) as img:
        gray = img.convert("L")
        gray.thumbnail((256, 256))
        hist = gray.histogram()
    return sum(hist[:160]) / max(1, sum(hist))


def _has_text_layer(sheet: Dict[str, Any]) -> bool:
    return sum(len(line["text"]) for line in sheet.get("text_lines") or []) >= SHEET_TEXT_LAYER_MIN_CHARS


def _tile_strings(sheet: Dict[str, Any], tile: Dict[str, Any]) -> Tuple[List[str], str]:
    """
    The text actually on this tile: from the PDF text layer when the sheet has
    one, otherwise transcribed by the OCR model (scans, and CAD exports whose
    text was converted to outlines). Returned strings are handed to the tile
    prompt and used to verify the values the vision model reports.
    """
    if _has_text_layer(sheet):
        return _lines_in_tile(sheet["text_lines"], tile["box"]), "text layer"
    if not DRAWING_OCR:
        return [], "none"
    try:
        text = _get_vision_llm("ocr").transcribe(tile["path"])
    except Exception as exc:
        print(f"[drawing_analysis] tile OCR failed: {exc}")
        return [], "none"
    lines = [line.strip(" |") for line in text.splitlines() if line.strip(" |-")]
    return lines, "ocr"


# ---------------------------------------------------------------------------
# Baseline coverage + review flags
# ---------------------------------------------------------------------------

def _baseline_coverage(baseline: Optional[Dict[str, Any]], checked_ids: set, requirements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not baseline:
        return []
    status = {bid: "not_located" for bid in checked_ids}
    located_at: Dict[str, List[str]] = {bid: [] for bid in checked_ids}
    for req in requirements:
        for bid in req.get("baseline_ids") or []:
            if bid not in status:
                continue
            if req.get("legibility") == "unreadable":
                if status[bid] == "not_located":
                    status[bid] = "unreadable"
            else:
                status[bid] = "located"
            if len(located_at[bid]) < 5 and req["reference"] not in located_at[bid]:
                located_at[bid].append(req["reference"])
    return [
        {**item, "status": status.get(item["id"], "not_checked"), "located_at": located_at.get(item["id"], [])}
        for item in baseline["items"]
    ]


def _review_warnings(label: str, baseline: Optional[Dict[str, Any]], coverage: List[Dict[str, Any]],
                     requirements: List[Dict[str, Any]]) -> List[str]:
    warnings: List[str] = []

    unreadable = [r for r in requirements if r.get("legibility") == "unreadable"]
    if unreadable:
        listed = "; ".join(f"{r['topic']} ({r['reference']})" for r in unreadable[:10])
        more = f"; and {len(unreadable) - 10} more" if len(unreadable) > 10 else ""
        warnings.append(
            f"{len(unreadable)} value(s) on '{label}' could not be read and were excluded from the "
            f"comparison - engineer review required: {listed}{more}."
        )

    if baseline:
        missing = [c for c in coverage if c["status"] == "not_located" and c["in_focus"]]
        if missing:
            listed = "; ".join(f"{c['id']} {c['element']} - {c['attribute']}" for c in missing[:10])
            more = f"; and {len(missing) - 10} more" if len(missing) > 10 else ""
            warnings.append(
                f"{len(missing)} baseline item(s) from '{baseline['reference_label']}' were not located on "
                f"'{label}' by the drawing reader: {listed}{more}. Absence is not confirmed - verify on the "
                f"drawing before treating any of these as an omission."
            )
        not_checked = sum(1 for c in coverage if c["status"] == "not_checked")
        if not_checked:
            warnings.append(
                f"The baseline from '{baseline['reference_label']}' has {len(coverage)} items; only the first "
                f"{MAX_CHECKLIST_ITEMS} (in-focus first) were given to the drawing reader as a checklist. The "
                f"other {not_checked} are still compared, but were not specifically searched for."
            )
        if baseline.get("fallback_batches"):
            warnings.append(
                f"Baseline classification failed for {baseline['fallback_batches']} batch(es) of "
                f"'{baseline['reference_label']}' - those statements were used as-is, without framework categories."
            )
    return warnings


# ---------------------------------------------------------------------------
# Profile one drawing
# ---------------------------------------------------------------------------

def _empty_profile(slot: str, label: str, source_name: str, note: str, drawing_inputs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "slot": slot,
        "label": label,
        "source_name": source_name,
        "char_count": 0,
        "chunk_count": 0,
        "total_chunk_count": 0,
        "focus_status": "no_focus",
        "requirements": [],
        "quantities": [],
        "standards": [],
        "entities": [],
        "internal_notes": note,
        "drawing_inputs": drawing_inputs,
        "sheets": [],
        "baseline": None,
        "baseline_coverage": [],
        "dxf_summary": None,
        "warnings": [f"Drawing '{label}': {note}"],
    }


def _missing_package_message(exc: ImportError, what: str) -> str:
    missing = exc.name or str(exc)
    package = {"pymupdf": "PyMuPDF", "fitz": "PyMuPDF", "ezdxf": "ezdxf", "PIL": "Pillow"}.get(missing, missing)
    return (
        f"The '{missing}' package required to read {what} isn't installed in the Python environment "
        f"running this app. Run: pip install {package} (using the same python/venv that launches api.py), "
        f"then restart the server."
    )


def build_drawing_profile(
    pdf_path: Optional[str], dxf_path: Optional[str], label: str, slot: str, source_name: str,
    review_focus: str = "", baseline: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Step 2 of the drawing protocol: read the drawing into an As-Drawn Set.
    receive_input guarantees at least one of *pdf_path* / *dxf_path* exists.
    """
    from nodes.document_analysis import _merge_chunk_results

    has_pdf = bool(pdf_path and os.path.exists(pdf_path))
    has_dxf = bool(dxf_path and os.path.exists(dxf_path))
    visual_source = "pdf" if has_pdf else "dxf"
    drawing_inputs = {"pdf": has_pdf, "dxf": has_dxf, "visual_source": visual_source}
    review_focus = (review_focus or "").strip()

    # One folder per drawing file - a set may hold many drawings read in turn.
    source_path = pdf_path if has_pdf else dxf_path
    render_dir = Path(os.path.dirname(source_path)) / f"_render_{slot}" / _safe_name(Path(source_path).stem)[:60]
    render_dir.mkdir(parents=True, exist_ok=True)

    mode = "PDF + DXF" if (has_pdf and has_dxf) else visual_source.upper()
    print(f"[drawing_analysis] '{label}': input {mode}, baseline={'yes' if baseline else 'no'}")
    warnings: List[str] = []
    if has_pdf and has_dxf:
        warnings.append(
            f"Drawing '{label}' was read from PDF + DXF: the DXF drives counts, grid spacings and block "
            f"attributes; the PDF provides the visual and annotation reading."
        )

    dxf_doc = None
    dxf = None
    if has_dxf:
        try:
            dxf_doc = _read_dxf(dxf_path)
            dxf = _extract_dxf_entities(dxf_doc)
            print(
                f"[drawing_analysis] '{label}': DXF {dxf['units']}, {len(dxf['layers'])} layer(s), "
                f"{len(dxf['inserts'])} block ref(s), {len(dxf['dimensions'])} dimension(s), "
                f"grids x={len(dxf['grids']['x'])} y={len(dxf['grids']['y'])}"
            )
        except ImportError as exc:
            message = _missing_package_message(exc, "DXF drawings")
            if not has_pdf:
                return _empty_profile(slot, label, source_name, message, drawing_inputs)
            warnings.append(f"DXF for '{label}' was not used: {message}")
        except Exception as exc:
            if not has_pdf:
                return _empty_profile(slot, label, source_name, f"Could not read the DXF: {exc}", drawing_inputs)
            warnings.append(f"DXF for '{label}' could not be read ({exc}) - continuing with the PDF only.")

    try:
        if has_pdf and pdf_path.lower().endswith(IMAGE_EXTENSIONS):
            sheets = _image_sheets(pdf_path, render_dir)
        elif has_pdf:
            sheets = _pdf_sheets(pdf_path, render_dir)
        else:
            sheets = _dxf_sheets(dxf_doc, render_dir)
    except ImportError as exc:
        return _empty_profile(slot, label, source_name, _missing_package_message(exc, f"{visual_source.upper()} drawings"), drawing_inputs)
    except Exception as exc:
        print(f"[drawing_analysis] '{label}': rendering failed: {exc}")
        return _empty_profile(slot, label, source_name, f"Could not render the drawing: {exc}", drawing_inputs)

    if not sheets:
        return _empty_profile(slot, label, source_name, "No non-empty sheet/page was found in the drawing.", drawing_inputs)
    if has_pdf and not any(_has_text_layer(s) for s in sheets) and not DRAWING_OCR:
        warnings.append(
            f"'{source_name}' has no text layer (scan, image, or text converted to outlines) - every value was read "
            f"visually, so legibility depends on the image quality."
        )

    # The checklist holds only what can apply to this drawing's structure (a tank drawing is not
    # searched for pump-station clauses); the full baseline is kept for pairing.
    reading_baseline = _scoped_baseline(baseline, source_name)
    checklist_text, checked_ids = _checklist(reading_baseline)
    dxf_context = _dxf_context(dxf) if dxf else ""

    results: List[Dict[str, Any]] = []
    if dxf:
        dxf_items = _dxf_profile_items(dxf)
        warnings.extend(dxf_items.pop("warnings"))
        results.append(dxf_items)

    debug_sheets: List[Dict[str, Any]] = []
    debug_tiles: List[Dict[str, Any]] = []
    total_tiles = blank_tiles = ocr_tiles = unverified_values = cut_off_tiles = failed_tiles = 0
    checklist = _checklist_phrases(baseline)

    from fleet import pmap
    # Sheet overviews side by side, then every tile of every sheet in ONE parallel pool: with a pool
    # per sheet, the model's parallel slots sit idle while each sheet's slowest tile finishes.
    overviews = pmap(lambda sh: _extract_sheet_overview(sh, label, dxf_context), sheets, role="drawing_overview")
    work = []
    for sheet, overview in zip(sheets, overviews):
        sheet_ref = overview["sheet_number"] or sheet["name"]
        results.append(_overview_result(overview, sheet_ref))

        overlap = _adaptive_overlap(sheet.get("text_height_px"))
        tiles = _tile_image(sheet["render_path"], render_dir, _safe_name(sheet["name"]), overlap)
        print(
            f"[drawing_analysis]   sheet {sheet_ref}: {len(tiles)} tile(s), overlap={overlap}px, "
            f"system='{overview['framing_type'] or '-'}', rev='{overview['revision'] or '-'}'"
        )
        debug_sheets.append({
            "name": sheet["name"], "sheet_reference": sheet_ref, "overlap_px": overlap,
            "text_height_px": sheet.get("text_height_px"), "text_line_count": len(sheet["text_lines"]),
            "overview": overview,
        })
        for i, tile in enumerate(tiles, start=1):
            if len(tiles) > 1 and _ink_fraction(tile["path"]) < BLANK_TILE_INK:
                blank_tiles += 1
                continue
            # A vector sheet carries every annotation in its text layer: a tile with no text on such a
            # sheet is pure linework - nothing to read, and anything "read" there would be invented.
            if len(tiles) > 1 and len(sheet.get("text_lines") or []) >= 20 and not _tile_strings(sheet, tile)[0]:
                blank_tiles += 1
                continue
            work.append((sheet, sheet_ref, overview, tiles, i, tile))
    total_tiles += len(work)

    def _read_tile(job):
        sheet, sheet_ref, overview, tiles, i, tile = job
        strings, text_source = _tile_strings(sheet, tile)
        prompt = _tile_prompt(
            sheet_ref, overview, tile, i, len(tiles), strings, checklist_text,
            review_focus, dxf_quantities=bool(dxf), text_source=text_source,
        )
        print(f"[drawing_analysis]   -> '{label}' sheet {sheet_ref} tile {i}/{len(tiles)} ({tile['region']}, "
              f"{len(strings)} {text_source} string(s))")
        result = _extract_tile(prompt, tile, sheet_ref, label, checked_ids, overview, checklist, strings,
                               text_source)
        result["second_reading_flags"] = _second_reading(result.get("requirements") or [], prompt, tile, strings)
        print(
            f"[drawing_analysis]      {len(result['requirements'])} item(s), "
            f"{len(result['quantities'])} quantity item(s)"
        )
        return sheet_ref, len(tiles), i, tile, strings, text_source, result

    for sheet_ref, n_tiles, i, tile, strings, text_source, result in pmap(_read_tile, work, role="drawing_reader"):
        ocr_tiles += text_source == "ocr"
        unverified_values += result.pop("unverified_values", 0) + result.pop("second_reading_flags", 0)
        cut_off_tiles += bool(result.pop("cut_off", False))
        failed_tiles += bool(result.pop("failed", False))
        results.append(result)
        debug_tiles.append({
            "sheet": sheet_ref, "tile": i, "total_tiles": n_tiles, "region": tile["region"],
            "box": tile["box"], "tile_image_path": str(tile["path"]), "text_source": text_source,
            "tile_strings": strings, **result,
        })

    if blank_tiles:
        print(f"[drawing_analysis] '{label}': {blank_tiles} blank tile(s) skipped")
    if ocr_tiles:
        warnings.append(
            f"'{source_name}' has no usable text layer - the text of {ocr_tiles} tile(s) was OCR-read and used to "
            f"anchor the values the drawing reader reported."
        )
    if failed_tiles:
        warnings.append(
            f"{failed_tiles} of {total_tiles} tile(s) of '{source_name}' could not be read (the vision model did not "
            f"answer) - nothing on those parts of the sheets was compared."
        )
    if cut_off_tiles:
        warnings.append(
            f"The drawing reader's reply was cut off on {cut_off_tiles} of {total_tiles} tile(s) of '{source_name}' "
            f"(the model got stuck repeating itself). Every complete item on those tiles was kept, but some details "
            f"of those tiles may not have been read."
        )
    if unverified_values:
        warnings.append(
            f"{unverified_values} value(s) reported for '{source_name}' do not appear in the sheet's own text and "
            f"may have been misread or invented by the vision model - they are marked 'verify' and cannot drive a "
            f"critical finding."
        )

    all_requirements = [req for r in results for req in r["requirements"]]
    coverage = _baseline_coverage(reading_baseline, checked_ids, all_requirements)
    warnings.extend(_review_warnings(label, reading_baseline, coverage, all_requirements))

    merged = _merge_chunk_results(results)
    merged["requirements"].sort(key=lambda r: category_rank(r.get("category", "")))
    if not merged["requirements"] and not merged["quantities"]:
        warnings.append(f"Nothing could be read from drawing '{label}' - it cannot be compared.")

    profile = {
        "slot": slot,
        "label": label,
        "source_name": source_name,
        "char_count": 0,               # not text-based - char count is not meaningful here
        "chunk_count": total_tiles,
        "total_chunk_count": total_tiles,
        "cut_off_tiles": cut_off_tiles,
        "failed_tiles": failed_tiles,
        "focus_status": "no_focus",    # drawings are always read in full; focus is applied at the diff
        **merged,
        "drawing_inputs": drawing_inputs,
        "sheets": [{"name": s["name"], "sheet_reference": s["sheet_reference"], **s["overview"]} for s in debug_sheets],
        "baseline": baseline,
        "baseline_coverage": coverage,
        "dxf_summary": _dxf_summary(dxf) if dxf else None,
        "warnings": warnings,
    }

    _write_debug_log(label, source_name, profile, debug_tiles, dxf)
    return profile


def _write_debug_log(label: str, source_name: str, profile: Dict[str, Any],
                     debug_tiles: List[Dict[str, Any]], dxf: Optional[Dict[str, Any]]) -> None:
    log_dir = debug_dir()
    if not log_dir:
        return
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{_safe_name(label)[:40]}_{_safe_name(os.path.splitext(source_name)[0])[:40]}"
        debug_path = os.path.join(log_dir, f"drawing_{name}_{timestamp}.json")
        with open(debug_path, "w", encoding="utf-8") as f:
            json.dump({
                "label": label,
                "source_name": source_name,
                "tiles": debug_tiles,
                "dxf_entities": dxf,
                "profile": profile,
            }, f, indent=2, default=str)
        print(f"[drawing_analysis] '{label}': debug log written to {debug_path}")
    except Exception as exc:
        print(f"[drawing_analysis] '{label}': failed to write debug log: {exc}")
