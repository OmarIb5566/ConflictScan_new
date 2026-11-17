"""
File ingestion - ConflictScan / RFI Automation
==============================================
Every uploaded file passes through here. Each side of a comparison is a SET
of files (real RFIs cite a median of two and up to nineteen attachments), so
this module answers two questions per file:

1. classify_file()  - is it a DOCUMENT (read as text: BOQ, specification,
                      schedule, letter, report) or a DRAWING (read visually:
                      plans, sections, single-line/riser diagrams, sketches)?
2. read_document()  - the full text of a document file, page-marked so
                      references can cite "p. N". Pages without a text layer
                      (scans - the large majority of real attachments) are
                      transcribed by the OCR model instead of being lost.

Drawings are not read here - nodes/drawing_analysis.py renders and reads them.
"""

import json
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".xlsm", ".csv", ".txt"}
DRAWING_EXTENSIONS = {".pdf", ".dxf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

DWG_HINT = (
    "DWG files aren't supported directly - please convert to DXF first "
    "(e.g. AutoCAD 'Save As' -> DXF, or a free DWG-to-DXF converter) and "
    "upload the .dxf file."
)

# A page with fewer extractable characters than this is treated as a scan.
MIN_TEXT_CHARS_PER_PAGE = 40
# OCR is slow on a local model - bound it per file and say so when the cap bites.
MAX_OCR_PAGES_PER_FILE = int(os.environ.get("CONFLICTSCAN_MAX_OCR_PAGES", "30"))
OCR_RENDER_LONG_PX = 1600

# ISO A4 long side is 842 pt; anything clearly larger is a drawing sheet.
A4_LONG_SIDE_PT = 842
DRAWING_VECTOR_PATHS = 400        # vector paths on a small page that make it a sketch, when text is light
DENSE_DRAWING_VECTOR_PATHS = 2000  # this many paths is a drawing whatever text it also carries


def decode_symbol_font(text: str) -> str:
    """
    Some CAD exports write text with a symbol-font encoding that maps ASCII
    into the U+F020-U+F0FF private-use block ('\\uf053\\uf048\\uf04f\\uf050' is
    'SHOP'). Shift those characters back so the text layer is readable.
    """
    if not text or not any(0xF020 <= ord(c) <= 0xF0FF for c in text):
        return text
    return "".join(chr(ord(c) - 0xF000) if 0xF020 <= ord(c) <= 0xF0FF else c for c in text)


_PAGE_KIND_SYSTEM = (
    "You look at one scanned page from a construction project and classify what MOST of the page is.\n"
    "- \"table\": rows and columns of text - a BOQ, list of queries, schedule, register or log, even with "
    "grid lines, logos, stamps or signatures.\n"
    "- \"text\": paragraphs - a letter, report, specification, form or calculation sheet.\n"
    "- \"drawing\": linework that depicts something physical or a system - a plan, section, elevation, "
    "detail, single-line or riser diagram, hand sketch, or a screenshot of a drawing or 3D model.\n"
    "Return JSON: {\"kind\": \"table\" | \"text\" | \"drawing\"}."
)


def _vision_page_kind(page) -> Optional[str]:
    """One vision call deciding whether a scanned page is a drawing or a text document; None if unavailable."""
    import tempfile
    import pymupdf
    try:
        from nodes.vision_client import vision_llm_client
        zoom = 1000 / max(page.rect.width, page.rect.height, 1.0)
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(tmp)
            result = vision_llm_client.run(system=_PAGE_KIND_SYSTEM, user="What is this page?",
                                           images=[tmp], temperature=0.0, max_retries=1,
                                           model=vision_llm_client.page_kind_model())
        finally:
            os.unlink(tmp)
        kind = str(result.get("kind") or "").strip().lower()
        if kind in ("table", "text", "document"):
            return "document"
        return "drawing" if kind == "drawing" else None
    except Exception as exc:
        print(f"[file_ingest] page-kind check unavailable ({str(exc)[:120]})")
        return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_file(path: str, use_vision: bool = True) -> Tuple[str, str]:
    """
    ("document" | "drawing", reason). Never raises - an unreadable file falls
    back on its extension and the reason says so.

    PDFs are decided by their pages: a sheet larger than A4, or a small page
    dense with vector linework, is a drawing; a page with running text is a
    document. A scanned small page is ambiguous - a scanned BOQ or letter, or
    a screenshot/sketch of a drawing - so one vision call looks at it. The
    user can always override the decision in the UI.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dxf":
        return "drawing", "CAD file"
    if ext in IMAGE_EXTENSIONS:
        return "drawing", "image - read visually as a sketch/sheet"
    if ext != ".pdf":
        return "document", f"{ext} file"

    try:
        import pymupdf
        with pymupdf.open(path) as doc:
            pages = list(doc)[:4]
            if not pages:
                return "document", "empty PDF"
            large = sum(1 for p in pages if max(p.rect.width, p.rect.height) > A4_LONG_SIDE_PT * 1.15)
            if large:
                return "drawing", f"{large} of the first {len(pages)} page(s) larger than A4"
            text_chars = [len(p.get_text().strip()) for p in pages]
            vector = []
            for p in pages:
                try:
                    vector.append(len(p.get_drawings()))
                except Exception:
                    vector.append(0)
            if max(vector) >= DENSE_DRAWING_VECTOR_PATHS:
                return "drawing", f"vector drawing ({max(vector):,} paths on one page)"
            if max(vector) >= DRAWING_VECTOR_PATHS and min(text_chars) < 1500:
                return "drawing", "A4 vector sketch (mostly linework)"
            if max(text_chars) < MIN_TEXT_CHARS_PER_PAGE:
                kind = _vision_page_kind(pages[0]) if use_vision else None
                if kind == "drawing":
                    return "drawing", "scanned page showing a drawing / sketch / screenshot"
                return "document", "scanned A4 pages - will be OCR-read"
            return "document", "A4 pages with a text layer"
    except Exception as exc:
        return "document", f"could not inspect PDF ({exc}) - treated as a document"


# ---------------------------------------------------------------------------
# Excel helpers (sheet clean-up + semantic labelling of generic sheet names)
# ---------------------------------------------------------------------------

_GENERIC_SHEET_NAMES = frozenset([
    "sheet1", "sheet2", "sheet3", "sheet4", "sheet5",
    "sheet6", "sheet7", "sheet8", "sheet9", "sheet10",
    "data", "table", "tab", "tab1", "tab2", "tab3",
    "untitled", "new sheet", "sheet", "worksheet",
])


def _is_generic_name(name: str) -> bool:
    return name.strip().lower() in _GENERIC_SHEET_NAMES


def _label_excel_sheets(sheets_info: list) -> dict:
    """ONE LLM call assigning semantic labels to generically-named sheets. {} on failure."""
    if not sheets_info:
        return {}
    try:
        from nodes.llm_client import llm_client

        summary = [
            {"sheet_name": s["name"], "column_headers": s["columns"][:25], "sample_data": s["sample_csv"][:600]}
            for s in sheets_info
        ]
        result = llm_client.run(
            system=(
                "You are a construction project document analyst. You analyse Excel spreadsheet content "
                "from construction projects to identify what each sheet contains and assign a clear label."
            ),
            user=(
                "Analyse these Excel sheets from a construction project document and assign a concise, "
                "descriptive label (max 60 chars) to each.\n\n"
                + json.dumps(summary, indent=2, ensure_ascii=False)
                + "\n\nReturn JSON:\n"
                '{"labels": {"<sheet_name>": "<semantic label>", ...}}\n\n'
                "Label examples: \"Bill of Quantities (BOQ)\", \"Technical Specifications\", "
                "\"Unit Rate Schedule\", \"Material Submittal List\", \"Panel Schedule\", "
                "\"Equipment Schedule\", \"RFI Log\". Use the most specific label that fits."
            ),
            temperature=0.1,
        )
        return result.get("labels", {})
    except Exception as exc:
        print(f"[file_ingest] Excel sheet LLM labelling failed: {exc}")
        return {}


def _standardise_excel_df(df):
    """
    Clean a raw sheet: drop empty rows/columns, forward-fill merged cells,
    promote the most text-like of the first rows to header, drop repeated
    header rows.
    """
    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        return df

    df = df.ffill(axis=0)

    best_row_idx, best_score = 0, -1
    for row_idx in range(min(5, len(df))):
        row = df.iloc[row_idx]
        score = int(row.apply(lambda v: isinstance(v, str) and len(str(v).strip()) > 1).sum())
        if score > best_score:
            best_score, best_row_idx = score, row_idx

    if best_score >= max(1, len(df.columns) * 0.3):
        seen: dict = {}
        clean_cols = []
        for h in df.iloc[best_row_idx].tolist():
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

    return df.dropna(how="all").dropna(axis=1, how="all")


def _read_excel(path: str) -> str:
    import pandas as pd

    raw_sheets = pd.read_excel(path, sheet_name=None, header=None)
    processed: list = []
    for sheet_name, df_raw in raw_sheets.items():
        df = _standardise_excel_df(df_raw)
        if df.empty:
            continue
        processed.append({
            "name": sheet_name,
            "columns": [str(c) for c in df.columns],
            "sample_csv": df.head(6).to_csv(index=False),
            "csv_text": df.to_csv(index=False),
        })
    labels = _label_excel_sheets([s for s in processed if _is_generic_name(s["name"])])
    blocks = []
    for s in processed:
        label = labels.get(s["name"], s["name"]) if _is_generic_name(s["name"]) else s["name"]
        blocks.append(f"=== SHEET: {label} ===\n{s['csv_text']}")
    return "\n\n".join(blocks)


def _read_docx(path: str) -> str:
    """Paragraphs AND tables, in document order - BOQs and schedules in Word live in tables."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(path)
    parts: List[str] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, doc).text.strip()
            if text:
                parts.append(text)
        elif tag == "tbl":
            for row in Table(child, doc).rows:
                cells, last = [], None
                for cell in row.cells:
                    value = cell.text.strip().replace("\n", " ")
                    if value != last:          # merged cells repeat their text
                        cells.append(value)
                    last = value
                if any(cells):
                    parts.append(" | ".join(cells))
            parts.append("")
    return "\n".join(parts)


