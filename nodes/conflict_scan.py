"""
Conflict Scan Node - ConflictScan / RFI Automation
===================================================
Compares the two independent document profiles produced by
nodes/document_analysis.py and emits a structured discrepancy register.

The two documents are treated symmetrically. Neither is assumed to be the
"correct" one - they are simply two stages of the same project, and where they
disagree the client has to say which governs. That is what the RFI is for.

Scan strategy
-------------
1. ALIGNMENT   - requirements from A and B are paired by keyword overlap on
                 topic + statement, producing candidate pairs to examine.
2. PAIR REVIEW - candidate pairs are sent to the LLM in batches; the model
                 decides whether each pair genuinely conflicts and, if so,
                 writes the discrepancy with both sides quoted.
3. COVERAGE    - requirements from either document with no counterpart in the
                 other are batched and reviewed as potential omissions.
4. QUANTITIES  - quantity items matched by description are compared
                 arithmetically; a mismatch in amount or unit is a discrepancy
                 raised deterministically, with no LLM call needed.

Nothing here is specific to any document type.
"""

import re
from typing import Dict, Any, List, Tuple

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
PAIR_BATCH_SIZE = 12        # candidate pairs per LLM call
UNMATCHED_BATCH_SIZE = 20   # unmatched requirements per LLM call
MIN_TOKEN_OVERLAP = 2       # shared keywords needed to call two topics related
MAX_UNMATCHED_REVIEWED = 120  # cap on omission review, keeps runtime bounded

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}

# Words that carry no discriminating meaning when matching topics
_STOPWORDS = frozenset("""
a an the of for to and or in on at by with from as is are be shall must will
this that these those all any each per its it not no than then such other
general works work item items section clause required requirement requirements
provide provided providing including include included
""".split())


def _tokens(*parts: Any) -> set:
    """Lower-cased significant keyword set for a topic/statement."""
    text = " ".join(str(p or "") for p in parts).lower()
    words = re.findall(r"[a-z0-9][a-z0-9\-/\.]{1,}", text)
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _overlap(a: set, b: set) -> int:
    return len(a & b)


# ---------------------------------------------------------------------------
# 1. Alignment
# ---------------------------------------------------------------------------

def align_requirements(
    reqs_a: List[Dict[str, Any]],
    reqs_b: List[Dict[str, Any]],
) -> Tuple[List[Tuple[Dict, Dict]], List[Dict], List[Dict]]:
    """
    Pair requirements across the two documents by keyword overlap.

    Returns
    -------
    (pairs, unmatched_a, unmatched_b)
        pairs        : list of (requirement_from_a, requirement_from_b)
        unmatched_a  : A requirements with no counterpart in B
        unmatched_b  : B requirements with no counterpart in A
    """
    tokens_a = [_tokens(r.get("topic"), r.get("statement")) for r in reqs_a]
    tokens_b = [_tokens(r.get("topic"), r.get("statement")) for r in reqs_b]

    pairs: List[Tuple[Dict, Dict]] = []
    matched_a = set()
    matched_b = set()

    for i, ta in enumerate(tokens_a):
        if not ta:
            continue
        best_j = -1
        best_score = 0
        for j, tb in enumerate(tokens_b):
            if not tb:
                continue
            score = _overlap(ta, tb)
            if score > best_score:
                best_score = score
                best_j = j
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

_PAIR_SYSTEM = """\
You are a senior project engineer reviewing two documents issued at DIFFERENT
STAGES of the same project. You are given pairs of statements that appear to
cover the same subject - one taken from each document.

For each pair, decide whether the two statements are genuinely INCONSISTENT in
a way that a contractor could not resolve on their own.

Report a discrepancy ONLY when it is real. Do not report a pair as conflicting
because of wording, formatting, rounding within tolerance, or because one side
is simply more detailed than the other. Silence is the correct answer for a
consistent pair.

Return a JSON object:

{
  "discrepancies": [
    {
      "pair_index": <integer index of the pair you are reporting on>,
      "topic": "<short subject line>",
      "type": "contradiction|ambiguity|scope_gap",
      "severity": "critical|major|minor",
      "description": "<what the inconsistency is, in one or two sentences>",
      "document_a_says": "<quote or close paraphrase of the A statement>",
      "document_b_says": "<quote or close paraphrase of the B statement>",
      "impact": "<the practical cost, programme, or quality consequence>",
      "suggested_question": "<the single clarification to put to the client>"
    }
  ]
}

Severity guide:
- critical: work cannot proceed, or proceeding risks rework, safety, or a claim
- major:    materially affects cost, programme, or quality
- minor:    should be tidied up but has no material consequence

Return ONLY valid JSON. Return {"discrepancies": []} if no pair conflicts.
"""


