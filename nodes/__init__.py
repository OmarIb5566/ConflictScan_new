"""
Nodes package - ConflictScan / RFI Automation
"""

from .input_processing import receive_input
from .file_ingest import classify_file, read_document
from .document_analysis import (
    analyze_documents,
    build_document_profile,
    chunk_document,
)
from .drawing_analysis import build_drawing_profile
from .conflict_scan import (
    conflict_scan_agent,
    align_requirements,
    compare_quantities,
    internal_candidates,
    interface_clusters,
)
from .rfi_drafting import (
    rfi_drafting_agent,
    group_discrepancies,
    resolve_template_path,
    build_token_values,
)
from .docx_template import fill_docx_template, list_template_tokens
from .export import export_discrepancy_register

__all__ = [
    # Input
    "receive_input",
    "classify_file",
    "read_document",
    # Per-document analysis
    "analyze_documents",
    "build_document_profile",
    "chunk_document",
    "build_drawing_profile",
    # Cross-document scan
    "conflict_scan_agent",
    "align_requirements",
    "compare_quantities",
    "internal_candidates",
    "interface_clusters",
    # RFI drafting
    "rfi_drafting_agent",
    "group_discrepancies",
    "resolve_template_path",
    "build_token_values",
    "fill_docx_template",
    "list_template_tokens",
    # Export
    "export_discrepancy_register",
]
