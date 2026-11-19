"""
RFI Drafting Node - ConflictScan / RFI Automation
==================================================
Turns the discrepancy register raised by nodes/conflict_scan.py into draft
RFIs (Requests For Information) that an engineer can review, edit, and send
to the client for clarification.

Three stages
------------
1. GROUP  - related findings (same discipline, same area/system) are
            consolidated into ONE RFI with numbered points, the way engineers
            write them: real RFIs routinely carry several points about one
            package, system or area (up to eleven in the learning corpus).
            "single" mode keeps one RFI per finding.

2. DRAFT  - the LLM writes the prose of each RFI in the house style from
            knowledge/rfi_taxonomy.json: start from the documents, one
            numbered point per issue naming every source with its reference
            and value, close with the request type (which governs / provide /
            confirm scope / resolve clash ...), note variation-order exposure
            neutrally. It ASKS, never decides.

3. FILL   - each RFI is written into the project's own .docx RFI form by token
            substitution (see nodes/docx_template.py).

If no template is present, drafting still runs and the RFI text is returned in
the state for on-screen review and editing - only the .docx output is skipped.
"""

import os
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, Any, List

from nodes.run_paths import output_dir, template_dir
from nodes.rfi_knowledge import closing_for, load_taxonomy, request_type_for, rfi_style_prompt

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
MAX_POINTS_PER_RFI = 8          # beyond this an RFI becomes hard to answer point by point
MAX_POINTS_PER_CALL = 8         # findings drafted per LLM call
DEFAULT_RESPONSE_DAYS = 7       # working-day allowance quoted on the form
MAX_ATTACHMENTS_LISTED = 12

# Where a project RFI form is looked up when the caller does not name one
DEFAULT_TEMPLATE_DIR = template_dir()
DEFAULT_TEMPLATE_NAME = "F-PQP-01-03 Request For Information.docx"

PRIORITY_BY_SEVERITY = {
    "critical": "high",
    "major": "medium",
    "minor": "low",
}
SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}

_DISCIPLINE_FAMILY = {
    "structural": "civil", "civil": "civil", "geotechnical": "civil",
    "architectural": "architectural", "landscape": "architectural", "arch": "architectural",
    "mechanical": "mechanical", "plumbing": "mechanical", "hvac": "mechanical", "fire": "mechanical",
    "electrical": "electrical", "low current": "electrical",
}

_STOP = frozenset("the of and for with between from in on at to a an is are not vs".split())


def _family(discipline: str) -> str:
    return _DISCIPLINE_FAMILY.get(str(discipline or "").strip().lower(), "general")


def _words(*parts: Any) -> set:
    text = " ".join(str(p or "") for p in parts).lower()
    return {w for w in re.findall(r"[a-z0-9]{3,}", text) if w not in _STOP}


def _place(disc: Dict[str, Any]) -> set:
    from nodes.conflict_scan import _loc_tokens
    return _loc_tokens(disc.get("location"))


# ---------------------------------------------------------------------------
# Stage 1 - group findings into RFIs
# ---------------------------------------------------------------------------

def group_discrepancies(discrepancies: List[Dict[str, Any]], mode: str = "consolidated") -> List[List[Dict[str, Any]]]:
    """
    Consolidate findings into RFI groups.

    A finding joins an existing group when it has the same discipline family,
    is not at a demonstrably different place, and shares the place, the
    element, or enough subject words with the group. Groups never exceed
    MAX_POINTS_PER_RFI points. Order within the register (severity first) is
    preserved, so each RFI's first point is its most severe.
    """
    if mode == "single":
        return [[d] for d in discrepancies]

    groups: List[Dict[str, Any]] = []
    for disc in discrepancies:
        fam = _family(disc.get("discipline"))
        place = _place(disc)
        words = _words(disc.get("topic"), disc.get("element"))
        element = str(disc.get("element") or "").strip().lower()
        target = None
        for g in groups:
            if g["family"] != fam or len(g["items"]) >= MAX_POINTS_PER_RFI:
                continue
            if place and g["places"] and not (place & g["places"]):
                continue
            same_place = bool(place and (place & g["places"]))
            same_element = bool(element and element in g["elements"])
            shared_words = len(words & g["words"]) >= 2
            if same_place or same_element or shared_words:
                target = g
                break
        if target is None:
            target = {"family": fam, "items": [], "places": set(), "words": set(), "elements": set()}
            groups.append(target)
        target["items"].append(disc)
        target["places"] |= place
        target["words"] |= words
        if element:
            target["elements"].add(element)
    return [g["items"] for g in groups]


