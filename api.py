"""
Flask API - ConflictScan / RFI Automation
==========================================
Compares any two documents from different stages of the same project, reports
the discrepancies between them, and drafts an RFI for each one.

The API layer owns file reading (PDF / DOCX / XLSX / TXT). The graph only ever
sees plain text, which keeps every node document-format agnostic.

Windows compatible.
"""

import hashlib
import json
import os
import traceback
import uuid
from datetime import datetime
from threading import Lock

from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

from graph import run_conflict_scan
from nodes.rfi_drafting import resolve_template_path, build_token_values
from nodes.docx_template import fill_docx_template, list_template_tokens

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False
    print("Warning: pdfplumber not available, PDF extraction will be limited")

try:
    from docx import Document
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False
    print("Warning: python-docx not available, DOCX extraction will be limited")


app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)

UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
EXPORT_FOLDER = os.path.join(os.getcwd(), "exports")
RFI_TEMPLATE_FOLDER = os.path.join(os.getcwd(), "templates_rfi")
for folder in (UPLOAD_FOLDER, EXPORT_FOLDER, RFI_TEMPLATE_FOLDER):
    os.makedirs(folder, exist_ok=True)

# Completed scans, keyed by event_id, so the UI can re-export edited RFIs
active_sessions = {}
session_lock = Lock()


# ---------------------------------------------------------------------------
# Excel sheet semantic labelling helpers
# ---------------------------------------------------------------------------

# Sheet names that carry no semantic meaning - these need LLM labelling
_GENERIC_SHEET_NAMES = frozenset([
    "sheet1", "sheet2", "sheet3", "sheet4", "sheet5",
    "sheet6", "sheet7", "sheet8", "sheet9", "sheet10",
    "data", "table", "tab", "tab1", "tab2", "tab3",
    "untitled", "new sheet", "sheet", "worksheet",
])


def _is_generic_name(name: str) -> bool:
    """Return True if the sheet name is non-descriptive."""
    return name.strip().lower() in _GENERIC_SHEET_NAMES


def _label_excel_sheets(sheets_info: list) -> dict:
    """
    Make ONE LLM call to assign semantic labels to all Excel sheets at once.

    Parameters
    ----------
    sheets_info : list of dicts with keys "name", "columns", "sample_csv".

    Returns {original sheet name: semantic label}. Empty dict on any failure,
    in which case callers fall back to the original sheet name.
    """
    if not sheets_info:
        return {}
    try:
        from nodes.llm_client import llm_client

        summary = [
            {
                "sheet_name": s["name"],
                "column_headers": s["columns"][:25],       # cap prompt length
                "sample_data": s["sample_csv"][:600],      # first ~600 chars
            }
            for s in sheets_info
        ]

        result = llm_client.run(
            system=(
                "You are a construction project document analyst. "
                "You analyse Excel spreadsheet content from construction projects "
                "to identify what each sheet contains and assign a clear label."
            ),
            user=(
                "Analyse these Excel sheets from a construction project document "
                "and assign a concise, descriptive label (max 60 chars) to each.\n\n"
                + json.dumps(summary, indent=2)
                + "\n\nReturn JSON:\n"
                '{"labels": {"<sheet_name>": "<semantic label>", ...}}\n\n'
                "Label examples:\n"
                '- "Bill of Quantities (BOQ)" - item lists with quantities/units\n'
                '- "Technical Specifications" - material/workmanship requirements\n'
                '- "Unit Rate Schedule" - price or rate tables\n'
                '- "Material Submittal List" - approved-material lists\n'
                '- "Preliminary & General Items" - mobilisation/general costs\n'
                '- "Project Summary" - summary or overview data\n'
                '- "Subcontractor Breakdown" - sub-contract cost breakdown\n'
                "Use the most specific label that fits the actual content."
            ),
            temperature=0.1,
        )
        return result.get("labels", {})
    except Exception as exc:
        print(f"[api] Excel sheet LLM labelling failed: {exc}")
        return {}


