"""
Conflict Scan Node - ConflictScan / RFI Automation
===================================================
Compares the two set profiles produced by nodes/document_analysis.py and
emits a structured discrepancy register.

The two sets are treated symmetrically. Neither is assumed to be the
"correct" one - they are two stages or disciplines of the same project, and
where they disagree the client has to say which governs. That is what the RFI
is for.

What is looked for comes from knowledge/rfi_taxonomy.json - the kinds of
conflict, gap and missing information engineers actually raise RFIs about
(value conflicts between documents, sheet-vs-sheet inconsistencies,
cross-discipline mismatches, physical clashes, level conflicts, missing
information, BOQ scope gaps, design changes, tie-ins, code/constructability).

Scan strategy
-------------
1. ALIGNMENT    - items are paired only when they can describe the SAME
                  element at the SAME place: baseline links first, then
                  keyword overlap gated by location (building / floor /
                  section / grid / tag). Two drawing items with no shared
                  anchor are never compared - different sheets show
                  different places, and their values are expected to differ.
2. PAIR REVIEW  - candidate pairs go to the LLM in batches; it decides which
                  genuinely conflict and classifies each finding.
3. INTERNAL     - each set is checked against itself: the same element given
                  different values on different files/sheets (plan vs
                  section, one diagram vs another, schedule vs drawing).
4. INTERFACES   - items that share a location but come from different files
                  or disciplines are reviewed together for clashes, level
                  conflicts and coordination gaps.
5. COVERAGE     - items present in one set only, and items a document itself
                  leaves open (TBD / by others / hold), are reviewed as
                  omissions, scope gaps or missing information.
6. QUANTITIES   - matched quantity items are compared arithmetically - no
                  LLM call, so those findings are exact and reproducible.

Findings are then calibrated (severity defaults per issue class, OCR-derived
evidence never critical), near-duplicates merged with their evidence, ranked
and numbered D-001, D-002, ...
"""

import re
from typing import Dict, Any, List, Optional, Tuple

from nodes.analysis_framework import FRAMEWORK_TEXT, category_rank, normalise_category
from nodes.rfi_knowledge import (
    VALID_TYPES, best_issue_class, default_severity, default_type, issue_class_prompt,
    normalise_issue_class, not_discrepancy_prompt,
)

_llm_client_instance = None


def _get_llm():
    """Return the shared Ollama llm_client, importing it once on first call."""
    global _llm_client_instance
    if _llm_client_instance is None:
        from nodes.llm_client import llm_client
        _llm_client_instance = llm_client
    return _llm_client_instance


# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------
PAIR_BATCH_SIZE = 12          # candidate pairs per LLM call
UNMATCHED_BATCH_SIZE = 20     # unmatched requirements per LLM call
MIN_TOKEN_OVERLAP = 2         # shared keywords needed to call two topics related
MAX_UNMATCHED_REVIEWED = 120  # cap on omission review, keeps runtime bounded
MAX_INTERNAL_PAIRS = 48       # per set
MAX_INTERFACE_CLUSTERS = 16
MAX_CLUSTER_ITEMS = 24
MERGE_SIMILARITY = 0.6        # topic/description token Jaccard at which two findings are one
COVERAGE_SCOPE_RATIO = 0.25   # a set this much smaller is not expected to repeat the other's content

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}

# Words that carry no discriminating meaning when matching topics
_STOPWORDS = frozenset("""
a an the of for to and or in on at by with from as is are be shall must will
this that these those all any each per its it not no than then such other
general works work item items section clause required requirement requirements
provide provided providing including include included
""".split())

# Words that say WHAT KIND of place, not WHICH place - ignored when comparing locations
_LOCATION_GENERIC = frozenset("""
floor level area zone building block plot room the of and sec section detail det view plan bet between
grid axis axes part typ typical sheet page no nr
""".split())

_GEOMETRIC_RE = re.compile(
    r"level|elev|depth|height|width|thick|dimension|size|diameter|dia\b|invert|foundation|footing|"
    r"route|routing|position|location|clearance|opening|shaft|coordinat|t\.?o\.?c|f\.?f\.?l|g\.?l\b|"
    r"slope|span|offset|setting.?out|sleeve|levels?|ceiling|beam|slab|wall|pipe|cable|duct|manhole|tank",
    re.IGNORECASE,
)

_SHEET_REGION_RE = re.compile(r"(?:top|middle|bottom)-(?:left|right|centre)|sheet centre|whole sheet", re.IGNORECASE)
_GENERIC_SHEET_RE = re.compile(r"^(?:page|image|layout|model space)\s*\d*$", re.IGNORECASE)


def _tokens(*parts: Any) -> set:
    """Lower-cased significant keyword set for a topic/statement."""
    text = " ".join(str(p or "") for p in parts).lower()
    words = re.findall(r"[a-z0-9][a-z0-9\-/\.]{1,}", text)
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _overlap(a: set, b: set) -> int:
    return len(a & b)


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def _is_false(value: Any) -> bool:
    return value is False or str(value).strip().lower() == "false"


# ---------------------------------------------------------------------------
# Location model - which place / element an item is about
# ---------------------------------------------------------------------------

def _norm_id(value: Any) -> str:
    """'TR-04' / 'TR 4' / 'tr04' -> 'TR4'; '' for empty."""
    s = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return re.sub(r"(?<=[A-Z])0+(?=\d)", "", s)


def _loc_tokens(value: Any) -> set:
    """Distinctive place tokens: numbers without leading zeros, and non-generic words."""
    text = str(value or "").lower()
    if not text or text in ("n/a", "none", "null", "-"):
        return set()
    out = set()
    for tok in re.findall(r"[a-z]+|\d+", text):
        if tok.isdigit():
            out.add(str(int(tok)))
        elif tok not in _LOCATION_GENERIC and len(tok) > 1:
            out.add(tok)
    return out


def _ref_part(reference: str, prefix: str) -> str:
    """Pull 'Grid X' / 'Detail Y' back out of a drawing citation 'Sheet S | Grid X | Detail Y'."""
    for part in str(reference or "").split("|"):
        part = part.strip()
        if part.lower().startswith(prefix.lower() + " "):
            value = part[len(prefix) + 1:].strip()
            return "" if value.lower() in ("n/a", "unspecified") or _SHEET_REGION_RE.search(value) else value
    return ""


def _anchors(req: Dict[str, Any]) -> Dict[str, Any]:
    """Every way an item can be tied to a physical place or element."""
    grid = req.get("grid") or _ref_part(req.get("reference"), "Grid")
    section = req.get("section_ref") or _ref_part(req.get("reference"), "Detail")
    sheet = str(req.get("sheet") or "").strip()
    if _GENERIC_SHEET_RE.match(sheet):
        # "Page 1" of one file is not "Page 1" of another - only a real sheet
        # number identifies the same sheet across files.
        sheet = f"{req.get('source_file', '')}#{sheet}"
    return {
        "element_id": _norm_id(req.get("element_id")),
        "location": _loc_tokens(req.get("location")),
        "section": _loc_tokens(section),
        "grid": _loc_tokens(grid),
        "sheet": _norm_id(sheet),
    }


def _has_anchor(a: Dict[str, Any]) -> bool:
    return bool(a["element_id"] or a["location"] or a["section"] or a["grid"])


def _locations_compatible(ra: Dict[str, Any], rb: Dict[str, Any]) -> bool:
    """False when the two items are demonstrably about different places or different tagged elements."""
    a, b = _anchors(ra), _anchors(rb)
    if a["element_id"] and b["element_id"] and a["element_id"] != b["element_id"]:
        return False
    for key in ("location", "section", "grid"):
        if a[key] and b[key] and not (a[key] & b[key]):
            return False
    return True


def _shared_anchor(ra: Dict[str, Any], rb: Dict[str, Any]) -> bool:
    """True when the two items positively share an element tag, place, or sheet."""
    a, b = _anchors(ra), _anchors(rb)
    if a["element_id"] and a["element_id"] == b["element_id"]:
        return True
    if a["sheet"] and a["sheet"] == b["sheet"]:
        return True
    return any(a[k] and b[k] and (a[k] & b[k]) for k in ("location", "section", "grid"))