def _read_pdf(path: str, ocr: bool, progress: Optional[Callable[[str], None]], render_dir: str) -> Dict[str, Any]:
    import pymupdf

    pages_text: List[str] = []
    ocr_pages: List[int] = []
    skipped_ocr: List[int] = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = decode_symbol_font(page.get_text() or "").strip()
            if len(text) < MIN_TEXT_CHARS_PER_PAGE and ocr:
                if len(ocr_pages) >= MAX_OCR_PAGES_PER_FILE:
                    skipped_ocr.append(i)
                else:
                    if progress:
                        progress(f"OCR page {i}/{len(doc)} of {os.path.basename(path)}")
                    text = _ocr_page(page, i, render_dir) or text
                    ocr_pages.append(i)
            pages_text.append(f"[Page {i}]\n{text}")
    return {"text": "\n\n".join(pages_text), "pages": len(pages_text), "ocr_pages": ocr_pages, "skipped_ocr": skipped_ocr}


def _ocr_page(page, page_no: int, render_dir: str) -> str:
    from nodes.vision_client import vision_llm_client

    os.makedirs(render_dir, exist_ok=True)
    long_pt = max(page.rect.width, page.rect.height, 1.0)
    zoom = OCR_RENDER_LONG_PX / long_pt
    import pymupdf
    img_path = os.path.join(render_dir, f"ocr_p{page_no}.png")
    page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(img_path)
    try:
        return vision_llm_client.transcribe(img_path)
    finally:
        try:
            os.unlink(img_path)
        except OSError:
            pass


