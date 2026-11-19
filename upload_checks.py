"""
Upload safety checks
====================
Two levels, so a bad or hostile file is stopped before the pipeline reads it:

sniff(path, ext)    - fast, runs in the upload request. Reads only the first
                      few KB and checks the content really is the type its
                      extension claims (a renamed .exe is refused).
inspect(path, ext)  - deeper, runs off the request thread (jobs.inspect_*):
                      page count, encrypted PDFs, zip bombs inside Office
                      files, oversized images, plus the document/drawing
                      guess shown in the UI. Problems are stored on the file
                      and block the scan until the file is removed.

Limits (environment variables):
    CONFLICTSCAN_MAX_FILE_MB     largest single file           (default 200)
    CONFLICTSCAN_MAX_PDF_PAGES   most pages in one PDF         (default 400)
    CONFLICTSCAN_MAX_IMAGE_MP    largest image, in megapixels  (default 200)

Windows compatible.
"""

import os
import zipfile
from typing import Any, Dict, Optional

MAX_FILE_MB = int(os.environ.get("CONFLICTSCAN_MAX_FILE_MB", "200"))
MAX_PDF_PAGES = int(os.environ.get("CONFLICTSCAN_MAX_PDF_PAGES", "400"))
MAX_IMAGE_MP = int(os.environ.get("CONFLICTSCAN_MAX_IMAGE_MP", "200"))
MAX_UNZIPPED_MB = 1024           # an .xlsx/.docx that inflates past this is treated as a zip bomb
MAX_ZIP_RATIO = 200

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"      # legacy .doc / .xls
_ZIP = (b"PK\x03\x04",)
_SIGNATURES = {
    ".pdf": None,                                 # checked separately (header may follow junk bytes)
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
    ".bmp": (b"BM",),
    ".docx": _ZIP,
    ".xlsx": _ZIP,
    ".xlsm": _ZIP,
    ".doc": (_OLE,),
    ".xls": (_OLE,),
}
_TEXT_TYPES = {".txt", ".csv", ".dxf"}
_NAMES = {".pdf": "PDF", ".png": "PNG image", ".jpg": "JPEG image", ".jpeg": "JPEG image",
          ".tif": "TIFF image", ".tiff": "TIFF image", ".bmp": "BMP image", ".docx": "Word document",
          ".doc": "Word 97-2003 document", ".xlsx": "Excel workbook", ".xlsm": "Excel workbook",
          ".xls": "Excel 97-2003 workbook", ".txt": "text file", ".csv": "CSV file", ".dxf": "DXF drawing"}


def max_file_bytes() -> int:
    return MAX_FILE_MB * 1024 * 1024


def sniff(path: str, ext: str) -> Optional[str]:
    """None when the file's first bytes match its extension, else a message for the user."""
    ext = ext.lower()
    size = os.path.getsize(path)
    if size == 0:
        return "is empty"
    if size > max_file_bytes():
        return f"is {size / 1048576:.0f} MB; the limit per file is {MAX_FILE_MB} MB"
    with open(path, "rb") as f:
        head = f.read(8192)
    what = _NAMES.get(ext, ext)
    if ext == ".pdf":
        return None if b"%PDF-" in head[:1024] else f"is not a real {what}"
    if ext in _TEXT_TYPES:
        if b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):   # allow UTF-16 text
            return f"is not a plain-text {what}"
        if ext == ".dxf" and b"SECTION" not in head.upper() and not head.startswith(b"AutoCAD Binary DXF"):
            return f"is not a {what}"
        return None
    signatures = _SIGNATURES.get(ext)
    if signatures and not head.startswith(signatures):
        return f"is not a real {what} (its content does not match the .{ext.lstrip('.')} extension)"
    return None


def _inspect_pdf(path: str) -> Dict[str, Any]:
    import fitz  # PyMuPDF
    with fitz.open(path) as doc:
        if doc.needs_pass:
            return {"problem": "is password-protected - remove the password and upload it again"}
        pages = doc.page_count
    if pages > MAX_PDF_PAGES:
        return {"pages": pages, "problem": f"has {pages} pages; the limit is {MAX_PDF_PAGES} - split it first"}
    return {"pages": pages}


def _inspect_zip(path: str) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        return {"problem": "is damaged and cannot be opened"}
    packed = max(1, sum(i.compress_size for i in infos))
    unpacked = sum(i.file_size for i in infos)
    if unpacked > MAX_UNZIPPED_MB * 1048576 or unpacked / packed > MAX_ZIP_RATIO:
        return {"problem": "expands to an unsafe size when opened and was not read"}
    return {}


def _inspect_image(path: str) -> Dict[str, Any]:
    from PIL import Image
    with Image.open(path) as img:
        w, h = img.size
        frames = getattr(img, "n_frames", 1)
    if w * h > MAX_IMAGE_MP * 1_000_000:
        return {"problem": f"is {w}x{h} pixels; the limit is {MAX_IMAGE_MP} megapixels"}
    return {"pages": frames}


def inspect(path: str, ext: str) -> Dict[str, Any]:
    """{pages, problem, detected_kind, detected_reason} - deeper checks plus the document/drawing guess."""
    from nodes.file_ingest import classify_file

    ext = ext.lower()
    out: Dict[str, Any] = {"pages": None, "problem": None}
    try:
        if ext == ".pdf":
            out.update(_inspect_pdf(path))
        elif ext in (".docx", ".xlsx", ".xlsm"):
            out.update(_inspect_zip(path))
        elif ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
            out.update(_inspect_image(path))
    except Exception as exc:  # noqa: BLE001
        out["problem"] = f"could not be opened ({exc})"
    if out.get("problem"):
        out["detected_kind"], out["detected_reason"] = None, None
        return out
    try:
        out["detected_kind"], out["detected_reason"] = classify_file(path, use_vision=False)
    except Exception as exc:  # noqa: BLE001
        out["detected_kind"], out["detected_reason"] = None, f"could not inspect ({exc})"
    return out