def _standardise_excel_df(df):
    """
    Clean up a raw DataFrame read from an Excel sheet:
    1. Drop fully-empty rows/columns.
    2. Forward-fill to handle merged (spanning) cells.
    3. Detect and promote the best header row (most text-like cells).
    4. Clean column names (strip whitespace, collapse newlines).
    5. Remove duplicate-header rows that sometimes recur mid-sheet.
    """
    if not PANDAS_AVAILABLE:
        return df

    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        return df

    df = df.ffill(axis=0)

    best_row_idx = 0
    best_score = -1
    for row_idx in range(min(5, len(df))):
        row = df.iloc[row_idx]
        score = int(row.apply(lambda v: isinstance(v, str) and len(str(v).strip()) > 1).sum())
        if score > best_score:
            best_score = score
            best_row_idx = row_idx

    # Only promote the row as header if at least 30% of its cells are textual
    if best_score >= max(1, len(df.columns) * 0.3):
        raw_headers = df.iloc[best_row_idx].tolist()
        seen: dict = {}
        clean_cols = []
        for h in raw_headers:
            col = str(h).strip().replace("\n", " ").replace("\r", "")
            if not col or col.lower() in ("nan", "none"):
                col = f"col_{len(clean_cols)}"
            if col in seen:
                seen[col] += 1
                col = f"{col}_{seen[col]}"
            else:
                seen[col] = 0
            clean_cols.append(col)
        df.columns = clean_cols
        df = df.iloc[best_row_idx + 1:].reset_index(drop=True)
    else:
        df.columns = [f"col_{i}" for i in range(len(df.columns))]

    header_set = {str(c).strip().lower() for c in df.columns if not str(c).startswith("col_")}
    if header_set:
        def _is_header_repeat(row):
            vals = {str(v).strip().lower() for v in row if str(v).strip().lower() not in ("nan", "")}
            return len(vals & header_set) >= max(2, len(header_set) * 0.6)
        df = df[~df.apply(_is_header_repeat, axis=1)].reset_index(drop=True)

    df = df.dropna(how="all").dropna(axis=1, how="all")
    return df


