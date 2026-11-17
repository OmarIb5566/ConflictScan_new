"""
Multi-discipline analysis framework shared by drawing extraction, baseline
building, and the conflict scan. Order matters: every comparison is
evaluated, and every register is sorted, in this sequence.

The original structural keys are kept unchanged; MEP, levels, finishes and
scope categories were added because most real RFIs are raised on electrical,
mechanical and architectural work, not only on structure.
"""

import re

ANALYSIS_FRAMEWORK = [
    ("structural_system", "Structural system identification - framing type (moment frame, shear wall, "
                          "braced frame, flat slab...), load path, lateral system"),
    ("grid_geometry",     "Grid & geometry - axis lines, spans, setting-out coordinates, column locations, "
                          "storey heights"),
    ("levels_grading",    "Levels & grading - finished floor, top of concrete/wall, ground and external "
                          "grade levels, inverts, level differences between packages"),
    ("members",           "Members - beams, columns, slabs, walls, footings: size, type, "
                          "schedule vs plan consistency"),
    ("reinforcement",     "Reinforcement - bar diameter, count, spacing, lap/development length, clear cover"),
    ("materials",         "Materials - concrete grade, steel grade, section profiles, waterproofing systems"),
    ("mep_systems",       "MEP systems & equipment - ratings, capacities, configurations, single-line and "
                          "riser diagrams, panel schedules, device and fixture types"),
    ("routing_connections", "Routing & connections - feeders and their sources, cable/pipe routes and sizes, "
                          "tie-ins, sleeves, connection points between packages"),
    ("interfaces",        "Interfaces - openings, penetrations, embedded items, shafts, physical coordination "
                          "between disciplines"),
    ("finishes_specification", "Finishes & specification - architectural finishes, product types, schedules "
                          "(doors, windows, finishes, signage), specified standards"),
    ("scope_commercial",  "Scope & commercial - items shown but not priced, BOQ coverage, quantities missing "
                          "from the BOQ, changes against the tender basis"),
    ("annotations_notes", "Annotations & notes - general notes vs specific callouts, revision status, "
                          "title block data"),
    ("quantities",        "Quantities - counts, lengths, areas, volumes derivable from the document"),
]

FRAMEWORK_ORDER = {key: i for i, (key, _desc) in enumerate(ANALYSIS_FRAMEWORK)}

FRAMEWORK_TEXT = "\n".join(
    f"{i + 1}. {key}: {desc}" for i, (key, desc) in enumerate(ANALYSIS_FRAMEWORK)
)


def normalise_category(value) -> str:
    """Map a model-supplied category onto a framework key; '' when it matches none."""
    key = re.sub(r"[^a-z]+", "_", str(value or "").lower()).strip("_")
    return key if key in FRAMEWORK_ORDER else ""


def category_rank(key: str) -> int:
    return FRAMEWORK_ORDER.get(key, len(FRAMEWORK_ORDER))
