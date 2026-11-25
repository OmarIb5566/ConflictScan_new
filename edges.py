"""
Edge Routing Functions - ConflictScan / RFI Automation

Conditional-edge predicates for graph.py. Each takes the state and returns the
name of the branch to follow.
"""

from typing import Dict, Any, Literal


def route_input_valid(state: Dict[str, Any]) -> Literal["analyze", "abort"]:
    """
    Stop the run when receive_input rejected the input.

    Two documents are the whole premise of this pipeline, so there is no
    degraded path to fall back to.
    """
    if state.get("error_message"):
        return "abort"
    return "analyze"


def route_inputs_sufficient(state: Dict[str, Any]) -> Literal["compare", "insufficient"]:
    """
    Compare only when both sets gave something to compare.

    An empty or unreadable set is reported as such (nodes/assessment.py) - the
    scan does not go looking for discrepancies against nothing.
    """
    assessment = state.get("input_assessment") or {}
    return "compare" if assessment.get("ok", True) else "insufficient"


def route_discrepancies(state: Dict[str, Any]) -> Literal["draft_rfi", "no_findings"]:
    """
    Only draft RFIs when the scan actually found something.

    A clean scan is a valid, useful result - it goes straight to export, which
    records that the two documents were compared and agreed.
    """
    if state.get("discrepancies"):
        return "draft_rfi"
    return "no_findings"
