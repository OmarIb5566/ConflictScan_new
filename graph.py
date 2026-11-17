"""
LangGraph Builder - ConflictScan / RFI Automation
==================================================

    receive_input          validate the two document sets and their stage labels
         |
    analyze_documents      read every file of each set (OCR for scans), profile
         |                 documents and drawings; a drawing is read against a
         |                 baseline built from the other set
         |
    conflict_scan          compare the two sets, check each set for internal
         |                 inconsistencies, review interfaces/clashes
         |                 -> discrepancy register
         |
    (any findings?) --no--> export
         |yes
    draft_rfi              group related findings into RFIs with numbered
         |                 points, fill the .docx form
         |
    export                 discrepancy register + RFI log
         |
        END

The pipeline is document-agnostic: the two inputs are any two documents from
different stages of the same project, identified only by a user-supplied label
("Tender BOQ", "IFC Specifications", "100% DD Drawings", ...). Those labels
flow through every prompt and every output.
"""

from typing import Callable, Dict, Any, List

from langgraph.graph import StateGraph, END

from state import ConflictScanState, create_initial_state

from nodes.input_processing import receive_input
from nodes.document_analysis import analyze_documents
from nodes.conflict_scan import conflict_scan_agent
from nodes.rfi_drafting import rfi_drafting_agent
from nodes.export import export_discrepancy_register

from edges import route_input_valid, route_discrepancies


def create_conflict_scan_graph() -> StateGraph:
    """Build and compile the two-document conflict scan -> RFI graph."""
    workflow = StateGraph(ConflictScanState)

    workflow.add_node("receive_input", receive_input)
    workflow.add_node("analyze_documents", analyze_documents)
    workflow.add_node("conflict_scan", conflict_scan_agent)
    workflow.add_node("draft_rfi", rfi_drafting_agent)
    workflow.add_node("export", export_discrepancy_register)

    workflow.set_entry_point("receive_input")

    workflow.add_conditional_edges(
        "receive_input",
        route_input_valid,
        {
            "analyze": "analyze_documents",
            "abort": END,
        },
    )

    workflow.add_edge("analyze_documents", "conflict_scan")

    workflow.add_conditional_edges(
        "conflict_scan",
        route_discrepancies,
        {
            "draft_rfi": "draft_rfi",
            "no_findings": "export",
        },
    )

    workflow.add_edge("draft_rfi", "export")
    workflow.add_edge("export", END)

    return workflow.compile()


conflict_scan_graph = create_conflict_scan_graph()


def run_conflict_scan(
    user_id: str,
    document_a_text: str,
    document_b_text: str,
    document_a_label: str = "Document A",
    document_b_label: str = "Document B",
    document_a_source: str = "pasted text",
    document_b_source: str = "pasted text",
    document_a_kind: str = "text",
    document_b_kind: str = "text",
    document_a_drawing_pdf_path: str = None,
    document_a_drawing_dxf_path: str = None,
    document_b_drawing_pdf_path: str = None,
    document_b_drawing_dxf_path: str = None,
    document_a_files: List[Dict[str, Any]] = None,
    document_b_files: List[Dict[str, Any]] = None,
    project_name: str = None,
    review_focus: str = None,
    document_precedence: str = None,
    rfi_grouping: str = "consolidated",
    rfi_template_path: str = None,
    rfi_form_meta: Dict[str, Any] = None,
    export_format: str = "excel",
    on_node_complete: Callable[[str, Dict[str, Any]], None] = None,
    should_stop: Callable[[], bool] = None,
) -> Dict[str, Any]:
    """
    Run a conflict scan over two document sets from different project stages.

    Parameters
    ----------
    document_a_files / document_b_files : the files of each set
        ([{name, path, kind, dxf_path}], see state.SourceFile). When given they
        replace the single-document text/drawing arguments below, which are
        kept for callers that compare one text against another.
    document_a_text / document_b_text : full plain text of each document
    document_a_label / document_b_label : the stage each document belongs to,
        as the engineer described it. Used verbatim in prompts and outputs.
    review_focus : optional free text steering what the scan pays attention to
    rfi_template_path : optional .docx RFI form; defaults to templates_rfi/
    on_node_complete : optional callback(node_name, node_delta) fired as each
        graph node finishes - used by the web app to show live progress
    should_stop : optional callable; when it returns True the run stops after
        the node in progress and ScanCancelled is raised
    """
    initial_state = create_initial_state()
    initial_state.update({
        "user_id": user_id,
        "project_name": project_name or "Unnamed Project",
        "document_a_text": document_a_text,
        "document_b_text": document_b_text,
        "document_a_label": document_a_label,
        "document_b_label": document_b_label,
        "document_a_source": document_a_source,
        "document_b_source": document_b_source,
        "document_a_kind": document_a_kind or "text",
        "document_b_kind": document_b_kind or "text",
        "document_a_drawing_pdf_path": document_a_drawing_pdf_path,
        "document_a_drawing_dxf_path": document_a_drawing_dxf_path,
        "document_b_drawing_pdf_path": document_b_drawing_pdf_path,
        "document_b_drawing_dxf_path": document_b_drawing_dxf_path,
        "document_a_files": document_a_files or [],
        "document_b_files": document_b_files or [],
        "review_focus": review_focus or "",
        "document_precedence": document_precedence or "",
        "rfi_grouping": rfi_grouping or "consolidated",
        "rfi_template_path": rfi_template_path,
        "rfi_form_meta": rfi_form_meta or {},
        "export_format": export_format,
    })

    print("\nStarting conflict scan:")
    for label, files, text, source in (
        (document_a_label, document_a_files, document_a_text, document_a_source),
        (document_b_label, document_b_files, document_b_text, document_b_source),
    ):
        if files:
            print(f"   '{label}': {len(files)} file(s) - " + ", ".join(f"{f.get('name')} [{f.get('kind')}]" for f in files))
        else:
            print(f"   '{label}': {len(text or ''):,} chars, {source}")
    if review_focus:
        print(f"   Focus: {review_focus}")

    if on_node_complete is None and should_stop is None:
        return conflict_scan_graph.invoke(initial_state)

    final_state = initial_state
    for mode, chunk in conflict_scan_graph.stream(initial_state, stream_mode=["updates", "values"]):
        if mode == "values":
            final_state = chunk
            continue
        for node_name, delta in (chunk or {}).items():
            if on_node_complete:
                on_node_complete(node_name, delta or {})
        if should_stop and should_stop():
            raise ScanCancelled()
    return final_state


class ScanCancelled(Exception):
    """Raised by run_conflict_scan when should_stop() asks the run to stop."""