def _is_drawing_item(req: Dict[str, Any]) -> bool:
    return req.get("source") in ("vision", "dxf")


def _location_label(req: Dict[str, Any]) -> str:
    parts = []
    for key, prefix in (("location", ""), ("section_ref", ""), ("grid", "Grid ")):
        value = str(req.get(key) or "").strip()
        if value and value.lower() not in ("n/a", "null", "none"):
            parts.append(prefix + value)
    if not parts:
        for prefix in ("Grid", "Detail"):
            value = _ref_part(req.get("reference"), prefix)
            if value:
                parts.append(f"{'Grid ' if prefix == 'Grid' else ''}{value}")
    return " / ".join(dict.fromkeys(parts))


def _summary(req: Dict[str, Any]) -> str:
    statement = str(req.get("statement") or req.get("description") or "").strip()
    value = req.get("value")
    if value not in (None, "") and str(value) not in statement:
        statement = f"{statement} [{value}]"
    return statement


def _says(req: Dict[str, Any]) -> str:
    """What the document says, verbatim when its own words were captured."""
    quote = str(req.get("quote") or "").strip()
    return quote or _summary(req)


def compact_reference(reference: str) -> str:
    """
    'Sheet Page 1 | Zone top-left of sheet | Detail n/a' -> 'Sheet Page 1'.
    Sheet-position zones and empty detail slots say nothing to the reader of an
    RFI, so they are dropped; grids, details and clauses are kept.
    """
    parts = []
    for part in str(reference or "").split("|"):
        part = part.strip()
        if not part or re.search(r"\b(?:n/a|unspecified)$", part, re.I) or _SHEET_REGION_RE.search(part):
            continue
        parts.append(part)
    return ", ".join(parts)


def _evidence(req: Dict[str, Any], label: str) -> Dict[str, Any]:
    quote = str(req.get("quote") or "").strip()
    return {
        "side": req.get("side", ""),
        "document": label,
        "file": req.get("source_file", ""),
        "reference": compact_reference(req.get("reference")),
        "says": _says(req),
        "verbatim": bool(quote),
        "summary": _summary(req),
        "low_confidence": bool(req.get("low_confidence") or req.get("legibility") == "partial"),
    }


# ---------------------------------------------------------------------------
# Prompt fragments
# ---------------------------------------------------------------------------

_DRAWING_RULES = f"""
AT LEAST ONE SET CONTAINS CONSTRUCTION DRAWINGS. Additional rules:

Add "category" (one key below) to every finding:
{FRAMEWORK_TEXT}

Add "in_focus": true|false to every finding - whether it falls within the
engineer's REVIEW FOCUS (true for everything when no focus is given). Outside
the focus, report ONLY critical findings.

Genuine discrepancy vs representation difference: the same requirement drawn
or written differently is NOT a discrepancy - e.g. bar notation written two
ways for the same bar size, grade and spacing; a member shown in plan, section
and schedule with consistent values; the same dimension given in mm and in m.
Report such a case with "type": "representation_difference" so it is recorded
but not raised.

When the evidence is ambiguous - a value partly legible, two readings
possible, a reference that may point to a different element - report
"type": "ambiguity" so a human engineer reviews it. Never assume a reading.
Quote drawing references exactly as given (sheet | grid/zone | detail).
"""

_DRAWING_COVERAGE_RULE = """
A drawing shows only part of the project scope. A requirement absent from a
drawing is an omission only when THIS drawing is the one that must show it
(same element, area and discipline) - not merely because the other set covers
more scope.
"""


def _issue_rules(keys: List[str]) -> str:
    return (
        "Classify every finding with \"issue_class\" - one of:\n"
        + issue_class_prompt(keys)
        + "\n\nThese are NOT discrepancies - never report them:\n"
        + not_discrepancy_prompt()
        + "\n"
    )


_SEVERITY_GUIDE = """
Severity guide:
- critical: work cannot proceed as drawn, or proceeding risks rework, safety,
            structural adequacy or a physical clash on site
- major:    materially affects cost, programme, quality, scope or procurement
- minor:    should be tidied up but has no material consequence
"""

_FINDING_FIELDS = """
      "topic": "<short subject line, naming the element>",
      "issue_class": "<one issue_class key>",
      "type": "contradiction|ambiguity|scope_gap|clash|missing_information|design_change",
      "severity": "critical|major|minor",
      "location": "<building / area / floor / section / grid concerned, else null>",
      "description": "<what the problem is, in one or two sentences, with the values involved>",
      "impact": "<the practical cost, programme, quality or safety consequence>",
      "suggested_question": "<the single clarification to put to the client>\""""


# ---------------------------------------------------------------------------
# 1. Alignment
# ---------------------------------------------------------------------------

def _baseline_linked_pairs(
    profile_a: Dict[str, Any], profile_b: Dict[str, Any],
) -> Tuple[List[Tuple[Dict, Dict]], set, set]:
    """
    Pair each drawing item with the reference statement behind every baseline
    item it addresses - exact, where keyword overlap is only a guess. Returns
    (pairs, ids of A requirements used, ids of B requirements used).
    """
    profiles = {"a": profile_a, "b": profile_b}
    pairs: List[Tuple[Dict, Dict]] = []
    used_a, used_b, seen = set(), set(), set()

    for drawing_slot, prof in profiles.items():
        baseline = prof.get("baseline") or {}
        ref_slot = baseline.get("reference_slot")
        if ref_slot not in profiles or ref_slot == drawing_slot:
            continue
        ref_reqs = profiles[ref_slot].get("requirements") or []
        by_uid = {r.get("uid"): r for r in ref_reqs if r.get("uid")}
        by_id = {item["id"]: item for item in baseline.get("items") or []}

        for req in prof.get("requirements") or []:
            if req.get("legibility") == "unreadable":
                continue
            for bid in req.get("baseline_ids") or []:
                item = by_id.get(bid)
                if not item or item.get("source_kind") != "requirement":
                    continue
                ref_req = by_uid.get(item.get("source_uid"))
                if ref_req is None:
                    idx = item.get("source_index")
                    if not isinstance(idx, int) or not 0 <= idx < len(ref_reqs):
                        continue
                    ref_req = ref_reqs[idx]
                ra, rb = (ref_req, req) if ref_slot == "a" else (req, ref_req)
                if (id(ra), id(rb)) in seen or not _locations_compatible(ra, rb):
                    continue
                seen.add((id(ra), id(rb)))
                pairs.append((ra, rb))
                used_a.add(id(ra))
                used_b.add(id(rb))

    return pairs, used_a, used_b


def _comparable(ra: Dict[str, Any], rb: Dict[str, Any]) -> bool:
    """
    Location gate for a candidate pair. Incompatible places never pair. Two
    DRAWING items additionally need a positive shared anchor (tag, place,
    section, grid or the same sheet number) - without one, nothing says they
    show the same place, and different places legitimately carry different
    values.
    """
    if not _locations_compatible(ra, rb):
        return False
    if _is_drawing_item(ra) and _is_drawing_item(rb):
        return _shared_anchor(ra, rb)
    return True


