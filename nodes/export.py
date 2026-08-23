"""
Export Node - ConflictScan / RFI Automation
============================================
Writes the discrepancy register to a file the team can circulate.

Two sheets when Excel is available:
    "Discrepancy Register" - one row per discrepancy, both documents quoted
    "RFI Log"              - one row per drafted RFI, with the .docx filename

Falls back to CSV when pandas/openpyxl are not installed. The RFI .docx files
themselves are written by nodes/rfi_drafting.py; this node only records them.

Returns delta only - no full state overwrite.
"""

import csv
import os
from datetime import datetime
from typing import Dict, Any, List

REGISTER_COLUMNS = [
    ("id", "ID"),
    ("severity", "Severity"),
    ("type", "Type"),
    ("discipline", "Discipline"),
    ("topic", "Topic"),
    ("description", "Description"),
    ("document_a_says", "{label_a} states"),
    ("document_a_reference", "{label_a} reference"),
    ("document_b_says", "{label_b} states"),
    ("document_b_reference", "{label_b} reference"),
    ("impact", "Impact"),
    ("suggested_question", "Clarification sought"),
]

RFI_COLUMNS = [
    ("rfi_no", "RFI No."),
    ("discrepancy_id", "Discrepancy"),
    ("priority", "Priority"),
    ("discipline", "Discipline"),
    ("subject", "Subject"),
    ("question", "Question to client"),
    ("proposed_solution", "Proposed way forward"),
    ("cost_impact", "Cost impact"),
    ("schedule_impact", "Programme impact"),
    ("response_required_by", "Response required by"),
    ("docx_file", "RFI document"),
]


def _safe_name(text: str) -> str:
    keep = [c if (c.isalnum() or c in " -_") else "_" for c in str(text)]
    return "".join(keep).strip().replace(" ", "_")[:60] or "project"


def _register_rows(discrepancies: List[Dict[str, Any]], label_a: str, label_b: str):
    """Build (headers, rows) for the discrepancy register."""
    headers = [
        title.format(label_a=label_a, label_b=label_b)
        for _key, title in REGISTER_COLUMNS
    ]
    rows = [
        [str(disc.get(key, "") or "") for key, _title in REGISTER_COLUMNS]
        for disc in discrepancies
    ]
    return headers, rows


def _rfi_rows(rfi_items: List[Dict[str, Any]]):
    """Build (headers, rows) for the RFI log."""
    headers = [title for _key, title in RFI_COLUMNS]
    rows = []
    for rfi in rfi_items:
        row = []
        for key, _title in RFI_COLUMNS:
            if key == "docx_file":
                path = rfi.get("docx_path")
                row.append(os.path.basename(path) if path else "not generated")
            else:
                row.append(str(rfi.get(key, "") or ""))
        rows.append(row)
    return headers, rows


def export_discrepancy_register(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph node: export

    Consumes discrepancies, rfi_items, project_name, document labels.
    Produces register_path, export_complete, step_history.
    """
    discrepancies = state.get("discrepancies") or []
    rfi_items = state.get("rfi_items") or []
    label_a = state.get("document_a_label") or "Document A"
    label_b = state.get("document_b_label") or "Document B"
    project_name = state.get("project_name") or "Unnamed Project"
    export_format = (state.get("export_format") or "excel").lower()

    if not discrepancies:
        print("[export] No discrepancies to export.")
        return {
            "export_complete": True,
            "step_history": ["export: skipped (no discrepancies)"],
        }

    export_dir = os.path.join(os.getcwd(), "exports")
    os.makedirs(export_dir, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    base = f"{_safe_name(project_name)}_conflict_scan_{stamp}"

    reg_headers, reg_rows = _register_rows(discrepancies, label_a, label_b)
    rfi_headers, rfi_rows = _rfi_rows(rfi_items)

    path = None
    if export_format == "excel":
        path = _write_excel(export_dir, base, reg_headers, reg_rows, rfi_headers, rfi_rows)

    if not path:
        path = _write_csv(export_dir, base, reg_headers, reg_rows)

    print(f"[export] Register written: {path}")

    return {
        "register_path": path,
        "export_complete": True,
        "step_history": [f"export: {len(discrepancies)} discrepancies, {len(rfi_items)} RFIs"],
    }


def _write_excel(export_dir, base, reg_headers, reg_rows, rfi_headers, rfi_rows):
    """Write the two-sheet workbook. Returns the path, or None if pandas is absent."""
    try:
        import pandas as pd
    except ImportError:
        print("[export] pandas not available - falling back to CSV")
        return None

    filepath = os.path.join(export_dir, f"{base}.xlsx")
    try:
        with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
            pd.DataFrame(reg_rows, columns=reg_headers).to_excel(
                writer, sheet_name="Discrepancy Register", index=False
            )
            if rfi_rows:
                pd.DataFrame(rfi_rows, columns=rfi_headers).to_excel(
                    writer, sheet_name="RFI Log", index=False
                )
        return filepath
    except Exception as exc:
        print(f"[export] Excel export failed ({exc}) - falling back to CSV")
        return None


def _write_csv(export_dir, base, headers, rows):
    """Write the register as CSV. Always available."""
    filepath = os.path.join(export_dir, f"{base}.csv")
    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    return filepath