# ---------------------------------------------------------------------------
# Stage 2 - draft the RFI prose
# ---------------------------------------------------------------------------

_DRAFT_SYSTEM = """\
You are a contracts / coordination engineer drafting Requests For Information
(RFIs) from the contractor to the consultant.

You are given groups of related findings from a comparison of two document
sets of the same project. Write ONE RFI per group; each finding in a group is
one numbered point of that RFI.

House style (learned from real project RFIs):
{style}

Examples of the length and tone wanted for a point summary:
{examples}

Rules:
- BE BRIEF. A point "summary" is ONE sentence of at most 25 words naming the
  element, where it is, and the problem. Do NOT copy drawing numbers, file
  names, sheet zones or quotes into it - the sources are listed under the
  point automatically, word for word.
- A point "question" is ONE direct question of at most 20 words.
- "background" is ONE short sentence: "With reference to <documents>, it is noted that:".
- An RFI ASKS. Never state which document governs, never instruct the
  consultant, never assume the answer. If a contract order of precedence is
  supplied you may mention it in the proposed solution as context ("subject
  to your confirmation"), never as a decision.
- Never invent drawing numbers, item numbers or values.
- Plain text only: no HTML tags (no <br>), no markdown, no bullet symbols.
- If cost or programme impact cannot be determined from what you were given,
  write "To be assessed on receipt of clarification".
- Points flagged "verify" rest on OCR-read or partly legible values: start the
  summary with "It appears that".

Return a JSON object:

{{
  "rfis": [
    {{
      "group_id": "<the group id given to you, copied exactly>",
      "subject": "<one line, under 90 characters, in the subject patterns above>",
      "discipline": "<civil|structural|architectural|mechanical|electrical|plumbing|general>",
      "area": "<building / area / system the RFI concerns, else ''>",
      "background": "<one sentence>",
      "points": [
        {{"discrepancy_id": "<id copied exactly>", "summary": "<one sentence, max 25 words>", "question": "<one question, max 20 words>"}}
      ],
      "proposed_solution": "<the contractor's suggested way forward in one or two sentences, or ''>",
      "commercial_note": "<one neutral sentence when a point may add scope beyond the BOQ/tender basis, else ''>",
      "cost_impact": "<the cost consequence, or 'To be assessed on receipt of clarification'>",
      "schedule_impact": "<the programme consequence, or 'To be assessed on receipt of clarification'>"
    }}
  ]
}}

Return ONLY valid JSON. Produce exactly one entry per group, and exactly one
point per finding in that group, in the order given.
"""

MAX_SUMMARY_WORDS = 40      # hard cap on a point summary, whatever the model returns
MAX_QUESTION_WORDS = 30
MAX_SOURCES_PER_POINT = 4
MAX_QUOTE_CHARS_IN_RFI = 180


def clean_text(value: Any, max_words: int = 0) -> str:
    """
    Model prose -> plain form text: HTML line breaks become new lines, any other
    tags and markdown emphasis are dropped, whitespace is tidied, and an
    optional word cap trims run-on output at a sentence boundary when possible.
    """
    text = str(value or "")
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"<[^>]{1,40}>", "", text)
    text = re.sub(r"[*_`#]{1,3}", "", text)
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if max_words:
        words = text.split()
        if len(words) > max_words:
            cut = " ".join(words[:max_words])
            stop = max(cut.rfind(". "), cut.rfind("? "))
            text = cut[:stop + 1] if stop > len(cut) // 2 else cut.rstrip(",;:") + "..."
    return text


def _first_sentence(text: str) -> str:
    text = clean_text(text)
    m = re.match(r"(.+?[.?!])(\s|$)", text)
    return m.group(1) if m else text


def _short_quote(text: str) -> str:
    text = clean_text(text).replace("\n", " ")
    return text if len(text) <= MAX_QUOTE_CHARS_IN_RFI else text[:MAX_QUOTE_CHARS_IN_RFI].rsplit(" ", 1)[0] + "..."