def align_requirements(
    reqs_a: List[Dict[str, Any]],
    reqs_b: List[Dict[str, Any]],
) -> Tuple[List[Tuple[Dict, Dict]], List[Dict], List[Dict]]:
    """
    Pair requirements across the two sets by keyword overlap, gated by location.

    Returns
    -------
    (pairs, unmatched_a, unmatched_b)
    """
    tokens_a = [_tokens(r.get("topic"), r.get("statement")) for r in reqs_a]
    tokens_b = [_tokens(r.get("topic"), r.get("statement")) for r in reqs_b]
    ids_b = [_norm_id(r.get("element_id")) for r in reqs_b]

    pairs: List[Tuple[Dict, Dict]] = []
    matched_a = set()
    matched_b = set()

    for i, ta in enumerate(tokens_a):
        if not ta:
            continue
        id_a = _norm_id(reqs_a[i].get("element_id"))
        best_j, best_score = -1, 0
        for j, tb in enumerate(tokens_b):
            if not tb:
                continue
            score = _overlap(ta, tb)
            same_id = bool(id_a) and id_a == ids_b[j]
            if same_id:
                score += 3
            if score <= best_score or (score < MIN_TOKEN_OVERLAP and not same_id):
                continue
            if not _comparable(reqs_a[i], reqs_b[j]):
                continue
            best_score, best_j = score, j
        if best_j >= 0 and best_score >= MIN_TOKEN_OVERLAP:
            pairs.append((reqs_a[i], reqs_b[best_j]))
            matched_a.add(i)
            matched_b.add(best_j)

    unmatched_a = [r for i, r in enumerate(reqs_a) if i not in matched_a]
    unmatched_b = [r for j, r in enumerate(reqs_b) if j not in matched_b]

    return pairs, unmatched_a, unmatched_b


# ---------------------------------------------------------------------------
# 2. Pair review - LLM
# ---------------------------------------------------------------------------

_PAIR_CLASSES = [
    "doc_value_conflict", "cross_discipline_mismatch", "level_conflict", "design_change",
    "scope_gap_boq", "spec_ambiguity", "technical_compliance", "site_condition",
    "submittal_conflict", "typical_unit_inconsistency",
]

_PAIR_SYSTEM = """\
You are a senior project engineer reviewing two document sets issued at
DIFFERENT STAGES or by DIFFERENT DISCIPLINES of the same project. You are given
pairs of statements that appear to cover the same element - one from each set.

For each pair, decide whether the two statements are genuinely INCONSISTENT in
a way that a contractor could not resolve on their own: a different value,
type, size, rating, level, configuration or scope for the SAME element at the
SAME place.

Report a discrepancy ONLY when it is real. Do not report a pair because of
wording, formatting, rounding within tolerance, or because one side is simply
more detailed. If the two statements may concern different places or
different elements, it is not a discrepancy. Silence is the correct answer
for a consistent pair.
""" + _SEVERITY_GUIDE + """
Return a JSON object:

{
  "discrepancies": [
    {
      "pair_index": <integer index of the pair you are reporting on>,""" + _FINDING_FIELDS + """
    }
  ]
}

Return ONLY valid JSON. Return {"discrepancies": []} if no pair conflicts.
"""


def _describe_extra(prefix: str, req: Dict[str, Any]) -> List[str]:
    """Context that helps the model judge sameness: file, view, location, tag."""
    label = f"{prefix} " if prefix else ""
    lines = []
    parts = [p for p in (req.get("category"), req.get("view")) if p]
    if parts:
        lines.append(f"{label}category/view: {' / '.join(parts)}")
    loc = _location_label(req)
    if loc:
        lines.append(f"{label}location   : {loc}")
    if req.get("element_id"):
        lines.append(f"{label}element id : {req['element_id']}")
    if req.get("source_file"):
        lines.append(f"{label}file       : {req['source_file']}")
    if req.get("low_confidence"):
        lines.append(f"{label}note       : read by OCR / partly legible - treat values with caution")
    return lines


def _attach_pair_evidence(disc: Dict[str, Any], ra: Dict[str, Any], rb: Dict[str, Any], label_a: str, label_b: str) -> None:
    # References come from our own records - the model is not trusted to
    # reproduce clause numbers accurately.
    disc["document_a_says"] = disc.get("document_a_says") or _says(ra)
    disc["document_b_says"] = disc.get("document_b_says") or _says(rb)
    disc["document_a_reference"] = ra.get("reference") or ""
    disc["document_b_reference"] = rb.get("reference") or ""
    disc["evidence"] = [_evidence(ra, label_a), _evidence(rb, label_b)]
    disc.setdefault("discipline", ra.get("discipline") or rb.get("discipline") or "general")
    if not disc.get("category"):
        disc["category"] = ra.get("category") or rb.get("category") or ""
    if not disc.get("location"):
        disc["location"] = _location_label(ra) or _location_label(rb)
    disc.setdefault("element", ra.get("element_id") or rb.get("element_id") or "")


def _review_pairs(
    pairs: List[Tuple[Dict, Dict]],
    label_a: str,
    label_b: str,
    review_focus: str,
    drawing_mode: bool = False,
) -> List[Dict[str, Any]]:
    """Send candidate pairs to the LLM in batches; return raw discrepancy dicts."""
    found: List[Dict[str, Any]] = []
    if not pairs:
        return found

    system = _PAIR_SYSTEM + _issue_rules(_PAIR_CLASSES) + (_DRAWING_RULES if drawing_mode else "")
    total_batches = (len(pairs) + PAIR_BATCH_SIZE - 1) // PAIR_BATCH_SIZE

    for batch_no in range(total_batches):
        batch = pairs[batch_no * PAIR_BATCH_SIZE: (batch_no + 1) * PAIR_BATCH_SIZE]

        lines = [f"SET A = {label_a}", f"SET B = {label_b}"]
        if review_focus:
            lines.append(f"REVIEW FOCUS REQUESTED BY THE ENGINEER: {review_focus}")
        lines.append("")

        for local_idx, (ra, rb) in enumerate(batch):
            global_idx = batch_no * PAIR_BATCH_SIZE + local_idx
            lines.append(f"--- PAIR {global_idx} ---")
            for prefix, req in (("A", ra), ("B", rb)):
                lines.append(f"{prefix} topic      : {req.get('topic') or '-'}")
                lines.append(f"{prefix} statement  : {req.get('statement') or '-'}")
                lines.append(f"{prefix} value      : {req.get('value') if req.get('value') is not None else '-'}")
                lines.append(f"{prefix} reference  : {req.get('reference') or '-'}")
                lines.extend(_describe_extra(prefix, req))
            lines.append("")

        print(f"[conflict_scan]   -> reviewing pair batch {batch_no + 1}/{total_batches} ({len(batch)} pair(s))")

        try:
            result = _get_llm().run(system=system, user="\n".join(lines), temperature=0.1)
        except Exception as exc:
            print(f"[conflict_scan] pair batch {batch_no} failed: {exc}")
            continue

        for disc in result.get("discrepancies") or []:
            if not isinstance(disc, dict):
                continue
            idx = disc.get("pair_index")
            if not (isinstance(idx, int) and 0 <= idx < len(pairs)):
                continue      # a finding we cannot tie back to its sources is not citable
            ra, rb = pairs[idx]
            disc["scope"] = "between"
            _attach_pair_evidence(disc, ra, rb, label_a, label_b)
            if ra.get("value") in (None, "") or rb.get("value") in (None, ""):
                disc["interpretive"] = True     # no value on one side - not a demonstrated value conflict
            found.append(disc)

    return found


# ---------------------------------------------------------------------------
# 3. Internal consistency - within one set, across files / sheets / views
# ---------------------------------------------------------------------------

_INTERNAL_CLASSES = ["internal_inconsistency", "cross_discipline_mismatch", "level_conflict", "design_change",
                     "typical_unit_inconsistency", "submittal_conflict"]

_INTERNAL_SYSTEM = """\
You are a senior project engineer checking ONE document set for internal
consistency. You are given pairs of statements about the SAME element taken
from DIFFERENT files, sheets or views of that set (plan vs section, one
diagram vs another, schedule vs drawing, BOQ vs specification).

Report a pair ONLY when the two statements genuinely disagree about the same
element at the same place - e.g. one load shown fed from two different
sources, an equipment rating that differs between two diagrams, a member with
two different sizes or levels, device counts on plans that differ from the
riser diagram. A value repeated consistently, or two different places, is not
a finding.
""" + _SEVERITY_GUIDE + """
Return a JSON object:

{
  "discrepancies": [
    {
      "pair_index": <integer index of the pair you are reporting on>,""" + _FINDING_FIELDS + """
    }
  ]
}

Return ONLY valid JSON. Return {"discrepancies": []} if every pair is consistent.
"""


