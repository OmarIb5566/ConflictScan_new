"""
ConflictScan / RFI Automation

Compares any two documents drawn from different stages of the same project,
reports the discrepancies between them, and drafts an RFI for each one so the
engineer can send it to the client for clarification.

Windows Compatible Version
"""

__version__ = "3.0.0"
__author__ = "AI Engineering Team"

from .graph import conflict_scan_graph, run_conflict_scan
from .state import ConflictScanState, create_initial_state

__all__ = [
    "conflict_scan_graph",
    "run_conflict_scan",
    "ConflictScanState",
    "create_initial_state",
]
