"""
Reading a folder of past RFIs (the "RFI training data" layout)
==============================================================
    <root>/<project>/...                    one folder per project
        ... *log*.xlsx                      the project's RFI log (optional)
        ... <RFI folder>/                   one folder per RFI (name contains "RFI")
                .../Attachment*/...         the documents the RFI cites
                .../Reply|In/*.pdf          the client's response (optional)
                RFI form .docx / .xlsx      subject + request (when there is no log)

load_rfis(root)  -> one record per RFI: project, number, subject, request,
                    discipline, reply text, attachment files
Used by `manage.py index-rfis` (the RFI memory) and the benchmark (bench/).

Windows compatible.
"""

import os
import re
import sys
import warnings
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tools"))

USABLE_EXT = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".xlsm", ".csv", ".txt", ".dxf", ".png", ".jpg", ".jpeg",
              ".tif", ".tiff", ".bmp"}


def _key(number: str) -> str:
    """'TRC02-RFI-RME-EHAF-STR-0003 Rev.01' and folder 'TRC02-RFI-RME-EHAF-0003' -> '0003'."""
    m = re.findall(r"(\d{3,5})", number or "")
    return m[-1].lstrip("0") if m else (number or "").strip().lower()


def _pdf_text(path: str, limit: int = 3000) -> str:
    try:
        import pymupdf
        out = []
        with pymupdf.open(path) as doc:
            for page in doc:
                out.append(page.get_text())
                if sum(len(t) for t in out) > limit:
                    break
        return re.sub(r"\s+", " ", " ".join(out)).strip()[:limit]
    except Exception:  # noqa: BLE001
        return ""


def _reply_text(folder: str) -> str:
    for dp, _dirs, fs in os.walk(folder):
        base = os.path.basename(dp).lower()
        if base in ("reply", "in", "response", "responses"):
            for f in sorted(fs):
                if f.lower().endswith(".pdf"):
                    text = _pdf_text(os.path.join(dp, f))
                    if text:
                        return text
    return ""


def _attachments(folder: str) -> List[str]:
    out = []
    for dp, _dirs, fs in os.walk(folder):
        if "attach" not in dp.lower():
            continue
        for f in sorted(fs):
            if not f.startswith("~$") and os.path.splitext(f)[1].lower() in USABLE_EXT:
                out.append(os.path.join(dp, f))
    return out


_RFI_FOLDER = re.compile(r"rfi.*\d{3,}", re.I)


def find_rfi_folders(root: str) -> List[str]:
    """Folders named like one RFI (an RFI number with 3+ digits), not nested inside another one."""
    found = []
    for dp, dirs, _files in os.walk(root):
        if dp != root and _RFI_FOLDER.search(os.path.basename(dp)):
            found.append(dp)
            dirs[:] = []
    return sorted(found)


def load_rfis(root: str, with_replies: bool = True) -> List[Dict[str, Any]]:
    from analyze_rfi_log import read_log
    import analyze_rfi_log
    analyze_rfi_log.find_rfi_folders = find_rfi_folders      # read_forms walks the same folders
    read_forms = analyze_rfi_log.read_forms
    warnings.filterwarnings("ignore")
    records: List[Dict[str, Any]] = []
    for project in sorted(os.listdir(root)):
        pdir = os.path.join(root, project)
        if not os.path.isdir(pdir):
            continue
        project_name = re.sub(r"^\d+\s*|request for info-?rfi\s*-?\s*|rfi data\s*-?\s*", "", project, flags=re.I).strip() or project
        log_entries = []
        for dp, _dirs, fs in os.walk(pdir):
            for f in fs:
                if f.lower().endswith((".xlsx", ".xls")) and "log" in f.lower() and not f.startswith("~$"):
                    try:
                        log_entries += read_log(os.path.join(dp, f))
                    except Exception:  # noqa: BLE001
                        pass
        by_key = {}
        for e in log_entries:
            by_key.setdefault(_key(e["number"]), e)
        forms = {_key(e["number"]): e for e in read_forms(pdir)}
        folders = {_key(os.path.basename(f)): f for f in find_rfi_folders(pdir)}
        for key in sorted(set(by_key) | set(forms) | set(folders), key=lambda k: (len(k), k)):
            e = by_key.get(key) or forms.get(key)
            if not e:
                continue
            folder = folders.get(key)
            records.append({
                "project_name": project_name,
                "rfi_no": e.get("number") or (os.path.basename(folder) if folder else key),
                "subject": (e.get("subject") or "").strip(),
                "text": (e.get("description") or "").strip(),
                "discipline": (e.get("discipline") or "").strip() or None,
                "response": _reply_text(folder) if (with_replies and folder) else None,
                "attachments": _attachments(folder) if folder else [],
                "folder": folder,
            })
    return records
