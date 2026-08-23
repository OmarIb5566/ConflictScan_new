"""
Input Processing Nodes - ConflictScan / RFI Automation

Text extraction from uploaded files happens at the API layer (api.py), which
knows about PDF/DOCX/XLSX readers. By the time the graph runs, both documents
are plain text. These nodes validate and normalise that input.

All nodes return a delta only - no full state overwrite.
"""

from datetime import datetime
from typing import Dict, Any, List


def receive_input(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate that two documents were supplied and normalise their labels.

    A missing label is filled with a neutral placeholder rather than being
    guessed at - the label appears verbatim in every discrepancy and every RFI,
    so an invented one would be misleading.
    """
    text_a = (state.get("document_a_text") or "").strip()
    text_b = (state.get("document_b_text") or "").strip()

    missing: List[str] = []
    if not text_a:
        missing.append("first")
    if not text_b:
        missing.append("second")

    if missing:
        return {
            "error_message": (
                "The " + " and ".join(missing) + " document is empty. "
                "Two documents are required to run a comparison."
            ),
            "step_history": ["receive_input: rejected (missing document)"],
        }

    label_a = (state.get("document_a_label") or "").strip() or "Document A"
    label_b = (state.get("document_b_label") or "").strip() or "Document B"

    # Identical labels make every discrepancy ambiguous to read - disambiguate.
    if label_a.lower() == label_b.lower():
        label_a = f"{label_a} (1)"
        label_b = f"{label_b} (2)"

    return {
        "document_a_text": text_a,
        "document_b_text": text_b,
        "document_a_label": label_a,
        "document_b_label": label_b,
        "document_a_source": (state.get("document_a_source") or "pasted text"),
        "document_b_source": (state.get("document_b_source") or "pasted text"),
        "step_history": [f"receive_input: '{label_a}' vs '{label_b}'"],
        "timestamps": {
            **(state.get("timestamps") or {}),
            "input_received": datetime.now().isoformat(),
        },
    }