def _review_pairs(
    pairs: List[Tuple[Dict, Dict]],
    label_a: str,
    label_b: str,
    review_focus: str,
) -> List[Dict[str, Any]]:
    """Send candidate pairs to the LLM in batches; return raw discrepancy dicts."""
    found: List[Dict[str, Any]] = []
    if not pairs:
        return found

    total_batches = (len(pairs) + PAIR_BATCH_SIZE - 1) // PAIR_BATCH_SIZE

    for batch_no in range(total_batches):
        batch = pairs[batch_no * PAIR_BATCH_SIZE: (batch_no + 1) * PAIR_BATCH_SIZE]

        lines = [
            f"DOCUMENT A = {label_a}",
            f"DOCUMENT B = {label_b}",
        ]
        if review_focus:
            lines.append(f"REVIEW FOCUS REQUESTED BY THE ENGINEER: {review_focus}")
        lines.append("")

        for local_idx, (ra, rb) in enumerate(batch):
            global_idx = batch_no * PAIR_BATCH_SIZE + local_idx
            lines.append(f"--- PAIR {global_idx} ---")
            lines.append(f"A topic     : {ra.get('topic') or '-'}")
            lines.append(f"A statement : {ra.get('statement') or '-'}")
            lines.append(f"A value     : {ra.get('value') if ra.get('value') is not None else '-'}")
            lines.append(f"A reference : {ra.get('reference') or '-'}")
            lines.append(f"B topic     : {rb.get('topic') or '-'}")
            lines.append(f"B statement : {rb.get('statement') or '-'}")
            lines.append(f"B value     : {rb.get('value') if rb.get('value') is not None else '-'}")
            lines.append(f"B reference : {rb.get('reference') or '-'}")
            lines.append("")

        print(f"[conflict_scan]   -> reviewing pair batch {batch_no + 1}/{total_batches} "
              f"({len(batch)} pair(s))")

        try:
            result = _get_llm().run(
                system=_PAIR_SYSTEM,
                user="\n".join(lines),
                temperature=0.1,
            )
        except Exception as exc:
            print(f"[conflict_scan] pair batch {batch_no} failed: {exc}")
            continue

        for disc in result.get("discrepancies") or []:
            if not isinstance(disc, dict):
                continue
            # Attach the source references from our own records - the model is
            # not trusted to reproduce clause numbers accurately.
            idx = disc.get("pair_index")
            if isinstance(idx, int) and 0 <= idx < len(pairs):
                ra, rb = pairs[idx]
                disc["document_a_reference"] = ra.get("reference") or ""
                disc["document_b_reference"] = rb.get("reference") or ""
                disc.setdefault("discipline", ra.get("discipline") or rb.get("discipline") or "general")
            found.append(disc)

    return found


# ---------------------------------------------------------------------------
# 3. Coverage review - requirements present in one document only
# ---------------------------------------------------------------------------