def read_document(path: str, ocr: bool = True, progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """
    Full text of a document file.

    Returns {"text", "pages", "ocr_pages": [page numbers OCR-read], "warnings": [...]}.
    PDF text is page-marked ("[Page N]") so the extraction step can cite pages.
    """
    name = os.path.basename(path)
    ext = os.path.splitext(path)[1].lower()
    warnings: List[str] = []
    result = {"text": "", "pages": 0, "ocr_pages": [], "warnings": warnings}
    render_dir = os.path.join(os.path.dirname(path), "_ocr_" + re.sub(r"\W+", "_", name))

    try:
        if ext == ".txt":
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                result["text"] = f.read()
        elif ext == ".csv":
            with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
                result["text"] = f.read()
        elif ext in (".docx", ".doc"):
            result["text"] = _read_docx(path)
        elif ext in (".xlsx", ".xls", ".xlsm"):
            result["text"] = _read_excel(path)
        elif ext == ".pdf":
            pdf = _read_pdf(path, ocr, progress, render_dir)
            result.update({k: pdf[k] for k in ("text", "pages", "ocr_pages")})
            if pdf["ocr_pages"]:
                warnings.append(
                    f"'{name}': {len(pdf['ocr_pages'])} scanned page(s) were read by OCR - transcription of "
                    f"scans (especially Arabic text) can contain errors; verify quoted values against the original."
                )
            if pdf["skipped_ocr"]:
                warnings.append(
                    f"'{name}': {len(pdf['skipped_ocr'])} further scanned page(s) were not OCR-read "
                    f"(limit {MAX_OCR_PAGES_PER_FILE} per file; set CONFLICTSCAN_MAX_OCR_PAGES to raise it)."
                )
        else:
            warnings.append(f"'{name}': unsupported file type {ext} - skipped.")
    except Exception as exc:
        warnings.append(f"'{name}' could not be read: {exc}")
    finally:
        if os.path.isdir(render_dir):
            import shutil
            shutil.rmtree(render_dir, ignore_errors=True)

    body = re.sub(r"\[Page \d+\]\s*", "", result["text"]).strip()
    if not body:
        warnings.append(f"'{name}' contained no readable text.")
    print(f"[file_ingest] {name}: {len(result['text']):,} chars, {result['pages']} page(s), "
          f"{len(result['ocr_pages'])} OCR page(s)")
    return result


# ---------------------------------------------------------------------------
# Document sets
# ---------------------------------------------------------------------------

def pair_drawing_files(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Fold each DXF into the drawing PDF/image with the same file stem, so one
    sheet read from both formats is profiled once (DXF for counts and
    measurements, PDF for the visual read). An unpaired DXF stays a drawing
    of its own.
    """
    drawings = [f for f in files if f.get("kind") == "drawing"]
    others = [f for f in files if f.get("kind") != "drawing"]
    stem = lambda f: os.path.splitext(f["name"])[0].strip().lower()
    dxfs = {stem(f): f for f in drawings if f["path"].lower().endswith(".dxf")}
    visual = [f for f in drawings if not f["path"].lower().endswith(".dxf")]

    paired: List[Dict[str, Any]] = []
    used = set()
    for f in visual:
        match = dxfs.get(stem(f))
        if match:
            used.add(stem(f))
            paired.append({**f, "dxf_path": match["path"], "name": f"{f['name']} + {match['name']}"})
        else:
            paired.append(f)
    for key, f in dxfs.items():
        if key not in used:
            paired.append({**f, "dxf_path": f["path"], "path": None})
    return others + paired
