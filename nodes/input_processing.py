"""
Input Processing Nodes - ConflictScan / RFI Automation

Each side of a comparison is a SET of files. The API saves uploads to disk
and passes the file list; pasted text arrives as a one-entry set. Reading the
files (text extraction, OCR, drawing renders) happens in analyze_documents, so
progress on long OCR/vision runs shows up in the scan log.

Older callers that pass one text, or one drawing PDF/DXF, per side are folded
into the same set shape here.

All nodes return a delta only - no full state overwrite.
"""

import os
from datetime import datetime
from typing import Dict, Any, List


def _legacy_files(state: Dict[str, Any], slot: str) -> List[Dict[str, Any]]:
    """The single-document fields of older callers, as a one-entry file set."""
    if state.get(f"document_{slot}_kind") == "drawing":
        pdf = state.get(f"document_{slot}_drawing_pdf_path")
        dxf = state.get(f"document_{slot}_drawing_dxf_path")
        if pdf or dxf:
            return [{
                "name": state.get(f"document_{slot}_source") or os.path.basename(pdf or dxf),
                "path": pdf, "dxf_path": dxf, "kind": "drawing",
            }]
        return []
    text = (state.get(f"document_{slot}_text") or "").strip()
    if text:
        return [{"name": state.get(f"document_{slot}_source") or "pasted text", "kind": "text", "text": text}]
    return []


def _usable(entry: Dict[str, Any]) -> bool:
    if entry.get("kind") == "text":
        return bool((entry.get("text") or "").strip())
    paths = (entry.get("path"), entry.get("dxf_path"))
    return any(p and os.path.exists(p) for p in paths)


def receive_input(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate that both sides have at least one usable file and normalise the
    stage labels.

    A missing label is filled with a neutral placeholder rather than being
    guessed at - the label appears verbatim in every discrepancy and every RFI,
    so an invented one would be misleading.
    """
    sets: Dict[str, List[Dict[str, Any]]] = {}
    for slot, ordinal in (("a", "first"), ("b", "second")):
        files = list(state.get(f"document_{slot}_files") or []) or _legacy_files(state, slot)
        unusable = [f.get("name", "?") for f in files if not _usable(f)]
        files = [f for f in files if _usable(f)]
        if not files:
            detail = f" ({', '.join(unusable)} could not be found or is empty)" if unusable else ""
            return {
                "error_message": (
                    f"The {ordinal} document set is empty{detail}. Two document sets are required to run a "
                    f"comparison - each needs at least one file or pasted text. Drawings must be PDF, image or DXF."
                ),
                "step_history": [f"receive_input: rejected (empty {ordinal} set)"],
            }
        sets[slot] = files

    label_a = (state.get("document_a_label") or "").strip() or "Document A"
    label_b = (state.get("document_b_label") or "").strip() or "Document B"

    # Identical labels make every discrepancy ambiguous to read - disambiguate.
    if label_a.lower() == label_b.lower():
        label_a = f"{label_a} (1)"
        label_b = f"{label_b} (2)"

    describe = lambda files: ", ".join(f"{f.get('name')} [{f.get('kind')}]" for f in files)
    return {
        "document_a_files": sets["a"],
        "document_b_files": sets["b"],
        "document_a_label": label_a,
        "document_b_label": label_b,
        "document_a_source": " + ".join(f.get("name", "") for f in sets["a"]),
        "document_b_source": " + ".join(f.get("name", "") for f in sets["b"]),
        "step_history": [
            f"receive_input: '{label_a}' ({len(sets['a'])} file(s): {describe(sets['a'])}) vs "
            f"'{label_b}' ({len(sets['b'])} file(s): {describe(sets['b'])})"
        ],
        "timestamps": {
            **(state.get("timestamps") or {}),
            "input_received": datetime.now().isoformat(),
        },
    }
