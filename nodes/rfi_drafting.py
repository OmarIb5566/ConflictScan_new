"""
RFI Drafting Node - ConflictScan / RFI Automation
==================================================
Turns each discrepancy raised by nodes/conflict_scan.py into a draft RFI
(Request For Information) that an engineer can review, edit, and send to the
client for clarification.

Two stages
----------
1. DRAFT  - the LLM writes the prose fields of each RFI (subject, background,
            question, proposed solution, impacts) from the discrepancy record.
            It is instructed to ask, never to decide: an RFI puts the question
            to the client, it does not assert which document governs.

2. FILL   - each drafted RFI is written into the project's own .docx RFI form
            by token substitution (see nodes/docx_template.py). The form lives
            in templates_rfi/ and is swapped for the client's official one
            without touching any code.

If no template is present, drafting still runs and the RFI text is returned in
the state for on-screen review and editing - only the .docx output is skipped.
"""

import os
from datetime import datetime, timedelta
from typing import Dict, Any, List

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
DRAFT_BATCH_SIZE = 6            # discrepancies per LLM call
DEFAULT_RESPONSE_DAYS = 7       # working-day allowance quoted on the form

# Where a project RFI form is looked up when the caller does not name one
DEFAULT_TEMPLATE_DIR = os.path.join(os.getcwd(), "templates_rfi")
DEFAULT_TEMPLATE_NAME = "F-PQP-01-03 Request For Information.docx"

PRIORITY_BY_SEVERITY = {
    "critical": "high",
    "major": "medium",
    "minor": "low",
}


# ---------------------------------------------------------------------------
# Stage 1 - draft the RFI prose
# ---------------------------------------------------------------------------

_DRAFT_SYSTEM = """\
You are a contracts engineer drafting Requests For Information (RFIs) to a
client, on behalf of the contractor.

You are given discrepancies found between two documents issued at different
stages of the same project. For each one, write a formal RFI.

House rules:
- An RFI ASKS. Never state which document governs, never instruct the client,
  never assume the answer. Set out both positions neutrally and ask which
  applies.
- Quote both documents by name and by clause/section reference where one was
  captured.
- One clear question per RFI, answerable with a decision. Not a list of
  musings.
- Formal, plain, professional English. No greetings, no sign-off, no filler
  such as "we hope" or "at your earliest convenience". No markdown.
- If cost or programme impact cannot be determined from what you were given,
  write "To be assessed on receipt of clarification" rather than inventing one.

Return a JSON object:

{
  "rfis": [
    {
      "discrepancy_id": "<the id given to you, copied exactly>",
      "subject": "<one line, under 100 characters>",
      "discipline": "<civil|structural|architectural|mechanical|electrical|plumbing|contractual|general>",
      "background": "<2-4 sentences: the documents involved and the context>",
      "discrepancy_summary": "<2-3 sentences stating both positions neutrally>",
      "question": "<the single clarification requested, as a direct question>",
      "proposed_solution": "<the contractor's suggested way forward, offered for approval>",
      "cost_impact": "<the cost consequence, or 'To be assessed on receipt of clarification'>",
      "schedule_impact": "<the programme consequence, or 'To be assessed on receipt of clarification'>"
    }
  ]
}

Return ONLY valid JSON. Produce exactly one entry per discrepancy given.
"""


def _draft_batch(
    batch: List[Dict[str, Any]],
    label_a: str,
    label_b: str,
    project_name: str,
) -> Dict[str, Dict[str, Any]]:
    """Draft one batch of RFIs; returns {discrepancy_id: drafted fields}."""
    lines = [
        f"PROJECT: {project_name}",
        f"DOCUMENT A: {label_a}",
        f"DOCUMENT B: {label_b}",
        "",
    ]
    for disc in batch:
        lines.append(f"--- DISCREPANCY {disc['id']} ---")
        lines.append(f"topic       : {disc.get('topic') or '-'}")
        lines.append(f"type        : {disc.get('type') or '-'}")
        lines.append(f"severity    : {disc.get('severity') or '-'}")
        lines.append(f"discipline  : {disc.get('discipline') or 'general'}")
        lines.append(f"description : {disc.get('description') or '-'}")
        lines.append(f"{label_a} says : {disc.get('document_a_says') or '-'}")
        lines.append(f"{label_a} ref  : {disc.get('document_a_reference') or 'not stated'}")
        lines.append(f"{label_b} says : {disc.get('document_b_says') or '-'}")
        lines.append(f"{label_b} ref  : {disc.get('document_b_reference') or 'not stated'}")
        lines.append(f"impact      : {disc.get('impact') or '-'}")
        lines.append(f"question hint: {disc.get('suggested_question') or '-'}")
        lines.append("")

    try:
        result = _get_llm().run(
            system=_DRAFT_SYSTEM,
            user="\n".join(lines),
            temperature=0.2,
        )
    except Exception as exc:
        print(f"[rfi_drafting] Draft batch failed: {exc}")
        return {}

    drafted: Dict[str, Dict[str, Any]] = {}
    for item in result.get("rfis") or []:
        if isinstance(item, dict) and item.get("discrepancy_id"):
            drafted[str(item["discrepancy_id"]).strip()] = item
    return drafted