def _source_key(req: Dict[str, Any]) -> str:
    return f"{req.get('source_file', '')}|{req.get('sheet') or ''}|{req.get('view') or ''}"


def _norm_value(value: Any) -> str:
    return re.sub(r"[\s,]+", "", str(value or "")).lower()


def internal_candidates(reqs: List[Dict[str, Any]]) -> List[Tuple[Dict, Dict]]:
    """
    Same element (tag, or identical topic at a shared place) stated with
    different values in different files/sheets/views of one set.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in reqs:
        if r.get("legibility") == "unreadable" or r.get("value") in (None, ""):
            continue
        attr = " ".join(sorted(_tokens(str(r.get("topic") or "").split(" - ")[-1])))
        eid = _norm_id(r.get("element_id"))
        if eid:
            groups.setdefault(f"id:{eid}|{attr}", []).append(r)
        else:
            topic = re.sub(r"\s+", " ", str(r.get("topic") or "").lower()).strip()
            if topic:
                groups.setdefault(f"topic:{topic}", []).append(r)

    pairs: List[Tuple[Dict, Dict]] = []
    seen = set()
    for key, members in groups.items():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                ra, rb = members[i], members[j]
                if _source_key(ra) == _source_key(rb):
                    continue
                if _norm_value(ra.get("value")) == _norm_value(rb.get("value")):
                    continue
                if not _locations_compatible(ra, rb):
                    continue
                if key.startswith("topic:") and not _shared_anchor(ra, rb):
                    continue      # identical topic but nothing ties them to one place
                pair_key = tuple(sorted((id(ra), id(rb))))
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                pairs.append((ra, rb))
    # Tagged elements first - the strongest evidence of "same element"
    pairs.sort(key=lambda p: 0 if p[0].get("element_id") else 1)
    return pairs[:MAX_INTERNAL_PAIRS]


def _review_internal(pairs: List[Tuple[Dict, Dict]], label: str, slot: str, review_focus: str,
                     drawing_mode: bool) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if not pairs:
        return found
    system = _INTERNAL_SYSTEM + _issue_rules(_INTERNAL_CLASSES) + (_DRAWING_RULES if drawing_mode else "")
    total_batches = (len(pairs) + PAIR_BATCH_SIZE - 1) // PAIR_BATCH_SIZE
    for batch_no in range(total_batches):
        batch = pairs[batch_no * PAIR_BATCH_SIZE: (batch_no + 1) * PAIR_BATCH_SIZE]
        lines = [f"DOCUMENT SET: {label}"]
        if review_focus:
            lines.append(f"REVIEW FOCUS REQUESTED BY THE ENGINEER: {review_focus}")
        lines.append("")
        for local_idx, (r1, r2) in enumerate(batch):
            lines.append(f"--- PAIR {batch_no * PAIR_BATCH_SIZE + local_idx} ---")
            for prefix, req in (("1", r1), ("2", r2)):
                lines.append(f"{prefix} topic      : {req.get('topic') or '-'}")
                lines.append(f"{prefix} statement  : {req.get('statement') or '-'}")
                lines.append(f"{prefix} value      : {req.get('value')}")
                lines.append(f"{prefix} reference  : {req.get('reference') or '-'}")
                lines.extend(_describe_extra(prefix, req))
            lines.append("")
        print(f"[conflict_scan]   -> internal consistency of '{label}' batch {batch_no + 1}/{total_batches}")
        try:
            result = _get_llm().run(system=system, user="\n".join(lines), temperature=0.1)
        except Exception as exc:
            print(f"[conflict_scan] internal batch {batch_no} failed: {exc}")
            continue
        for disc in result.get("discrepancies") or []:
            if not isinstance(disc, dict):
                continue
            idx = disc.get("pair_index")
            if not (isinstance(idx, int) and 0 <= idx < len(pairs)):
                continue
            r1, r2 = pairs[idx]
            disc.setdefault("issue_class", "internal_inconsistency")
            _attach_internal_evidence(disc, [r1, r2], label, slot)
            found.append(disc)
    return found


def _attach_internal_evidence(disc: Dict[str, Any], reqs: List[Dict[str, Any]], label: str, slot: str) -> None:
    disc["scope"] = f"within_{slot}"
    evidence = [_evidence(r, label) for r in reqs]
    joined = " || ".join(f"{e['file']} ({e['reference']}): {e['says']}".strip() for e in evidence)
    not_involved = f"Not involved - inconsistency within '{label}'."
    if slot == "a":
        disc["document_a_says"], disc["document_b_says"] = joined, not_involved
        disc["document_a_reference"] = "; ".join(e["reference"] for e in evidence)
        disc["document_b_reference"] = ""
    else:
        disc["document_a_says"], disc["document_b_says"] = not_involved, joined
        disc["document_a_reference"] = ""
        disc["document_b_reference"] = "; ".join(e["reference"] for e in evidence)
    disc["evidence"] = evidence
    disc.setdefault("discipline", reqs[0].get("discipline") or "general")
    if not disc.get("category"):
        disc["category"] = reqs[0].get("category") or ""
    if not disc.get("location"):
        disc["location"] = next((_location_label(r) for r in reqs if _location_label(r)), "")
    disc.setdefault("element", next((r.get("element_id") for r in reqs if r.get("element_id")), ""))


# ---------------------------------------------------------------------------
# 4. Interfaces - items sharing a place, from different files or disciplines
# ---------------------------------------------------------------------------

_INTERFACE_CLASSES = ["spatial_clash", "level_conflict", "cross_discipline_mismatch", "interface_tie_in",
                      "technical_compliance", "missing_information"]

_INTERFACE_SYSTEM = """\
You are a senior coordination engineer. You are given everything the
documents say about ONE PLACE (a building, area, section, grid zone or tagged
element), collected from different files and disciplines. Work out whether
the elements there fit together physically and functionally.

Check, using only the values given:
- do two elements occupy the same space (foundations overlapping, a pipe or
  cable route passing through a foundation, beam, tank or manhole)?
- do the levels work (finished levels vs adjacent grades, retained height vs
  what the detail allows, pipe levels vs beams/foundations, inverts)?
- is there enough depth / clearance / room for what must fit (pipe + slope in
  a floor build-up, ducts in a shaft, services below beams, equipment plus
  access)?
- does one discipline show an element the other does not support (no
  structure under a wall, no feed for equipment, no opening for a duct)?
- is a connection or interface left undefined?

Raise a finding ONLY when the given values show the problem; quote them. Two
items that are simply different elements at the same place are not a
problem. When the values suggest a problem but do not prove it, use
"type": "ambiguity" and ask the coordination question.
""" + _SEVERITY_GUIDE + """
Return a JSON object:

{
  "discrepancies": [
    {
      "item_indices": [<indices of the items that evidence the finding>],""" + _FINDING_FIELDS + """
    }
  ]
}