def source_lines(disc: Dict[str, Any], label_a: str, label_b: str) -> List[str]:
    """
    One line per source, built from our own records - never from model prose:
    '- <document> (<file>, <reference>): "<what it says, verbatim>"'.
    """
    lines = []
    evidence = disc.get("evidence") or []
    if not evidence:
        evidence = [
            {"document": label_a, "reference": disc.get("document_a_reference"), "says": disc.get("document_a_says")},
            {"document": label_b, "reference": disc.get("document_b_reference"), "says": disc.get("document_b_says")},
        ]
    for e in evidence[:MAX_SOURCES_PER_POINT]:
        says = str(e.get("says") or "").strip()
        if not says or says.lower().startswith("not addressed in") or says.lower().startswith("not involved"):
            says_part = "not addressed"
        else:
            says_part = f'"{_short_quote(says)}"'
        from nodes.conflict_scan import compact_reference
        where = ", ".join(p for p in (e.get("file"), compact_reference(e.get("reference"))) if p)
        lines.append(f"- {e.get('document') or 'Source'}{f' ({where})' if where else ''}: {says_part}")
    return lines


def _sources_text(disc: Dict[str, Any], label_a: str, label_b: str) -> List[str]:
    return [f"   source : {line[2:]}" for line in source_lines(disc, label_a, label_b)]


def _draft_groups(
    batch: List[tuple],
    label_a: str,
    label_b: str,
    project_name: str,
    precedence: str,
) -> Dict[str, Dict[str, Any]]:
    """Draft one batch of RFI groups; returns {group_id: drafted fields}."""
    lines = [f"PROJECT: {project_name}", f"SET A: {label_a}", f"SET B: {label_b}"]
    if precedence:
        lines.append(f"CONTRACT ORDER OF PRECEDENCE (context only): {precedence}")
    lines.append("")
    for group_id, items in batch:
        lines.append(f"=== GROUP {group_id} ({len(items)} finding(s)) ===")
        for disc in items:
            req_type = request_type_for(disc.get("issue_class", ""))
            lines.append(f"--- FINDING {disc['id']} ---")
            lines.append(f"   topic        : {disc.get('topic') or '-'}")
            lines.append(f"   issue class  : {disc.get('issue_class') or '-'} (request type: {req_type})")
            lines.append(f"   discipline   : {disc.get('discipline') or 'general'}")
            if disc.get("location"):
                lines.append(f"   location     : {disc['location']}")
            if disc.get("scope", "between") != "between":
                lines.append("   scope        : inconsistency WITHIN one document set")
            lines.append(f"   description  : {disc.get('description') or '-'}")
            lines.extend(_sources_text(disc, label_a, label_b))
            lines.append(f"   question hint: {disc.get('suggested_question') or '-'}")
            if disc.get("low_confidence"):
                lines.append("   verify       : values were OCR-read or partly legible")
        lines.append("")

    style = load_taxonomy().get("rfi_style") or {}
    examples = "\n".join(f"- {e}" for e in style.get("example_points") or [])
    try:
        result = _get_llm().run(
            system=_DRAFT_SYSTEM.format(style=rfi_style_prompt(), examples=examples or "-"),
            user="\n".join(lines),
            temperature=0.2,
        )
    except Exception as exc:
        print(f"[rfi_drafting] Draft batch failed: {exc}")
        return {}

    drafted: Dict[str, Dict[str, Any]] = {}
    for item in result.get("rfis") or []:
        if isinstance(item, dict) and item.get("group_id"):
            drafted[str(item["group_id"]).strip()] = item
    return drafted


def _fallback_point(disc: Dict[str, Any], label_a: str, label_b: str) -> Dict[str, str]:
    return {
        "discrepancy_id": disc["id"],
        "summary": _first_sentence(disc.get("description") or disc.get("topic") or ""),
        "question": disc.get("suggested_question") or closing_for(request_type_for(disc.get("issue_class", ""))),
    }


def _fallback_draft(items: List[Dict[str, Any]], label_a: str, label_b: str) -> Dict[str, Any]:
    """
    Build an RFI from the findings alone, without the LLM.

    Used when the drafting call fails or omits a group, so that every finding
    still reaches the engineer as an editable draft.
    """
    first = items[0]
    subject = first.get("topic") or "Clarification required"
    if len(items) > 1:
        subject = f"Clarifications required - {first.get('location') or first.get('topic')}"
    return {
        "subject": subject,
        "discipline": first.get("discipline") or "general",
        "area": first.get("location") or "",
        "background": f"With reference to '{label_a}' and '{label_b}', it is noted that:",
        "points": [_fallback_point(d, label_a, label_b) for d in items],
        "proposed_solution": "",
        "commercial_note": "",
        "cost_impact": "To be assessed on receipt of clarification",
        "schedule_impact": "To be assessed on receipt of clarification",
    }


