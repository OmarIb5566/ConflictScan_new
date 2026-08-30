"""
LangGraph Builder - ConflictScan / RFI Automation
==================================================

    receive_input          validate the two documents and their stage labels
         |
    analyze_documents      chunk and profile EACH document independently
         |
    conflict_scan          compare the two profiles -> discrepancy register
         |
    (any findings?) --no--> export
         |yes
    draft_rfi              write a draft RFI per discrepancy, fill the .docx form
         |
    export                 discrepancy register + RFI log
         |
        END

The pipeline is document-agnostic: the two inputs are any two documents from
different stages of the same project, identified only by a user-supplied label
("Tender BOQ", "IFC Specifications", "100% DD Drawings", ...). Those labels
flow through every prompt and every output.
"""

from typing import Dict, Any

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
    project_name: str = None,
    review_focus: str = None,
    rfi_template_path: str = None,
    rfi_form_meta: Dict[str, Any] = None,
    export_format: str = "excel",
) -> Dict[str, Any]:
    """
    Run a conflict scan over two documents from different project stages.

    Parameters
    ----------
    document_a_text / document_b_text : full plain text of each document
    document_a_label / document_b_label : the stage each document belongs to,
        as the engineer described it. Used verbatim in prompts and outputs.
    review_focus : optional free text steering what the scan pays attention to
    rfi_template_path : optional .docx RFI form; defaults to templates_rfi/
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
        "review_focus": review_focus or "",
        "rfi_template_path": rfi_template_path,
        "rfi_form_meta": rfi_form_meta or {},
        "export_format": export_format,
    })

    print("\nStarting conflict scan:")
    print(f"   A: '{document_a_label}' ({len(document_a_text or ''):,} chars, {document_a_source})")
    print(f"   B: '{document_b_label}' ({len(document_b_text or ''):,} chars, {document_b_source})")
    if review_focus:
        print(f"   Focus: {review_focus}")

    return conflict_scan_graph.invoke(initial_state)