Return ONLY valid JSON. Return {"discrepancies": []} when everything fits.
"""


def _anchor_keys(req: Dict[str, Any]) -> List[str]:
    a = _anchors(req)
    keys = []
    if a["element_id"]:
        keys.append(f"id:{a['element_id']}")
    for kind in ("section", "grid", "location"):
        if a[kind]:
            keys.append(f"{kind}:{' '.join(sorted(a[kind]))}")
    return keys


def interface_clusters(reqs: List[Dict[str, Any]], drawing_pairs: bool = False) -> List[Dict[str, Any]]:
    """
    Group items by shared place. A cluster is worth reviewing when it holds
    items from at least two files or two disciplines and at least one item
    is geometric (level, size, route, position...).
    """
    clusters: Dict[str, List[Dict[str, Any]]] = {}
    for r in reqs:
        if r.get("legibility") == "unreadable":
            continue
        for key in _anchor_keys(r):
            clusters.setdefault(key, []).append(r)

    out = []
    for key, members in clusters.items():
        files = {m.get("source_file") for m in members}
        disciplines = {m.get("discipline") for m in members if m.get("discipline") not in (None, "", "general")}
        if len(files) < 2 and len(disciplines) < 2:
            continue
        geometric = [m for m in members if _GEOMETRIC_RE.search(f"{m.get('topic')} {m.get('statement')}")]
        if not geometric:
            continue
        ordered = geometric + [m for m in members if m not in geometric]
        out.append({"key": key, "items": ordered[:MAX_CLUSTER_ITEMS], "files": len(files), "disciplines": len(disciplines)})

    # Clusters spanning more files/disciplines first; drop clusters whose items another already covers
    out.sort(key=lambda c: (-(c["files"] + c["disciplines"]), -len(c["items"])))
    kept, covered = [], set()
    for c in out:
        ids = {id(m) for m in c["items"]}
        if ids <= covered:
            continue
        covered |= ids
        kept.append(c)
    return kept[:MAX_INTERFACE_CLUSTERS]


def sheet_pair_clusters(profile_a: Dict[str, Any], profile_b: Dict[str, Any], limit: int = 4) -> List[Dict[str, Any]]:
    """
    Drawing vs drawing with no shared anchors at all: review the geometric
    content of sheet pairs together, so interfaces between two packages that
    never name a common place (e.g. a fence foundation sheet and a retaining
    wall sheet) still get a coordination read.
    """
    def by_sheet(profile):
        sheets: Dict[str, List[Dict[str, Any]]] = {}
        for r in profile.get("requirements") or []:
            if _is_drawing_item(r) and r.get("legibility") != "unreadable" and \
                    _GEOMETRIC_RE.search(f"{r.get('topic')} {r.get('statement')}"):
                sheets.setdefault(f"{r.get('source_file')}|{r.get('sheet')}", []).append(r)
        return sheets

    sa, sb = by_sheet(profile_a), by_sheet(profile_b)
    out = []
    for ka, items_a in list(sa.items())[:limit]:
        for kb, items_b in list(sb.items())[:limit]:
            half = MAX_CLUSTER_ITEMS // 2
            out.append({"key": f"sheets {ka} + {kb}", "items": items_a[:half] + items_b[:half], "files": 2, "disciplines": 0})
            if len(out) >= limit:
                return out
    return out


def _review_interfaces(clusters: List[Dict[str, Any]], labels: Dict[str, str], review_focus: str,
                       drawing_mode: bool) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    system = _INTERFACE_SYSTEM + _issue_rules(_INTERFACE_CLASSES) + (_DRAWING_RULES if drawing_mode else "")
    for n, cluster in enumerate(clusters, start=1):
        items = cluster["items"]
        lines = [f"PLACE: {cluster['key']}"]
        if review_focus:
            lines.append(f"REVIEW FOCUS REQUESTED BY THE ENGINEER: {review_focus}")
        lines.append("")
        for i, req in enumerate(items):
            lines.append(f"--- ITEM {i} ({labels.get(req.get('side'), req.get('side'))}) ---")
            lines.append(f"topic      : {req.get('topic') or '-'}")
            lines.append(f"statement  : {req.get('statement') or '-'}")
            lines.append(f"value      : {req.get('value') if req.get('value') is not None else '-'}")
            lines.append(f"discipline : {req.get('discipline') or '-'}")
            lines.append(f"reference  : {req.get('reference') or '-'}")
            lines.extend(_describe_extra("", req))
        print(f"[conflict_scan]   -> interface review {n}/{len(clusters)} ({cluster['key']}, {len(items)} item(s))")
        try:
            result = _get_llm().run(system=system, user="\n".join(lines), temperature=0.1)
        except Exception as exc:
            print(f"[conflict_scan] interface review {n} failed: {exc}")
            continue
        for disc in result.get("discrepancies") or []:
            if not isinstance(disc, dict):
                continue
            idxs = [i for i in (disc.get("item_indices") or []) if isinstance(i, int) and 0 <= i < len(items)]
            if not idxs:
                continue
            evidence_reqs = [items[i] for i in idxs]
            sides = {r.get("side") for r in evidence_reqs}
            if len(sides) == 1:
                slot = sides.pop()
                _attach_internal_evidence(disc, evidence_reqs, labels.get(slot, slot), slot)
            else:
                a_reqs = [r for r in evidence_reqs if r.get("side") == "a"]
                b_reqs = [r for r in evidence_reqs if r.get("side") == "b"]
                disc["scope"] = "between"
                disc["document_a_says"] = " || ".join(_says(r) for r in a_reqs)
                disc["document_b_says"] = " || ".join(_says(r) for r in b_reqs)
                disc["document_a_reference"] = "; ".join(r.get("reference") or "" for r in a_reqs)
                disc["document_b_reference"] = "; ".join(r.get("reference") or "" for r in b_reqs)
                disc["evidence"] = [_evidence(r, labels.get(r.get("side"), "")) for r in evidence_reqs]
                disc.setdefault("discipline", evidence_reqs[0].get("discipline") or "general")
                if not disc.get("location"):
                    disc["location"] = next((_location_label(r) for r in evidence_reqs if _location_label(r)), "")
            found.append(disc)
    return found


# ---------------------------------------------------------------------------
# 5. Coverage review - items in one set only, and items left open
# ---------------------------------------------------------------------------

_COVERAGE_CLASSES = ["scope_gap_boq", "missing_information", "interface_tie_in", "design_change",
                     "cross_discipline_mismatch", "spec_ambiguity"]

_COVERAGE_SYSTEM = """\
You are a senior project engineer reviewing two document sets issued at
DIFFERENT STAGES or by DIFFERENT DISCIPLINES of the same project.

You are given statements found in ONE set that have no counterpart in the
other, and statements a document itself leaves OPEN (marked TBD, by others,
hold, refer elsewhere, blank quantity). Most unmatched statements are
harmless - sets at different stages naturally carry different levels of
detail.

Report a finding ONLY where there is a genuine problem:
- an item shown or specified on one side with no BOQ/pricing on the other
  (or priced but never designed) - scope_gap_boq;
- information a builder needs that neither set gives - missing_information;
- a connection to another package, plot or utility left undefined -
  interface_tie_in;
- something changed or dropped between stages that alters cost, programme,
  quality or responsibility - design_change.
""" + _SEVERITY_GUIDE + """
Return a JSON object:

{
  "discrepancies": [
    {
      "item_index": <integer index of the statement you are reporting on>,""" + _FINDING_FIELDS + """
    }
  ]
}