def _fallback_draft(disc: Dict[str, Any], label_a: str, label_b: str) -> Dict[str, Any]:
    """
    Build an RFI from the discrepancy record alone, without the LLM.

    Used when the drafting call fails or omits an entry, so that every
    discrepancy still reaches the engineer as an editable draft.
    """
    return {
        "subject": disc.get("topic") or "Clarification required",
        "discipline": disc.get("discipline") or "general",
        "background": (
            f"A review of '{label_a}' against '{label_b}' identified an inconsistency "
            f"concerning {disc.get('topic') or 'the item below'}."
        ),
        "discrepancy_summary": (
            f"{disc.get('description') or ''} "
            f"{label_a} states: {disc.get('document_a_says') or 'not stated'}. "
            f"{label_b} states: {disc.get('document_b_says') or 'not stated'}."
        ).strip(),
        "question": (
            disc.get("suggested_question")
            or f"Please confirm which requirement governs for {disc.get('topic') or 'this item'}."
        ),
        "proposed_solution": "To be agreed with the Engineer on receipt of clarification.",
        "cost_impact": disc.get("impact") or "To be assessed on receipt of clarification",
        "schedule_impact": "To be assessed on receipt of clarification",
    }


# ---------------------------------------------------------------------------
# Stage 2 - fill the project's .docx RFI form
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

    return {
        "rfi_no":               rfi.get("rfi_no", ""),
        "rfi_date":             today.strftime("%d %B %Y"),
        "response_required_by": rfi.get("response_required_by") or due.strftime("%d %B %Y"),
        "project_name":         state.get("project_name") or "",
        "consultant":           form_meta.get("consultant", ""),
        "employer":             form_meta.get("employer", ""),
        "project_code":         form_meta.get("project_code", ""),
        "to":                   form_meta.get("to", ""),
        "location":             form_meta.get("location", ""),
        "boq_no":               form_meta.get("boq_no", ""),
        "dwg_no":               form_meta.get("dwg_no", ""),
        "level":                form_meta.get("level", ""),
        "specs_no":             form_meta.get("specs_no", ""),
        "discipline":           rfi.get("discipline", ""),
        "priority":             rfi.get("priority", ""),
        "subject":              rfi.get("subject", ""),
        "document_a_label":     label_a,
        "document_b_label":     label_b,
        "document_a_reference": rfi.get("document_a_reference", ""),
        "document_b_reference": rfi.get("document_b_reference", ""),
        "background":           rfi.get("background", ""),
        "discrepancy_summary":  rfi.get("discrepancy_summary", ""),
        "question":             rfi.get("question", ""),
        "proposed_solution":    rfi.get("proposed_solution", ""),
        "cost_impact":          rfi.get("cost_impact", ""),
        "schedule_impact":      rfi.get("schedule_impact", ""),
        "originator":           state.get("user_id") or "",
        "discrepancy_id":       rfi.get("discrepancy_id", ""),
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
    rfi_template_path (optional)

    Produces
    --------
    rfi_items    : one drafted RFI per discrepancy, editable on screen
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

    # --- Stage 1: draft prose, in batches ---------------------------------
    drafted: Dict[str, Dict[str, Any]] = {}
    total_batches = (len(discrepancies) + DRAFT_BATCH_SIZE - 1) // DRAFT_BATCH_SIZE

    for batch_no in range(total_batches):
        batch = discrepancies[batch_no * DRAFT_BATCH_SIZE: (batch_no + 1) * DRAFT_BATCH_SIZE]
        print(f"[rfi_drafting]   -> drafting batch {batch_no + 1}/{total_batches} "
              f"({len(batch)} RFI(s))")
        drafted.update(_draft_batch(batch, label_a, label_b, project_name))

    # --- Assemble the RFI records -----------------------------------------
    due_date = (datetime.now() + timedelta(days=DEFAULT_RESPONSE_DAYS)).strftime("%d %B %Y")
    rfi_items: List[Dict[str, Any]] = []

    for i, disc in enumerate(discrepancies, start=1):
        fields = drafted.get(disc["id"])
        if not fields:
            print(f"[rfi_drafting] {disc['id']}: no LLM draft returned - using fallback text")
            fields = _fallback_draft(disc, label_a, label_b)

        rfi_items.append({
            "rfi_no": f"RFI-{i:03d}",
            "discrepancy_id": disc["id"],
            "subject": str(fields.get("subject") or disc.get("topic") or "").strip(),
            "discipline": str(fields.get("discipline") or disc.get("discipline") or "general").strip(),
            "priority": PRIORITY_BY_SEVERITY.get(disc.get("severity", "minor"), "low"),
            "background": str(fields.get("background") or "").strip(),
            "discrepancy_summary": str(fields.get("discrepancy_summary") or "").strip(),
            "document_a_reference": disc.get("document_a_reference") or "",
            "document_b_reference": disc.get("document_b_reference") or "",
            "question": str(fields.get("question") or disc.get("suggested_question") or "").strip(),
            "proposed_solution": str(fields.get("proposed_solution") or "").strip(),
            "cost_impact": str(fields.get("cost_impact") or "").strip(),
            "schedule_impact": str(fields.get("schedule_impact") or "").strip(),
            "response_required_by": due_date,
            "docx_path": None,
        })

    # --- Stage 2: fill the .docx form -------------------------------------
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
        export_dir = os.path.join(os.getcwd(), "exports")
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
            f"draft_rfi: {len(rfi_items)} drafted, {len(rfi_paths)} .docx written"
        ],
    }