_COVERAGE_SYSTEM = """\
You are a senior project engineer reviewing two documents issued at DIFFERENT
STAGES of the same project.

You are given statements found in ONE document that have no counterpart in the
other. Most such statements are harmless - documents at different stages
naturally carry different levels of detail, and each document has its own
purpose.

Report an omission ONLY where the absence is a genuine problem: a requirement
that the other document had to carry and does not, a scope item that appears on
one side only, or something dropped between stages that changes cost, programme,
quality, or responsibility.

Return a JSON object:

{
  "discrepancies": [
    {
      "item_index": <integer index of the statement you are reporting on>,
      "topic": "<short subject line>",
      "type": "omission|scope_gap",
      "severity": "critical|major|minor",
      "description": "<what is missing from the other document and why it matters>",
      "impact": "<the practical cost, programme, or quality consequence>",
      "suggested_question": "<the single clarification to put to the client>"
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
) -> List[Dict[str, Any]]:
    """
    Review requirements that exist in one document only.

    *present_in_slot* is "a" or "b" and tells the caller which side to quote.
    """
    found: List[Dict[str, Any]] = []
    if not items:
        return found

    items = items[:MAX_UNMATCHED_REVIEWED]
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
            lines.append("")

        print(f"[conflict_scan]   -> reviewing coverage batch {batch_no + 1}/{total_batches} "
              f"for '{present_in_label}' ({len(batch)} item(s))")

        try:
            result = _get_llm().run(
                system=_COVERAGE_SYSTEM,
                user="\n".join(lines),
                temperature=0.1,
            )
        except Exception as exc:
            print(f"[conflict_scan] coverage batch {batch_no} failed: {exc}")
            continue

        for disc in result.get("discrepancies") or []:
            if not isinstance(disc, dict):
                continue
            idx = disc.get("item_index")
            req = items[idx] if isinstance(idx, int) and 0 <= idx < len(items) else {}
            statement = req.get("statement") or disc.get("description") or ""
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

            disc.setdefault("discipline", req.get("discipline") or "general")
            found.append(disc)

    return found


# ---------------------------------------------------------------------------
# 4. Quantity comparison - deterministic, no LLM
# ---------------------------------------------------------------------------

QUANTITY_TOLERANCE = 0.01   # 1% - anything within this is treated as rounding


def compare_quantities(
    qtys_a: List[Dict[str, Any]],
    qtys_b: List[Dict[str, Any]],
    label_a: str,
    label_b: str,
) -> List[Dict[str, Any]]:
    """
    Match quantity items across documents by description keywords and flag
    amount or unit mismatches. Purely arithmetic - the LLM is not involved,
    so these findings are exact and reproducible.
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
            if score > best_score:
                best_score = score
                best_q = qb
        if best_q is None or best_score < MIN_TOKEN_OVERLAP:
            continue

        amt_a, amt_b = qa.get("amount"), best_q.get("amount")
        unit_a = str(qa.get("unit") or "").strip().lower()
        unit_b = str(best_q.get("unit") or "").strip().lower()

        problems: List[str] = []
        severity = "minor"

        if unit_a and unit_b and unit_a != unit_b:
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
            "severity": severity,
            "discipline": "general",
            "description": (
                f"The measured quantity for '{qa.get('description')}' is not consistent "
                f"between the two documents: " + "; ".join(problems) + "."
            ),
            "document_a_says": f"{amt_a} {unit_a}".strip() or "-",
            "document_b_says": f"{amt_b} {unit_b}".strip() or "-",
            "document_a_reference": qa.get("reference") or "",
            "document_b_reference": best_q.get("reference") or "",
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
# Normalisation, de-duplication, ordering
# ---------------------------------------------------------------------------

_VALID_TYPES = {"contradiction", "omission", "ambiguity", "scope_gap"}