def read_document_file(file_storage):
    """
    Read an uploaded file and return its full extracted text.

    Supported formats
    -----------------
    .txt          - read directly
    .pdf          - extract with pdfplumber (all pages)
    .docx / .doc  - extract paragraphs with python-docx
    .xlsx / .xls  - read ALL sheets; standardise data; apply LLM semantic
                    labelling for sheets with generic names (Sheet1, etc.)
    """
    if not file_storage or not file_storage.filename:
        return ""

    filename = secure_filename(file_storage.filename)
    ext = os.path.splitext(filename)[1].lower()

    tmp_path = os.path.join(
        UPLOAD_FOLDER,
        f"temp_{hashlib.md5(filename.encode()).hexdigest()}{ext}",
    )
    file_storage.save(tmp_path)

    try:
        print(f"Reading file: {filename} ({ext})")

        if ext == ".txt":
            with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            print(f"Extracted {len(text):,} characters from TXT")
            return text

        elif ext == ".pdf" and PDFPLUMBER_AVAILABLE:
            pages = []
            with pdfplumber.open(tmp_path) as pdf:
                print(f"Processing {len(pdf.pages)} PDF pages...")
                for page in pdf.pages:
                    pages.append(page.extract_text() or "")
            result = "\n".join(pages)
            print(f"Extracted {len(result):,} characters from PDF")
            return result

        elif ext in (".docx", ".doc") and DOCX_AVAILABLE:
            doc = Document(tmp_path)
            result = "\n".join(p.text for p in doc.paragraphs)
            print(f"Extracted {len(result):,} characters from Word")
            return result

        elif ext in (".xlsx", ".xls") and PANDAS_AVAILABLE:
            raw_sheets = pd.read_excel(tmp_path, sheet_name=None, header=None)
            processed: list = []

            for sheet_name, df_raw in raw_sheets.items():
                df = _standardise_excel_df(df_raw)
                if df.empty:
                    print(f"  Sheet '{sheet_name}': empty after cleaning - skipped")
                    continue

                processed.append({
                    "name":       sheet_name,
                    "columns":    [str(c) for c in df.columns],
                    "sample_csv": df.head(6).to_csv(index=False),
                    "csv_text":   df.to_csv(index=False),
                    "num_rows":   len(df),
                })
                print(f"  Sheet '{sheet_name}': {len(df)} rows, {len(df.columns)} columns")

            sheets_needing_labels = [s for s in processed if _is_generic_name(s["name"])]
            semantic_labels: dict = {}
            if sheets_needing_labels:
                print(
                    f"  Running LLM semantic labelling for "
                    f"{len(sheets_needing_labels)} generic sheet(s)..."
                )
                semantic_labels = _label_excel_sheets(sheets_needing_labels)
                for s in sheets_needing_labels:
                    print(f"    '{s['name']}' -> '{semantic_labels.get(s['name'], s['name'])}'")

            sheet_blocks: list = []
            total_rows = 0
            for s in processed:
                label = semantic_labels.get(s["name"], s["name"]) if _is_generic_name(s["name"]) else s["name"]
                sheet_blocks.append(f"=== SHEET: {label} ===\n{s['csv_text']}")
                total_rows += s["num_rows"]

            result = "\n\n".join(sheet_blocks)
            print(
                f"Extracted Excel: {len(processed)} sheet(s), "
                f"{total_rows} total rows, {len(result):,} characters"
            )
            return result

        else:
            print(f"Unsupported file type or missing library: {ext}")
            return ""

    except Exception as exc:
        print(f"Error reading file '{filename}': {exc}")
        return ""
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _resolve_document(slot: str):
    """
    Read one document slot from the request.

    Each slot accepts either an uploaded file or pasted text, plus a stage
    label. Returns (text, label, source_name).
    """
    uploaded = request.files.get(f"document_{slot}_file")
    pasted = (request.form.get(f"document_{slot}_text") or "").strip()
    label = (request.form.get(f"document_{slot}_label") or "").strip()

    if uploaded and uploaded.filename:
        text = read_document_file(uploaded)
        source = secure_filename(uploaded.filename)
    else:
        text = pasted
        source = "pasted text"

    return text, label, source


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the main web interface."""
    return render_template("index.html")


@app.route("/static/<path:filename>")
def serve_static(filename):
    """Serve static files (CSS, images)."""
    return send_from_directory(app.static_folder, filename)


@app.route("/api/health")
def health_check():
    """Health check endpoint."""
    template_path = resolve_template_path()
    return jsonify({
        "status": "running",
        "service": "ConflictScan / RFI Automation",
        "version": "3.0.0",
        "platform": "windows_compatible",
        "features": {
            "langgraph": True,
            "two_document_comparison": True,
            "document_type_agnostic": True,
            "discrepancy_detection": True,
            "rfi_drafting": True,
            "rfi_template_loaded": bool(template_path),
        },
    })


@app.route("/api/rfi-template", methods=["GET"])
def rfi_template_status():
    """Report which RFI form is in use and which tokens it contains."""
    path = resolve_template_path()
    if not path:
        return jsonify({
            "loaded": False,
            "message": (
                "No RFI form found. Drop the project's .docx RFI form into "
                "templates_rfi/ (placeholders written as {{token}}), or upload it here."
            ),
        })
    try:
        tokens = list_template_tokens(path)
    except Exception as exc:
        tokens = []
        print(f"[api] Could not read tokens from template: {exc}")

    return jsonify({
        "loaded": True,
        "filename": os.path.basename(path),
        "tokens": tokens,
    })


@app.route("/api/rfi-template", methods=["POST"])
def upload_rfi_template():
    """
    Upload the project's own .docx RFI form.

    It is saved into templates_rfi/ and used for every subsequent scan. The
    form's own layout is preserved - only {{token}} placeholders are replaced.
    """
    uploaded = request.files.get("template")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "No template file supplied."}), 400

    filename = secure_filename(uploaded.filename)
    if not filename.lower().endswith(".docx"):
        return jsonify({"error": "The RFI form must be a .docx file."}), 400

    path = os.path.join(RFI_TEMPLATE_FOLDER, filename)
    uploaded.save(path)

    try:
        tokens = list_template_tokens(path)
    except Exception as exc:
        return jsonify({"error": f"Uploaded file could not be read as a Word document: {exc}"}), 400

    print(f"[api] RFI form uploaded: {filename} ({len(tokens)} token(s))")
    return jsonify({"status": "success", "filename": filename, "tokens": tokens})


@app.route("/scan", methods=["POST"])
def scan():
    """
    Run a conflict scan over two documents from different project stages.

    Form fields
    -----------
    document_a_text / document_a_file  - one of the two is required
    document_a_label                   - e.g. "Tender BOQ"
    document_b_text / document_b_file
    document_b_label                   - e.g. "IFC Specifications"
    project_name, review_focus, user_id
    """
    try:
        print("\n" + "=" * 80)
        print("NEW CONFLICT SCAN REQUEST")
        print("=" * 80)

        project_name = (request.form.get("project_name") or "").strip()
        review_focus = (request.form.get("review_focus") or "").strip()
        user_id = (request.form.get("user_id") or "anonymous").strip()

        text_a, label_a, source_a = _resolve_document("a")
        text_b, label_b, source_b = _resolve_document("b")

        print(f"User: {user_id}")
        print(f"Project: {project_name or '(none)'}")
        print(f"A: '{label_a or 'unlabelled'}' from {source_a} - {len(text_a):,} chars")
        print(f"B: '{label_b or 'unlabelled'}' from {source_b} - {len(text_b):,} chars")

        missing = []
        if not text_a:
            missing.append("first")
        if not text_b:
            missing.append("second")
        if missing:
            return jsonify({
                "error": (
                    "Please provide the " + " and ".join(missing) +
                    " document, either as pasted text or an uploaded file."
                )
            }), 400

        result_state = run_conflict_scan(
            user_id=user_id,
            document_a_text=text_a,
            document_b_text=text_b,
            document_a_label=label_a or "Document A",
            document_b_label=label_b or "Document B",
            document_a_source=source_a,
            document_b_source=source_b,
            project_name=project_name or "Unnamed Project",
            review_focus=review_focus,
            export_format="excel",
        )

        if result_state.get("error_message"):
            return jsonify({"error": result_state["error_message"]}), 400

        discrepancies = result_state.get("discrepancies", [])
        rfi_items = result_state.get("rfi_items", [])
        profile_a = result_state.get("profile_a", {}) or {}
        profile_b = result_state.get("profile_b", {}) or {}

        event_id = str(uuid.uuid4())
        with session_lock:
            active_sessions[event_id] = {
                "user_id": user_id,
                "project_name": project_name,
                "document_a_label": result_state.get("document_a_label"),
                "document_b_label": result_state.get("document_b_label"),
                "discrepancies": discrepancies,
                "rfi_items": rfi_items,
                "timestamp": datetime.now().isoformat(),
            }

        register_path = result_state.get("register_path")

        response = {
            "event_id": event_id,
            "project_name": project_name,
            "document_a": {
                "label": result_state.get("document_a_label"),
                "source": profile_a.get("source_name", source_a),
                "char_count": profile_a.get("char_count", 0),
                "chunk_count": profile_a.get("chunk_count", 0),
                "requirement_count": len(profile_a.get("requirements", [])),
                "quantity_count": len(profile_a.get("quantities", [])),
                "standards": profile_a.get("standards", []),
            },
            "document_b": {
                "label": result_state.get("document_b_label"),
                "source": profile_b.get("source_name", source_b),
                "char_count": profile_b.get("char_count", 0),
                "chunk_count": profile_b.get("chunk_count", 0),
                "requirement_count": len(profile_b.get("requirements", [])),
                "quantity_count": len(profile_b.get("quantities", [])),
                "standards": profile_b.get("standards", []),
            },
            "scan_summary": result_state.get("scan_summary", ""),
            "confidence_level": result_state.get("confidence_level", "medium"),
            "warnings": result_state.get("analysis_warnings", []),
            "discrepancies": discrepancies,
            "rfi_items": [
                {**rfi, "docx_file": os.path.basename(rfi["docx_path"]) if rfi.get("docx_path") else None}
                for rfi in rfi_items
            ],
            "register_file": os.path.basename(register_path) if register_path else None,
            "steps_executed": result_state.get("step_history", []),
        }

        print(f"\nScan complete: {len(discrepancies)} discrepancy(ies), {len(rfi_items)} RFI(s)")
        print("=" * 80 + "\n")

        return jsonify(response)

    except Exception as exc:
        print("\n" + "=" * 80)
        print("ERROR DURING SCAN")
        print("=" * 80)
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500


@app.route("/api/rfi/export", methods=["POST"])
def export_edited_rfi():
    """
    Re-generate one RFI .docx from fields the engineer edited on screen.

    Body (JSON): {"event_id": "...", "rfi": {<edited RFI fields>}}

    The edited text is written into the project's RFI form and saved to
    exports/ under a "_edited" suffix, leaving the original draft intact.
    """
    payload = request.get_json(silent=True) or {}
    event_id = payload.get("event_id")
    rfi = payload.get("rfi") or {}

    if not rfi.get("rfi_no"):
        return jsonify({"error": "The RFI payload must include rfi_no."}), 400

    template_path = resolve_template_path()
    if not template_path:
        return jsonify({
            "error": "No RFI form is loaded. Upload the project's .docx form first."
        }), 400

    with session_lock:
        session = active_sessions.get(event_id, {})

    state_like = {
        "project_name": session.get("project_name") or payload.get("project_name") or "",
        "user_id": session.get("user_id") or "",
    }
    label_a = session.get("document_a_label") or payload.get("document_a_label") or "Document A"
    label_b = session.get("document_b_label") or payload.get("document_b_label") or "Document B"

    safe_no = secure_filename(str(rfi["rfi_no"])) or "RFI"
    out_path = os.path.join(EXPORT_FOLDER, f"{safe_no}_edited.docx")

    try:
        fill_docx_template(
            template_path,
            out_path,
            build_token_values(rfi, state_like, label_a, label_b),
        )
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": f"Could not write the RFI document: {exc}"}), 500

    print(f"[api] Edited RFI written: {os.path.basename(out_path)}")
    return jsonify({"status": "success", "file": os.path.basename(out_path)})


@app.route("/exports/<path:filename>")
def serve_export(filename):
    """Serve generated registers and RFI documents."""
    return send_from_directory(EXPORT_FOLDER, filename, as_attachment=True)


if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("STARTING CONFLICTSCAN / RFI AUTOMATION")
    print("=" * 80)
    print("Web Interface: http://localhost:5000")
    print("API Health:    http://localhost:5000/api/health")
    template = resolve_template_path()
    print(f"RFI form:      {os.path.basename(template) if template else 'none loaded (templates_rfi/)'}")
    print("=" * 80 + "\n")

    app.run(host="0.0.0.0", port=5000, debug=True)
