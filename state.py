"""
LangGraph State Definition — ConflictScan / RFI Automation
==========================================================

The pipeline compares TWO DOCUMENT SETS drawn from different stages or
disciplines of the same project (e.g. Tender BOQ + Specifications vs IFC
drawings, Structural IFC sheets vs Architectural IFC sheets). Each side is a
set of one or more files - real RFIs cite a median of two and up to nineteen
attachments - mixing text documents and drawings. It produces:

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
    files: List[Dict[str, Any]]          # per-file summary when the side is a set of files


class DrawingProfile(DocumentProfile, total=False):
    """A DocumentProfile read from a drawing (nodes/drawing_analysis.py) - the As-Drawn Set."""
    drawing_inputs: Dict[str, Any]           # {"pdf": bool, "dxf": bool, "visual_source": "pdf"|"dxf"}
    sheets: List[Dict[str, Any]]             # per-sheet title block / structural system / grids / clouds
    baseline: Optional[Dict[str, Any]]       # Baseline Requirement Set built from the other document
    baseline_coverage: List[Dict[str, Any]]  # each baseline item: located | unreadable | not_located | not_checked
    dxf_summary: Optional[Dict[str, Any]]    # units, layers, entity types, block counts, grid labels
    warnings: List[str]                      # engineer-review flags raised while reading the drawing


class SourceFile(TypedDict, total=False):
    """One file in a document set (one side of the comparison)."""
    name: str                      # original filename, or "pasted text"
    path: Optional[str]            # saved upload (None for pasted text / DXF-only drawings)
    kind: str                      # "document" | "drawing" | "text" (pasted)
    dxf_path: Optional[str]        # drawing read from PDF + DXF, or DXF only
    text: str                      # pasted text only
    role: str                      # optional user note, e.g. "Electrical SLDs"
    classified_as: str             # why the file was treated as document/drawing


class EvidenceItem(TypedDict, total=False):
    """One source quoted by a finding - a finding may cite several (e.g. two drawings and the BOQ)."""
    side: str                      # "a" | "b"
    document: str                  # side label
    file: str                      # file the statement came from
    reference: str                 # sheet / page / clause / item / grid / section
    says: str                      # the statement or value, as written


class Discrepancy(TypedDict, total=False):
    """One conflict, gap, or ambiguity found BETWEEN the two sets, or within one set."""
    id: str                        # "D-001"
    topic: str                     # short subject, e.g. "Concrete grade — columns"
    type: str                      # contradiction | omission | ambiguity | scope_gap | clash | missing_information | design_change
    issue_class: str               # knowledge/rfi_taxonomy.json key, e.g. spatial_clash, scope_gap_boq
    scope: str                     # between | within_a | within_b
    severity: str                  # critical | major | minor
    category: str                  # analysis_framework key, "" when not classified
    location: str                  # building / area / level / grid / section the finding concerns
    element: str                   # element or system concerned
    evidence: List[EvidenceItem]   # every source quoted, in order
    low_confidence: bool           # rests on OCR text or partly legible drawing values
    description: str
    document_a_says: str
    document_b_says: str
    document_a_reference: str      # section / page / clause locator, if stated
    document_b_reference: str
    impact: str                    # cost / schedule / quality consequence
    suggested_question: str        # the clarification to put to the client


class RFIItem(TypedDict, total=False):
    """One drafted RFI, ready to be written into the client's RFI form.

    Related findings on the same system/area are consolidated into ONE RFI
    with numbered points, the way engineers write them."""
    rfi_no: str
    discrepancy_id: str            # comma-joined ids of every finding in this RFI
    discrepancy_ids: List[str]
    points: List[Dict[str, Any]]   # [{"no", "discrepancy_id", "text", "question"}]
    area: str                      # block / building / zone the RFI concerns
    attachments: List[str]         # sources to attach: file | sheet/page references
    request_type: str              # which_governs | provide_information | confirm_scope | resolve_clash | ...
    commercial_note: str           # variation-order note, when the clarification may add scope
    description: str               # full RFI body as it goes on the form
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
    document_a_kind: Annotated[str, LastValue]      # "text" | "drawing"
    document_a_drawing_pdf_path: Optional[str]      # when kind == "drawing": PDF, DXF, or both
    document_a_drawing_dxf_path: Optional[str]

    # --- Document B --------------------------------------------------------
    document_b_text: Annotated[str, LastValue]
    document_b_label: Annotated[str, LastValue]     # e.g. "IFC Specifications"
    document_b_source: Annotated[str, LastValue]
    document_b_kind: Annotated[str, LastValue]      # "text" | "drawing"
    document_b_drawing_pdf_path: Optional[str]      # when kind == "drawing": PDF, DXF, or both
    document_b_drawing_dxf_path: Optional[str]

    # --- Document sets (one or more files per side) -------------------------
    document_a_files: Annotated[List[SourceFile], LastValue]
    document_b_files: Annotated[List[SourceFile], LastValue]

    # --- Optional steer ----------------------------------------------------
    review_focus: Optional[str]     # free-text: what the engineer wants checked
    document_precedence: Optional[str]   # order of precedence stated in the contract, if any
    rfi_grouping: Optional[str]     # "consolidated" (default) | "single" (one RFI per finding)

    # --- Per-document analysis --------------------------------------------
    profile_a: Annotated[Dict[str, Any], merge_dicts]
    profile_b: Annotated[Dict[str, Any], merge_dicts]
    documents_analyzed: Optional[bool]

    # --- Cross-document conflict scan -------------------------------------
    discrepancies: Annotated[List[Discrepancy], add_to_list]
    scan_summary: Annotated[str, LastValue]
    input_assessment: Optional[Dict[str, Any]]      # enough in both sets to compare? (nodes/assessment.py)
    assessment: Optional[Dict[str, Any]]            # the honest verdict of the scan (nodes/assessment.py)
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
        document_a_kind="text",
        document_a_drawing_pdf_path=None,
        document_a_drawing_dxf_path=None,

        document_b_text="",
        document_b_label="Document B",
        document_b_source="",
        document_b_kind="text",
        document_b_drawing_pdf_path=None,
        document_b_drawing_dxf_path=None,

        document_a_files=[],
        document_b_files=[],

        review_focus="",
        document_precedence="",
        rfi_grouping="consolidated",

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