def _normalise(disc: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a raw discrepancy dict into the state.Discrepancy shape."""
    severity = str(disc.get("severity") or "minor").strip().lower()
    if severity not in SEVERITY_ORDER:
        severity = "minor"

    dtype = str(disc.get("type") or "contradiction").strip().lower()
    if dtype not in _VALID_TYPES:
        dtype = "contradiction"

    return {
        "id": "",   # assigned after sorting
        "topic": str(disc.get("topic") or "Unspecified").strip(),
        "type": dtype,
        "severity": severity,
        "discipline": str(disc.get("discipline") or "general").strip().lower(),
        "description": str(disc.get("description") or "").strip(),
        "document_a_says": str(disc.get("document_a_says") or "").strip(),
        "document_b_says": str(disc.get("document_b_says") or "").strip(),
        "document_a_reference": str(disc.get("document_a_reference") or "").strip(),
        "document_b_reference": str(disc.get("document_b_reference") or "").strip(),
        "impact": str(disc.get("impact") or "").strip(),
        "suggested_question": str(disc.get("suggested_question") or "").strip(),
    }


def _dedupe_and_rank(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalise, drop near-duplicates, sort by severity, and assign D-nnn ids."""
    seen: Dict[str, Dict[str, Any]] = {}

    for disc in raw:
        norm = _normalise(disc)
        if not norm["description"] and not norm["topic"]:
            continue
        key = _norm_key(norm)
        # Keep the entry with the higher severity when two collide
        if key in seen:
            if SEVERITY_ORDER[norm["severity"]] < SEVERITY_ORDER[seen[key]["severity"]]:
                seen[key] = norm
        else:
            seen[key] = norm

    ordered = sorted(
        seen.values(),
        key=lambda d: (SEVERITY_ORDER[d["severity"]], d["topic"].lower()),
    )
    for i, disc in enumerate(ordered, start=1):
        disc["id"] = f"D-{i:03d}"
    return ordered


def _norm_key(disc: Dict[str, Any]) -> str:
    topic = re.sub(r"\s+", " ", disc["topic"].lower())
    desc = re.sub(r"\s+", " ", disc["description"].lower())[:160]
    return f"{topic}|{desc}"


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

    counts = {"critical": 0, "major": 0, "minor": 0}
    by_type: Dict[str, int] = {}
    for d in discrepancies:
        counts[d["severity"]] = counts.get(d["severity"], 0) + 1
        by_type[d["type"]] = by_type.get(d["type"], 0) + 1

    if not discrepancies:
        return (
            f"No discrepancies found between '{label_a}' "
            f"({len(profile_a.get('requirements', []))} requirements) and '{label_b}' "
            f"({len(profile_b.get('requirements', []))} requirements)."
        )

    type_str = ", ".join(f"{n} {t.replace('_', ' ')}" for t, n in sorted(by_type.items()))
    return (
        f"{len(discrepancies)} discrepancies between '{label_a}' and '{label_b}': "
        f"{counts['critical']} critical, {counts['major']} major, {counts['minor']} minor. "
        f"By type: {type_str}."
    )


def _confidence(profile_a: Dict[str, Any], profile_b: Dict[str, Any]) -> str:
    """Confidence in the scan, based on how much was extractable from each side."""
    reqs_a = len(profile_a.get("requirements", []))
    reqs_b = len(profile_b.get("requirements", []))
    if reqs_a == 0 or reqs_b == 0:
        return "low"
    if reqs_a < 5 or reqs_b < 5:
        return "low"
    if reqs_a < 20 or reqs_b < 20:
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
    discrepancies      : ranked, de-duplicated register
    scan_summary       : factual tally of the findings
    confidence_level   : high | medium | low
    scan_complete      : True
    step_history       : one entry
    """
    print("\n[conflict_scan] === Comparing the two documents ===")

    profile_a = state.get("profile_a") or {}
    profile_b = state.get("profile_b") or {}
    label_a = profile_a.get("label") or state.get("document_a_label") or "Document A"
    label_b = profile_b.get("label") or state.get("document_b_label") or "Document B"
    review_focus = (state.get("review_focus") or "").strip()

    reqs_a = profile_a.get("requirements") or []
    reqs_b = profile_b.get("requirements") or []

    if not reqs_a and not reqs_b:
        print("[conflict_scan] Nothing extracted from either document - nothing to compare.")
        return {
            "discrepancies": [],
            "scan_summary": "Neither document yielded any extractable content, so no comparison was possible.",
            "confidence_level": "low",
            "scan_complete": True,
            "analysis_warnings": ["Conflict scan skipped: no content extracted from either document."],
            "step_history": ["conflict_scan: skipped (no content)"],
        }

    # 1. Alignment
    pairs, unmatched_a, unmatched_b = align_requirements(reqs_a, reqs_b)
    print(
        f"[conflict_scan] Aligned {len(pairs)} candidate pair(s); "
        f"{len(unmatched_a)} unmatched in '{label_a}', {len(unmatched_b)} in '{label_b}'"
    )

    raw: List[Dict[str, Any]] = []

    # 2. Pair review
    raw.extend(_review_pairs(pairs, label_a, label_b, review_focus))

    # 3. Coverage review, both directions
    raw.extend(_review_unmatched(unmatched_a, label_a, label_b, "a", review_focus))
    raw.extend(_review_unmatched(unmatched_b, label_b, label_a, "b", review_focus))

    # 4. Quantities
    qty_findings = compare_quantities(
        profile_a.get("quantities") or [],
        profile_b.get("quantities") or [],
        label_a,
        label_b,
    )
    print(f"[conflict_scan] {len(qty_findings)} quantity mismatch(es) found arithmetically")
    raw.extend(qty_findings)

    # Rank and label
    discrepancies = _dedupe_and_rank(raw)
    summary = _build_summary(discrepancies, profile_a, profile_b)
    confidence = _confidence(profile_a, profile_b)

    print(f"[conflict_scan] {summary}")
    print("[conflict_scan] === Scan complete ===\n")

    return {
        "discrepancies": discrepancies,
        "scan_summary": summary,
        "confidence_level": confidence,
        "scan_complete": True,
        "step_history": [
            f"conflict_scan: {len(pairs)} pairs reviewed, {len(discrepancies)} discrepancies raised"
        ],
    }