Return ONLY valid JSON. Be conservative - return {"discrepancies": []} when the
statements are simply stage-appropriate detail.
"""


def _review_unmatched(
    items: List[Dict[str, Any]],
    present_in_label: str,
    missing_from_label: str,
    present_in_slot: str,
    review_focus: str,
    drawing_mode: bool = False,
) -> List[Dict[str, Any]]:
    """
    Review requirements that exist in one set only (open items first).

    *present_in_slot* is "a" or "b" and tells the caller which side to quote.
    """
    found: List[Dict[str, Any]] = []
    if not items:
        return found

    system = _COVERAGE_SYSTEM + _issue_rules(_COVERAGE_CLASSES) + (
        _DRAWING_RULES + _DRAWING_COVERAGE_RULE if drawing_mode else "")
    items = sorted(items, key=lambda r: 0 if r.get("open_item") else 1)[:MAX_UNMATCHED_REVIEWED]
    total_batches = (len(items) + UNMATCHED_BATCH_SIZE - 1) // UNMATCHED_BATCH_SIZE

    for batch_no in range(total_batches):
        batch = items[batch_no * UNMATCHED_BATCH_SIZE: (batch_no + 1) * UNMATCHED_BATCH_SIZE]

        lines = [
            f"THESE STATEMENTS APPEAR IN : {present_in_label}",
            f"THEY HAVE NO COUNTERPART IN: {missing_from_label}",
        ]
        if review_focus:
            lines.append(f"REVIEW FOCUS REQUESTED BY THE ENGINEER: {review_focus}")
        lines.append("")

        for local_idx, req in enumerate(batch):
            global_idx = batch_no * UNMATCHED_BATCH_SIZE + local_idx
            lines.append(f"--- ITEM {global_idx} ---")
            lines.append(f"topic     : {req.get('topic') or '-'}")
            lines.append(f"statement : {req.get('statement') or '-'}")
            lines.append(f"value     : {req.get('value') if req.get('value') is not None else '-'}")
            lines.append(f"reference : {req.get('reference') or '-'}")
            if req.get("open_item"):
                lines.append("OPEN ITEM : the document itself leaves this unresolved")
            lines.extend(_describe_extra("", req))
            lines.append("")

        print(f"[conflict_scan]   -> reviewing coverage batch {batch_no + 1}/{total_batches} "
              f"for '{present_in_label}' ({len(batch)} item(s))")

        try:
            result = _get_llm().run(system=system, user="\n".join(lines), temperature=0.1)
        except Exception as exc:
            print(f"[conflict_scan] coverage batch {batch_no} failed: {exc}")
            continue

        for disc in result.get("discrepancies") or []:
            if not isinstance(disc, dict):
                continue
            idx = disc.get("item_index")
            if not (isinstance(idx, int) and 0 <= idx < len(items)):
                continue
            req = items[idx]
            statement = _says(req)
            reference = req.get("reference") or ""

            if present_in_slot == "a":
                disc["document_a_says"] = statement
                disc["document_b_says"] = f"Not addressed in {missing_from_label}."
                disc["document_a_reference"] = reference
                disc["document_b_reference"] = ""
            else:
                disc["document_a_says"] = f"Not addressed in {missing_from_label}."
                disc["document_b_says"] = statement
                disc["document_a_reference"] = ""
                disc["document_b_reference"] = reference

            disc["scope"] = "between"
            disc["evidence"] = [_evidence(req, present_in_label)]
            disc.setdefault("discipline", req.get("discipline") or "general")
            if not disc.get("category"):
                disc["category"] = req.get("category") or ""
            if not disc.get("location"):
                disc["location"] = _location_label(req)
            disc.setdefault("element", req.get("element_id") or "")
            found.append(disc)

    return found


# ---------------------------------------------------------------------------
# 6. Quantity comparison - deterministic, no LLM
# ---------------------------------------------------------------------------

QUANTITY_TOLERANCE = 0.01   # 1% - anything within this is treated as rounding

# Spellings of the same unit - a BOQ's "nr" against a drawing's "no." is a
# representation difference, not a unit-of-measure mismatch.
_UNIT_ALIASES = {
    **dict.fromkeys(("nr", "no", "nos", "ea", "each", "pc", "pcs", "number", "عدد"), "nr"),
    **dict.fromkeys(("m2", "m²", "sqm", "sq.m", "sq m", "م2", "م٢"), "m2"),
    **dict.fromkeys(("m3", "m³", "cum", "cu.m", "cu m", "م3", "م٣"), "m3"),
    **dict.fromkeys(("m", "lm", "rm", "l.m", "lin.m", "م.ط", "م ط", "مط"), "m"),
    **dict.fromkeys(("t", "ton", "tons", "tonne", "tonnes", "طن"), "t"),
}


def _unit(value: Any) -> str:
    key = str(value or "").strip().lower().rstrip(".")
    return _UNIT_ALIASES.get(key, key)


def compare_quantities(
    qtys_a: List[Dict[str, Any]],
    qtys_b: List[Dict[str, Any]],
    label_a: str,
    label_b: str,
) -> List[Dict[str, Any]]:
    """
    Match quantity items across sets by description keywords (and location)
    and flag amount or unit mismatches. Purely arithmetic - the LLM is not
    involved, so these findings are exact and reproducible.
    """
    found: List[Dict[str, Any]] = []
    tokens_b = [(_tokens(q.get("description")), q) for q in qtys_b]

    for qa in qtys_a:
        ta = _tokens(qa.get("description"))
        if not ta:
            continue

        best_q = None
        best_score = 0
        for tb, qb in tokens_b:
            score = _overlap(ta, tb)
            if score > best_score and _locations_compatible(qa, qb):
                best_score = score
                best_q = qb
        if best_q is None or best_score < MIN_TOKEN_OVERLAP:
            continue

        amt_a, amt_b = qa.get("amount"), best_q.get("amount")
        unit_a = str(qa.get("unit") or "").strip().lower()
        unit_b = str(best_q.get("unit") or "").strip().lower()

        problems: List[str] = []
        severity = "minor"

        if unit_a and unit_b and _unit(unit_a) != _unit(unit_b):
            problems.append(f"unit of measure differs ({unit_a} vs {unit_b})")
            severity = "major"

        if isinstance(amt_a, (int, float)) and isinstance(amt_b, (int, float)):
            largest = max(abs(amt_a), abs(amt_b), 1e-9)
            deviation = abs(amt_a - amt_b) / largest
            if deviation > QUANTITY_TOLERANCE:
                pct = deviation * 100
                problems.append(f"quantity differs by {pct:.1f}% ({amt_a} vs {amt_b})")
                severity = "critical" if pct >= 10 else "major"

        if not problems:
            continue

        found.append({
            "topic": f"Quantity - {qa.get('description')}",
            "type": "contradiction",
            "issue_class": "doc_value_conflict",
            "scope": "between",
            "severity": severity,
            "category": "quantities",
            "discipline": "general",
            "location": qa.get("location") or best_q.get("location") or "",
            "description": (
                f"The measured quantity for '{qa.get('description')}' is not consistent "
                f"between the two sets: " + "; ".join(problems) + "."
            ),
            "document_a_says": f"{amt_a} {unit_a}".strip() or "-",
            "document_b_says": f"{amt_b} {unit_b}".strip() or "-",
            "document_a_reference": qa.get("reference") or "",
            "document_b_reference": best_q.get("reference") or "",
            "evidence": [
                {"side": "a", "document": label_a, "file": qa.get("source_file", ""), "reference": qa.get("reference") or "",
                 "says": f"{qa.get('description')}: {amt_a} {unit_a}".strip(), "low_confidence": bool(qa.get("low_confidence"))},
                {"side": "b", "document": label_b, "file": best_q.get("source_file", ""), "reference": best_q.get("reference") or "",
                 "says": f"{best_q.get('description')}: {amt_b} {unit_b}".strip(), "low_confidence": bool(best_q.get("low_confidence"))},
            ],
            "impact": (
                "Pricing, procurement, and progress measurement will diverge until the "
                "governing quantity is confirmed."
            ),
            "suggested_question": (
                f"Please confirm which quantity governs for '{qa.get('description')}': "
                f"the {label_a} figure or the {label_b} figure."
            ),
        })

    return found


# ---------------------------------------------------------------------------
# Normalisation, calibration, merging, ordering
# ---------------------------------------------------------------------------

def _compact_refs(value: Any) -> str:
    parts = [compact_reference(p) for p in str(value or "").split(";")]
    return "; ".join(dict.fromkeys(p for p in parts if p))


def _normalise(disc: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a raw discrepancy dict into the state.Discrepancy shape, with taxonomy defaults."""
    text = f"{disc.get('topic') or ''} {disc.get('description') or ''}"
    issue = normalise_issue_class(disc.get("issue_class")) or best_issue_class(text, "doc_value_conflict")

    severity = str(disc.get("severity") or "").strip().lower()
    if severity not in SEVERITY_ORDER:
        severity = default_severity(issue)

    dtype = str(disc.get("type") or "").strip().lower()
    if dtype not in VALID_TYPES:
        dtype = default_type(issue) or "contradiction"

    evidence = [e for e in (disc.get("evidence") or []) if isinstance(e, dict)]
    low_confidence = bool(disc.get("low_confidence")) or any(e.get("low_confidence") for e in evidence)

    return {
        "id": "",   # assigned after sorting
        "topic": str(disc.get("topic") or "Unspecified").strip(),
        "type": dtype,
        "issue_class": issue,
        "scope": str(disc.get("scope") or "between"),
        "severity": severity,
        "category": normalise_category(disc.get("category")),
        "discipline": str(disc.get("discipline") or "general").strip().lower(),
        "location": str(disc.get("location") or "").strip(),
        "element": str(disc.get("element") or "").strip(),
        "description": str(disc.get("description") or "").strip(),
        "document_a_says": str(disc.get("document_a_says") or "").strip(),
        "document_b_says": str(disc.get("document_b_says") or "").strip(),
        "document_a_reference": _compact_refs(disc.get("document_a_reference")),
        "document_b_reference": _compact_refs(disc.get("document_b_reference")),
        "evidence": evidence,
        "low_confidence": low_confidence,
        "interpretive": bool(disc.get("interpretive")),
        "impact": str(disc.get("impact") or "").strip(),
        "suggested_question": str(disc.get("suggested_question") or "").strip(),
    }


