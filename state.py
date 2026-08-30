"""
LangGraph State Definition — ConflictScan / RFI Automation
==========================================================

The pipeline compares ANY TWO documents drawn from different stages of the
same project (e.g. Tender BOQ vs IFC Specifications, 100% DD Drawings vs
Construction Specs, Schematic Report vs Tender Addendum) and produces:

    1. A structured discrepancy register.
    2. A set of draft RFIs (Requests For Information) the engineer can edit
       and send to the client for clarification.

Nothing in this state is specific to BOQ, specifications, or material lists.
The two inputs are simply "document A" and "document B", each carrying a
user-supplied stage label used verbatim in prompts and in every output.

Windows compatible.
"""

from typing import TypedDict, Optional, List, Dict, Any, Annotated
from langgraph.channels import LastValue


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------

def add_to_list(existing: List, new: List) -> List:
    """Reducer that appends to a list instead of replacing it."""
    if existing is None:
        existing = []
    if new is None:
        new = []
    if not isinstance(new, list):
        new = [new]
    return existing + new


def merge_dicts(existing: Dict, new: Dict) -> Dict:
    """Reducer that merges dictionaries."""
    if existing is None:
        existing = {}
    if new is None:
        new = {}
    return {**existing, **new}


# ---------------------------------------------------------------------------
# Record shapes
# ---------------------------------------------------------------------------

class DocumentProfile(TypedDict):
    """Everything extracted from ONE document, independent of the other."""
    slot: str                      # "a" | "b"
    label: str                     # user-supplied stage label, e.g. "Tender BOQ"
    source_name: str               # original filename, or "pasted text"
    char_count: int
    chunk_count: int
    total_chunk_count: int         # chunk count for the whole document, before any focus sectioning
    focus_status: str              # no_focus | sectioned | no_headings_found | no_relevant_headings | selection_failed
    requirements: List[Dict[str, Any]]   # atomic statements the doc asserts
    quantities: List[Dict[str, Any]]     # numeric/measurable commitments
    standards: List[str]                 # ASTM / BS / EN / ECP / ISO refs
    entities: List[str]                  # elements, systems, locations named
    internal_notes: str


class Discrepancy(TypedDict):
    """One conflict, gap, or ambiguity found BETWEEN the two documents."""
    id: str                        # "D-001"
    topic: str                     # short subject, e.g. "Concrete grade — columns"
    type: str                      # contradiction | omission | ambiguity | scope_gap
    severity: str                  # critical | major | minor
    description: str
    document_a_says: str
    document_b_says: str
    document_a_reference: str      # section / page / clause locator, if stated
    document_b_reference: str
    impact: str                    # cost / schedule / quality consequence
    suggested_question: str        # the clarification to put to the client


class RFIItem(TypedDict):
    """One drafted RFI, ready to be written into the client's RFI form."""
    rfi_no: str
    discrepancy_id: str
    subject: str
    discipline: str
    priority: str                  # high | medium | low
    background: str
    discrepancy_summary: str
    document_a_reference: str
    document_b_reference: str
    question: str
    proposed_solution: str
    cost_impact: str
    schedule_impact: str
    response_required_by: str
    docx_path: Optional[str]


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------

class ConflictScanState(TypedDict):
    """Shared state for the two-document conflict scan → RFI pipeline."""

    # --- Session -----------------------------------------------------------
    user_id: Annotated[str, LastValue]
    session_id: Optional[str]
    project_name: Optional[str]

    # --- Document A --------------------------------------------------------
    document_a_text: Annotated[str, LastValue]
    document_a_label: Annotated[str, LastValue]     # e.g. "Tender BOQ"
    document_a_source: Annotated[str, LastValue]    # filename or "pasted text"

    # --- Document B --------------------------------------------------------
    document_b_text: Annotated[str, LastValue]
    document_b_label: Annotated[str, LastValue]     # e.g. "IFC Specifications"
    document_b_source: Annotated[str, LastValue]

    # --- Optional steer ----------------------------------------------------
    review_focus: Optional[str]     # free-text: what the engineer wants checked

    # --- Per-document analysis --------------------------------------------
    profile_a: Annotated[Dict[str, Any], merge_dicts]
    profile_b: Annotated[Dict[str, Any], merge_dicts]
    documents_analyzed: Optional[bool]

    # --- Cross-document conflict scan -------------------------------------
    discrepancies: Annotated[List[Discrepancy], add_to_list]
    scan_summary: Annotated[str, LastValue]
    confidence_level: Optional[str]                 # high | medium | low
    analysis_warnings: Annotated[List[str], add_to_list]
    scan_complete: Optional[bool]

    # --- RFI drafting ------------------------------------------------------
    rfi_template_path: Optional[str]                # .docx template to fill
    rfi_form_meta: Annotated[Dict[str, Any], merge_dicts]  # consultant, employer, project_code, to, location, boq_no, dwg_no, level, specs_no
    rfi_items: Annotated[List[RFIItem], add_to_list]
    rfi_paths: Annotated[List[str], add_to_list]    # generated .docx files
    rfi_drafted: Optional[bool]

    # --- Export ------------------------------------------------------------
    export_format: Optional[str]                    # excel | csv
    register_path: Optional[str]                    # discrepancy register file
    export_complete: Optional[bool]

    # --- Metadata ----------------------------------------------------------
    error_message: Optional[str]
    step_history: Annotated[List[str], add_to_list]
    timestamps: Annotated[Dict[str, str], merge_dicts]
    event_id: Optional[str]


def create_initial_state() -> ConflictScanState:
    """Create an initial state with safe defaults."""
    return ConflictScanState(
        user_id=None,
        session_id=None,
        project_name=None,

        document_a_text="",
        document_a_label="Document A",
        document_a_source="",

        document_b_text="",
        document_b_label="Document B",
        document_b_source="",

        review_focus="",

        profile_a={},
        profile_b={},
        documents_analyzed=False,

        discrepancies=[],
        scan_summary="",
        confidence_level="medium",
        analysis_warnings=[],
        scan_complete=False,

        rfi_template_path=None,
        rfi_form_meta={},
        rfi_items=[],
        rfi_paths=[],
        rfi_drafted=False,

        export_format="excel",
        register_path=None,
        export_complete=False,

        error_message=None,
        step_history=[],
        timestamps={},
        event_id=None,
    )