def _numbered(points: List[Dict[str, Any]], key: str, separator: str = "\n") -> str:
    texts = [str(p.get(key) or "").strip() for p in points]
    texts = [t for t in texts if t]
    if len(texts) == 1:
        return texts[0]
    return separator.join(f"{i}- {t}" for i, t in enumerate(texts, start=1))


def _attachments(items: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for disc in items:
        for e in disc.get("evidence") or []:
            ref = str(e.get("reference") or "").split("|")[0].strip()
            entry = " - ".join(p for p in (e.get("file") or e.get("document"), ref) if p)
            if entry and entry not in out:
                out.append(entry)
        if not disc.get("evidence"):
            for key in ("document_a_reference", "document_b_reference"):
                ref = disc.get(key)
                if ref and ref not in out:
                    out.append(ref)
    return out[:MAX_ATTACHMENTS_LISTED]


def _assemble(rfi_no: str, items: List[Dict[str, Any]], fields: Dict[str, Any], due_date: str,
              label_a: str, label_b: str) -> Dict[str, Any]:
    by_id = {d["id"]: d for d in items}
    drafted_points = [p for p in (fields.get("points") or []) if isinstance(p, dict)]
    # Keep one point per finding, in register order - fill any the model dropped
    points: List[Dict[str, Any]] = []
    for disc in items:
        match = next((p for p in drafted_points if str(p.get("discrepancy_id", "")).strip() == disc["id"]), None)
        summary = clean_text((match or {}).get("summary") or (match or {}).get("text"), MAX_SUMMARY_WORDS)
        if not summary:
            match = _fallback_point(disc, label_a, label_b)
            summary = clean_text(match["summary"], MAX_SUMMARY_WORDS)
        question = clean_text(match.get("question") or disc.get("suggested_question"), MAX_QUESTION_WORDS)
        sources = source_lines(disc, label_a, label_b)
        points.append({
            "no": len(points) + 1,
            "discrepancy_id": disc["id"],
            "summary": summary,
            "sources": sources,
            "text": "\n".join([summary, *("   " + line for line in sources)]),
            "question": question,
        })

    worst = min(items, key=lambda d: SEVERITY_ORDER.get(d.get("severity"), 2))
    req_types = Counter(request_type_for(d.get("issue_class", "")) for d in items)
    request_type = request_type_for(worst.get("issue_class", "")) if len(req_types) > 1 else next(iter(req_types))
    background = clean_text(fields.get("background"), 40)
    summary = _numbered(points, "text", separator="\n\n")
    question = _numbered(points, "question")
    commercial = clean_text(fields.get("commercial_note"), 50)
    description = "\n\n".join(p for p in (background, summary, commercial) if p)
    disciplines = Counter(str(d.get("discipline") or "general") for d in items)

    return {
        "rfi_no": rfi_no,
        "discrepancy_id": ", ".join(by_id),
        "discrepancy_ids": list(by_id),
        "points": points,
        "subject": clean_text(fields.get("subject") or worst.get("topic"), 18),
        "discipline": str(fields.get("discipline") or disciplines.most_common(1)[0][0]).strip(),
        "area": str(fields.get("area") or worst.get("location") or "").strip(),
        "priority": PRIORITY_BY_SEVERITY.get(worst.get("severity", "minor"), "low"),
        "request_type": request_type,
        "background": background,
        "discrepancy_summary": summary,
        "description": description,
        "document_a_reference": "; ".join(dict.fromkeys(d.get("document_a_reference") for d in items if d.get("document_a_reference"))),
        "document_b_reference": "; ".join(dict.fromkeys(d.get("document_b_reference") for d in items if d.get("document_b_reference"))),
        "question": question,
        "proposed_solution": clean_text(fields.get("proposed_solution"), 60),
        "commercial_note": commercial,
        "cost_impact": clean_text(fields.get("cost_impact"), 40) or "To be assessed on receipt of clarification",
        "schedule_impact": clean_text(fields.get("schedule_impact"), 40) or "To be assessed on receipt of clarification",
        "attachments": _attachments(items),
        "response_required_by": due_date,
        "low_confidence": any(d.get("low_confidence") for d in items),
        "docx_path": None,
    }


# ---------------------------------------------------------------------------
# Stage 3 - fill the project's .docx RFI form
# ---------------------------------------------------------------------------

def resolve_template_path(explicit_path: str = None) -> str:
    """
    Locate the .docx RFI form to fill.

    Order of preference:
    1. *explicit_path*, when the caller named one.
    2. templates_rfi/F-PQP-01-03 Request For Information.docx (the company's
       official RFI form)
    3. the first .docx found in templates_rfi/ (so dropping a different form
       into that folder is enough to start using it)

    Returns "" when no template is available.
    """
    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path

    default = os.path.join(DEFAULT_TEMPLATE_DIR, DEFAULT_TEMPLATE_NAME)
    if os.path.isfile(default):
        return default

    if os.path.isdir(DEFAULT_TEMPLATE_DIR):
        for name in sorted(os.listdir(DEFAULT_TEMPLATE_DIR)):
            if name.lower().endswith(".docx") and not name.startswith("~$"):
                return os.path.join(DEFAULT_TEMPLATE_DIR, name)

    return ""


def build_token_values(
    rfi: Dict[str, Any],
    state: Dict[str, Any],
    label_a: str,
    label_b: str,
) -> Dict[str, str]:
    """
    Map one RFI onto the {{token}} names the .docx form expects.

    Every token the built-in form uses is documented in
    templates_rfi/README.md. Tokens a custom form does not use are simply
    ignored; tokens it uses that are absent here are emptied.
    """
    today = datetime.now()
    due = today + timedelta(days=DEFAULT_RESPONSE_DAYS)
    form_meta = state.get("rfi_form_meta") or {}
    attachments = rfi.get("attachments") or []
    if isinstance(attachments, str):
        attachments = [a.strip() for a in attachments.split("\n") if a.strip()]

    return {
        "rfi_no":               rfi.get("rfi_no", ""),
        "rfi_date":             today.strftime("%d %B %Y"),
        "response_required_by": rfi.get("response_required_by") or due.strftime("%d %B %Y"),
        "project_name":         state.get("project_name") or "",
        "consultant":           form_meta.get("consultant", ""),
        "employer":             form_meta.get("employer", ""),
        "project_code":         form_meta.get("project_code", ""),
        "to":                   form_meta.get("to", ""),
        "location":             form_meta.get("location", "") or rfi.get("area", ""),
        "boq_no":               form_meta.get("boq_no", ""),
        "dwg_no":               form_meta.get("dwg_no", ""),
        "level":                form_meta.get("level", ""),
        "specs_no":             form_meta.get("specs_no", ""),
        "discipline":           rfi.get("discipline", ""),
        "priority":             rfi.get("priority", ""),
        "subject":              rfi.get("subject", ""),
        "area":                 rfi.get("area", ""),
        "document_a_label":     label_a,
        "document_b_label":     label_b,
        "document_a_reference": rfi.get("document_a_reference", ""),
        "document_b_reference": rfi.get("document_b_reference", ""),
        "background":           rfi.get("background", ""),
        "discrepancy_summary":  rfi.get("discrepancy_summary", ""),
        "description":          rfi.get("description", ""),
        "question":             rfi.get("question", ""),
        "proposed_solution":    rfi.get("proposed_solution", ""),
        "commercial_note":      rfi.get("commercial_note", ""),
        "cost_impact":          rfi.get("cost_impact", ""),
        "schedule_impact":      rfi.get("schedule_impact", ""),
        "attachments":          "\n".join(f"{i}. {a}" for i, a in enumerate(attachments, start=1)),
        "attachment_count":     str(len(attachments)) if attachments else "No attachment",
        "originator":           state.get("user_id") or "",
        "discrepancy_id":       rfi.get("discrepancy_id", ""),
        "request_type":         str(rfi.get("request_type", "")).replace("_", " "),
    }


def _safe_name(text: str) -> str:
    """Filesystem-safe fragment for a filename."""
    keep = [c if (c.isalnum() or c in " -_") else "_" for c in str(text)]
    return "".join(keep).strip().replace(" ", "_")[:60] or "RFI"


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

def rfi_drafting_agent(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph node: draft_rfi

    Consumes
    --------
    discrepancies, document_a_label, document_b_label, project_name,
    rfi_grouping ("consolidated" | "single"), document_precedence,
    rfi_template_path (optional)

    Produces
    --------
    rfi_items    : drafted RFIs (each with numbered points), editable on screen
    rfi_paths    : .docx files written into exports/ (empty if no template)
    rfi_drafted  : True
    """
    print("\n[rfi_drafting] === Drafting RFIs ===")

    discrepancies = state.get("discrepancies") or []
    if not discrepancies:
        print("[rfi_drafting] No discrepancies - nothing to draft.")
        return {
            "rfi_items": [],
            "rfi_paths": [],
            "rfi_drafted": True,
            "step_history": ["draft_rfi: skipped (no discrepancies)"],
        }

    label_a = state.get("document_a_label") or "Document A"
    label_b = state.get("document_b_label") or "Document B"
    project_name = state.get("project_name") or "Unnamed Project"
    precedence = (state.get("document_precedence") or "").strip()
    mode = (state.get("rfi_grouping") or "consolidated").strip().lower()

    # --- Stage 1: group ------------------------------------------------------
    groups = group_discrepancies(discrepancies, mode)
    group_ids = [f"G-{i:02d}" for i in range(1, len(groups) + 1)]
    print(f"[rfi_drafting] {len(discrepancies)} finding(s) -> {len(groups)} RFI(s) ({mode})")

    # --- Stage 2: draft prose, batching groups up to MAX_POINTS_PER_CALL ----
    drafted: Dict[str, Dict[str, Any]] = {}
    batch: List[tuple] = []
    batch_points = 0
    batches: List[List[tuple]] = []
    for gid, items in zip(group_ids, groups):
        if batch and batch_points + len(items) > MAX_POINTS_PER_CALL:
            batches.append(batch)
            batch, batch_points = [], 0
        batch.append((gid, items))
        batch_points += len(items)
    if batch:
        batches.append(batch)
    for n, b in enumerate(batches, start=1):
        print(f"[rfi_drafting]   -> drafting batch {n}/{len(batches)} ({len(b)} RFI(s))")
        drafted.update(_draft_groups(b, label_a, label_b, project_name, precedence))

    # --- Assemble the RFI records -----------------------------------------
    due_date = (datetime.now() + timedelta(days=DEFAULT_RESPONSE_DAYS)).strftime("%d %B %Y")
    rfi_items: List[Dict[str, Any]] = []
    for i, (gid, items) in enumerate(zip(group_ids, groups), start=1):
        fields = drafted.get(gid)
        if not fields:
            print(f"[rfi_drafting] {gid}: no LLM draft returned - using fallback text")
            fields = _fallback_draft(items, label_a, label_b)
        rfi_items.append(_assemble(f"RFI-{i:03d}", items, fields, due_date, label_a, label_b))

    # --- Stage 3: fill the .docx form -------------------------------------
    template_path = resolve_template_path(state.get("rfi_template_path"))
    rfi_paths: List[str] = []
    warnings: List[str] = []

    if not template_path:
        msg = (
            "No RFI .docx form found in templates_rfi/ - RFIs were drafted for on-screen "
            "review but no Word documents were generated. Drop the project's RFI form "
            "into templates_rfi/ to enable document output."
        )
        print(f"[rfi_drafting] {msg}")
        warnings.append(msg)
    else:
        print(f"[rfi_drafting] Filling form: {template_path}")
        export_dir = output_dir()
        project_fragment = _safe_name(project_name)

        try:
            from nodes.docx_template import fill_docx_template
        except ImportError as exc:
            msg = f"python-docx is not installed, so no RFI documents were written ({exc})."
            print(f"[rfi_drafting] {msg}")
            warnings.append(msg)
            fill_docx_template = None

        if fill_docx_template is not None:
            for rfi in rfi_items:
                out_name = f"{project_fragment}_{rfi['rfi_no']}_{_safe_name(rfi['subject'])}.docx"
                out_path = os.path.join(export_dir, out_name)
                try:
                    fill_docx_template(
                        template_path,
                        out_path,
                        build_token_values(rfi, state, label_a, label_b),
                    )
                    rfi["docx_path"] = out_path
                    rfi_paths.append(out_path)
                    print(f"[rfi_drafting]   wrote {out_name}")
                except Exception as exc:
                    msg = f"Could not write {rfi['rfi_no']}: {exc}"
                    print(f"[rfi_drafting] {msg}")
                    warnings.append(msg)

    print(f"[rfi_drafting] === {len(rfi_items)} RFI(s) drafted, "
          f"{len(rfi_paths)} document(s) written ===\n")

    return {
        "rfi_items": rfi_items,
        "rfi_paths": rfi_paths,
        "rfi_drafted": True,
        "analysis_warnings": warnings,
        "step_history": [
            f"draft_rfi: {len(discrepancies)} finding(s) -> {len(rfi_items)} RFI(s) ({mode}), "
            f"{len(rfi_paths)} .docx written"
        ],
    }