def _calibrate(disc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Severity sanity rules learned from how engineers grade RFIs:
    * evidence read by OCR or partly legible never makes a finding critical -
      it goes to the engineer as major at most, flagged for verification;
    * an ambiguity is a question, not a defect - never critical;
    * specification wording questions are rarely more than major.
    """
    if disc["low_confidence"] and disc["severity"] == "critical":
        disc["severity"] = "major"
    if disc["type"] == "ambiguity" and disc["severity"] == "critical":
        disc["severity"] = "major"
    if disc["issue_class"] == "spec_ambiguity" and disc["severity"] == "critical":
        disc["severity"] = "major"
    # A finding quoting one side only (an omission / open question) is a
    # request for information, not proof of a defect - only a physical clash
    # evidenced on one side may stay critical.
    sides = {e.get("side") for e in disc["evidence"] if e.get("side")}
    one_sided = len(disc["evidence"]) <= 1 and len(sides) <= 1
    if (one_sided or disc.pop("interpretive", False)) and disc["severity"] == "critical" \
            and disc["issue_class"] != "spatial_clash":
        disc["severity"] = "major"
    disc.pop("interpretive", None)
    return disc


def _norm_key(disc: Dict[str, Any]) -> str:
    topic = re.sub(r"\s+", " ", disc["topic"].lower())
    desc = re.sub(r"\s+", " ", disc["description"].lower())[:160]
    return f"{topic}|{desc}"


def _coded_tokens(text: str) -> set:
    """Tokens carrying a number - tags, sizes, ratings, item codes ('tr4', 'c30', '1250')."""
    return {t for t in re.findall(r"[a-z0-9][a-z0-9/\.\-]*", str(text or "").lower()) if any(c.isdigit() for c in t)}


def _same_finding(d1: Dict[str, Any], d2: Dict[str, Any]) -> bool:
    if d1["issue_class"] != d2["issue_class"]:
        return False
    if d1["category"] == "quantities" or d2["category"] == "quantities":
        return d1["topic"].lower() == d2["topic"].lower()   # templated descriptions - only the item tells them apart
    c1, c2 = _coded_tokens(d1["topic"]), _coded_tokens(d2["topic"])
    if c1 and c2 and not (c1 & c2):
        return False                                          # different tags / sizes / items
    if d1["element"] and d2["element"] and _norm_id(d1["element"]) != _norm_id(d2["element"]):
        return False
    l1, l2 = _loc_tokens(d1["location"]), _loc_tokens(d2["location"])
    if l1 and l2 and not (l1 & l2):
        return False
    t1 = _tokens(d1["topic"], d1["description"])
    t2 = _tokens(d2["topic"], d2["description"])
    return _jaccard(t1, t2) >= MERGE_SIMILARITY


def _merge_into(keep: Dict[str, Any], other: Dict[str, Any]) -> None:
    if SEVERITY_ORDER[other["severity"]] < SEVERITY_ORDER[keep["severity"]]:
        keep["severity"] = other["severity"]
    seen = {(e.get("file"), e.get("reference"), e.get("says")) for e in keep["evidence"]}
    for e in other["evidence"]:
        k = (e.get("file"), e.get("reference"), e.get("says"))
        if k not in seen:
            keep["evidence"].append(e)
            seen.add(k)
    for side in ("a", "b"):
        ref_key = f"document_{side}_reference"
        extra = other[ref_key]
        if extra and extra not in keep[ref_key]:
            keep[ref_key] = "; ".join(p for p in (keep[ref_key], extra) if p)
    keep["low_confidence"] = keep["low_confidence"] and other["low_confidence"]


def _dedupe_and_rank(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalise, calibrate, merge near-duplicates, sort by severity, assign D-nnn ids."""
    exact: Dict[str, Dict[str, Any]] = {}
    for disc in raw:
        norm = _calibrate(_normalise(disc))
        if not norm["description"] and not norm["topic"]:
            continue
        key = _norm_key(norm)
        if key in exact:
            _merge_into(exact[key], norm)
        else:
            exact[key] = norm

    merged: List[Dict[str, Any]] = []
    for disc in sorted(exact.values(), key=lambda d: SEVERITY_ORDER[d["severity"]]):
        target = next((m for m in merged if _same_finding(m, disc)), None)
        if target:
            _merge_into(target, disc)
        else:
            merged.append(disc)

    ordered = sorted(
        merged,
        key=lambda d: (SEVERITY_ORDER[d["severity"]], category_rank(d["category"]), d["topic"].lower()),
    )
    for i, disc in enumerate(ordered, start=1):
        disc["id"] = f"D-{i:03d}"
    return ordered


def _split_representation_differences(found: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rep = [d for d in found if str(d.get("type", "")).strip().lower() == "representation_difference"]
    rep_ids = {id(d) for d in rep}
    return [d for d in found if id(d) not in rep_ids], rep


def _apply_focus(found: List[Dict[str, Any]], review_focus: str, drawing_mode: bool) -> Tuple[List[Dict[str, Any]], int]:
    """Review focus as a filter: out-of-focus findings survive only when critical."""
    if not (review_focus and drawing_mode):
        return found, 0
    kept = [
        d for d in found
        if not _is_false(d.get("in_focus")) or str(d.get("severity", "")).strip().lower() == "critical"
    ]
    return kept, len(found) - len(kept)


def _build_summary(
    discrepancies: List[Dict[str, Any]],
    profile_a: Dict[str, Any],
    profile_b: Dict[str, Any],
) -> str:
    """
    Build the scan summary deterministically from the register itself.

    This is a factual tally of what the scan found - not a model-written
    narrative of the project scope.
    """
    label_a = profile_a.get("label", "Document A")
    label_b = profile_b.get("label", "Document B")
    files_a = len(profile_a.get("files") or []) or 1
    files_b = len(profile_b.get("files") or []) or 1

    if not discrepancies:
        return (
            f"No discrepancies found between '{label_a}' ({files_a} file(s), "
            f"{len(profile_a.get('requirements', []))} requirements) and '{label_b}' ({files_b} file(s), "
            f"{len(profile_b.get('requirements', []))} requirements)."
        )

    counts = {"critical": 0, "major": 0, "minor": 0}
    by_class: Dict[str, int] = {}
    within = 0
    for d in discrepancies:
        counts[d["severity"]] = counts.get(d["severity"], 0) + 1
        by_class[d["issue_class"]] = by_class.get(d["issue_class"], 0) + 1
        within += d.get("scope", "between") != "between"

    class_str = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(by_class.items(), key=lambda kv: -kv[1]))
    within_str = f" {within} of them are inconsistencies within one set." if within else ""
    low = sum(1 for d in discrepancies if d.get("low_confidence"))
    low_str = f" {low} rest on OCR-read or partly legible values and need verification." if low else ""
    return (
        f"{len(discrepancies)} discrepancies between '{label_a}' ({files_a} file(s)) and '{label_b}' "
        f"({files_b} file(s)): {counts['critical']} critical, {counts['major']} major, {counts['minor']} minor. "
        f"By issue: {class_str}.{within_str}{low_str}"
    )


def _confidence(profile_a: Dict[str, Any], profile_b: Dict[str, Any]) -> str:
    """Confidence in the scan, based on how much was extractable and how much rests on OCR."""
    reqs_a = profile_a.get("requirements", [])
    reqs_b = profile_b.get("requirements", [])
    if not reqs_a or not reqs_b or len(reqs_a) < 5 or len(reqs_b) < 5:
        return "low"
    all_reqs = reqs_a + reqs_b
    low_share = sum(1 for r in all_reqs if r.get("low_confidence")) / len(all_reqs)
    if len(reqs_a) < 20 or len(reqs_b) < 20 or low_share > 0.5:
        return "medium"
    return "high"


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

def conflict_scan_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph node: conflict_scan

    Consumes
    --------
    profile_a, profile_b, document_a_label, document_b_label, review_focus

    Produces
    --------
    discrepancies      : ranked, merged register (with issue_class, scope, evidence)
    scan_summary       : factual tally of the findings
    confidence_level   : high | medium | low
    scan_complete      : True
    step_history       : one entry
    """
    print("\n[conflict_scan] === Comparing the two document sets ===")

    profile_a = state.get("profile_a") or {}
    profile_b = state.get("profile_b") or {}
    label_a = profile_a.get("label") or state.get("document_a_label") or "Document A"
    label_b = profile_b.get("label") or state.get("document_b_label") or "Document B"
    labels = {"a": label_a, "b": label_b}
    review_focus = (state.get("review_focus") or "").strip()

    reqs_a = profile_a.get("requirements") or []
    reqs_b = profile_b.get("requirements") or []

    if not reqs_a and not reqs_b:
        print("[conflict_scan] Nothing extracted from either set - nothing to compare.")
        return {
            "discrepancies": [],
            "scan_summary": "Neither document set yielded any extractable content, so no comparison was possible.",
            "confidence_level": "low",
            "scan_complete": True,
            "analysis_warnings": ["Conflict scan skipped: no content extracted from either document set."],
            "step_history": ["conflict_scan: skipped (no content)"],
        }

    drawing_mode = bool(profile_a.get("drawing_inputs") or profile_b.get("drawing_inputs"))
    warnings: List[str] = []

    # Unreadable drawing values were flagged for engineer review during
    # extraction - comparing a null value would only invent a finding.
    readable_a = [r for r in reqs_a if r.get("legibility") != "unreadable"]
    readable_b = [r for r in reqs_b if r.get("legibility") != "unreadable"]

    # 1. Alignment - baseline links first, location-gated keyword overlap for the rest
    linked, used_a, used_b = _baseline_linked_pairs(profile_a, profile_b)
    keyword_pairs, unmatched_a, unmatched_b = align_requirements(
        [r for r in readable_a if id(r) not in used_a],
        [r for r in readable_b if id(r) not in used_b],
    )
    pairs = linked + keyword_pairs
    print(
        f"[conflict_scan] Aligned {len(pairs)} candidate pair(s) ({len(linked)} via baseline links); "
        f"{len(unmatched_a)} unmatched in '{label_a}', {len(unmatched_b)} in '{label_b}'"
    )

    raw: List[Dict[str, Any]] = []

    # 2. Pair review
    raw.extend(_review_pairs(pairs, label_a, label_b, review_focus, drawing_mode))

    # 3. Internal consistency of each set
    internal_counts = {}
    for slot, reqs in (("a", readable_a), ("b", readable_b)):
        candidates = internal_candidates(reqs)
        internal_counts[slot] = len(candidates)
        if candidates:
            print(f"[conflict_scan] {len(candidates)} internal-consistency candidate pair(s) in '{labels[slot]}'")
            raw.extend(_review_internal(candidates, labels[slot], slot, review_focus, drawing_mode))

    # 4. Interfaces / clashes - places shared across files or disciplines
    clusters = interface_clusters(readable_a + readable_b)
    if not clusters and profile_a.get("drawing_inputs") and profile_b.get("drawing_inputs"):
        clusters = sheet_pair_clusters(profile_a, profile_b)
        if clusters:
            warnings.append(
                "The two drawing sets name no common place, element tag, section or grid, so values were not "
                "compared item-by-item (different sheets legitimately carry different values). Their geometric "
                "content was reviewed sheet-pair by sheet-pair for interface clashes instead - verify the "
                "relative position of the two packages before relying on any clash raised."
            )
    if clusters:
        print(f"[conflict_scan] {len(clusters)} interface cluster(s) to review")
        raw.extend(_review_interfaces(clusters, labels, review_focus, drawing_mode))

    # 5. Coverage review, both directions (open items always included)
    # A set can only be expected to carry what another set says when it is of
    # comparable scope: a cover letter or a short instruction is not "missing"
    # the contents of a drawing package. Items of the much larger set are
    # therefore not reviewed as omissions from the much smaller one (open items
    # the larger set itself leaves unresolved are still reviewed).
    unmatched_ids = {id(r) for r in unmatched_a + unmatched_b}
    open_a = [r for r in readable_a if r.get("open_item") is True and id(r) not in unmatched_ids]
    open_b = [r for r in readable_b if r.get("open_item") is True and id(r) not in unmatched_ids]
    for slot, other, unmatched, open_items in (("a", "b", unmatched_a, open_a), ("b", "a", unmatched_b, open_b)):
        own, theirs = len(readable_a if slot == "a" else readable_b), len(readable_b if slot == "a" else readable_a)
        if theirs < COVERAGE_SCOPE_RATIO * own and unmatched:
            warnings.append(
                f"'{labels[other]}' is much smaller than '{labels[slot]}' ({theirs} vs {own} items), so items of "
                f"'{labels[slot]}' were not reviewed as omissions from it - a short document is not expected to "
                f"repeat a larger set's content."
            )
            unmatched = [r for r in unmatched if r.get("open_item") is True]
        raw.extend(_review_unmatched(open_items + unmatched, labels[slot], labels[other], slot, review_focus, drawing_mode))

    # 6. Quantities
    qty_findings = compare_quantities(
        profile_a.get("quantities") or [],
        profile_b.get("quantities") or [],
        label_a,
        label_b,
    )
    if review_focus and drawing_mode:
        focus_tokens = _tokens(review_focus)
        for finding in qty_findings:
            finding["in_focus"] = bool(_tokens(finding["topic"]) & focus_tokens)
    print(f"[conflict_scan] {len(qty_findings)} quantity mismatch(es) found arithmetically")
    raw.extend(qty_findings)

    raw, representation = _split_representation_differences(raw)
    if representation:
        topics = "; ".join(str(d.get("topic") or "-") for d in representation[:8])
        more = f"; and {len(representation) - 8} more" if len(representation) > 8 else ""
        warnings.append(
            f"{len(representation)} pair(s) were judged to be the same requirement expressed differently "
            f"(e.g. plan vs section vs schedule notation) and were not raised: {topics}{more}."
        )

    raw, dropped = _apply_focus(raw, review_focus, drawing_mode)
    if dropped:
        warnings.append(
            f"{dropped} finding(s) outside the review focus '{review_focus}' were below critical severity "
            f"and were not raised. Critical findings outside the focus are kept."
        )

    raw_count = len(raw)
    discrepancies = _dedupe_and_rank(raw)
    if raw_count > len(discrepancies):
        print(f"[conflict_scan] {raw_count} raw finding(s) merged into {len(discrepancies)}")
    summary = _build_summary(discrepancies, profile_a, profile_b)
    confidence = _confidence(profile_a, profile_b)

    print(f"[conflict_scan] {summary}")
    print("[conflict_scan] === Scan complete ===\n")

    return {
        "discrepancies": discrepancies,
        "scan_summary": summary,
        "confidence_level": confidence,
        "scan_complete": True,
        "analysis_warnings": warnings,
        "step_history": [
            f"conflict_scan: {len(pairs)} pairs reviewed ({len(linked)} baseline-linked), "
            f"{internal_counts.get('a', 0) + internal_counts.get('b', 0)} internal pairs, "
            f"{len(clusters)} interface clusters, {len(discrepancies)} discrepancies raised"
        ],
    }
