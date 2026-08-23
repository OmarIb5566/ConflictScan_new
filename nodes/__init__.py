"""
Nodes package - ConflictScan / RFI Automation
"""

from .input_processing import receive_input
from .document_analysis import (
    analyze_documents,
    build_document_profile,
    chunk_document,
)
from .conflict_scan import (
    conflict_scan_agent,
    align_requirements,
    compare_quantities,
)
from .rfi_drafting import (
    rfi_drafting_agent,
    resolve_template_path,
    build_token_values,
)
from .docx_template import fill_docx_template, list_template_tokens
from .export import export_discrepancy_register

__all__ = [
    # Input
    "receive_input",
    # Per-document analysis
    "analyze_documents",
    "build_document_profile",
    "chunk_document",
    # Cross-document scan
    "conflict_scan_agent",
    "align_requirements",
    "compare_quantities",
    # RFI drafting
    "rfi_drafting_agent",
    "resolve_template_path",
    "build_token_values",
    "fill_docx_template",
    "list_template_tokens",
    # Export
    "export_discrepancy_register",
]
