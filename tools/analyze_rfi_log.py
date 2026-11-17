"""
Analyse a project's RFI log (and optionally its RFI folders) against the
issue taxonomy in knowledge/rfi_taxonomy.json.

    python tools/analyze_rfi_log.py "<path to RFI log .xlsx>" [--rfi-folder "<folder of RFI sub-folders>"]
                                    [--out knowledge/corpus_stats/<name>.json] [--show-unclassified]

What it reports
---------------
* how often each issue class occurs (keyword classification, multi-label)
* discipline mix
* attachments per RFI - parsed from the log's attachment column
  ("03 Pages A4 & 02 Sketches A3") and, with --rfi-folder, counted from the
  actual attachment files, including how many pages carry a text layer

The log layout is detected, not assumed: the header row is the first row that
contains both a subject-like and a description-like column. Any project whose
log has those two columns can be analysed, which is how the taxonomy is meant
to grow - one more project at a time, never tuned to a single one.
"""

import argparse
import json
import os
import re
import statistics
import sys
import warnings
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nodes.rfi_knowledge import classify_text, issue_classes  # noqa: E402

HEADER_HINTS = {
    "subject": re.compile(r"subject|title", re.I),
    "description": re.compile(r"description|details|query|question", re.I),
    "discipline": re.compile(r"discipline|trade", re.I),
    "number": re.compile(r"(rfi|submittal).*no|ref", re.I),
    "attach": re.compile(r"attach|enclos", re.I),
    "area": re.compile(r"block|area|location|zone", re.I),
    "revision": re.compile(r"rev", re.I),
}

DISCIPLINE_FAMILIES = {
    "elec": "electrical", "mech": "mechanical", "arch": "architectural",
    "civil": "civil_structural", "str": "civil_structural", "general": "general",
}


def _find_header(rows):
    for i, row in enumerate(rows[:40]):
        cells = [str(c or "").strip() for c in row]
        cols = {}
        for j, c in enumerate(cells):
            for key, pat in HEADER_HINTS.items():
                if key not in cols and c and pat.search(c):
                    cols[key] = j
        if "subject" in cols and "description" in cols:
            return i, cols
    return None, {}


def parse_attachment_text(value: str) -> dict:
    """'01 Page A4 & 02 Sketches A3' -> {'pages': 1, 'sketches': 2, 'total': 3}."""
    text = str(value or "").lower()
    if not text or re.search(r"no\s*attach", text):
        return {"pages": 0, "sketches": 0, "total": 0}
    pages = sum(int(n) for n in re.findall(r"(\d+)\s*pa?ge", text))
    sketches = sum(int(n) for n in re.findall(r"(\d+)\s*s\w*tch", text))
    files = sum(int(n) for n in re.findall(r"(\d+)\s*files?\b", text))
    return {"pages": pages, "sketches": sketches, "total": pages + sketches + files}


def read_log(path: str):
    import openpyxl
    warnings.filterwarnings("ignore")
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    entries = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        h, cols = _find_header(rows)
        if h is None:
            continue
        for row in rows[h + 1:]:
            get = lambda k: (str(row[cols[k]]).strip() if k in cols and cols[k] < len(row) and row[cols[k]] is not None else "")
            subject, description = get("subject"), get("description")
            if not (subject or description):
                continue
            entries.append({
                "number": get("number"), "revision": get("revision"), "discipline": get("discipline"),
                "subject": subject, "description": description, "area": get("area"), "attach": get("attach"),
            })
    return entries


def find_rfi_folders(root: str) -> list:
    """Every folder named like an RFI (at any depth), not nested inside another one."""
    found = []
    for dp, dirs, _files in os.walk(root):
        if re.search(r"rfi", os.path.basename(dp), re.I) and dp != root and \
                not any(dp.startswith(f + os.sep) for f in found):
            found.append(dp)
            dirs[:] = []          # an RFI's own sub-folders are not RFIs
    return sorted(found)


def _attachment_files(folder: str) -> list:
    files = []
    for dp, _dirs, fs in os.walk(folder):
        if "attach" in dp.lower():
            files += [os.path.join(dp, f) for f in fs if not f.startswith("~$")]
    return files


def _form_text(path: str) -> str:
    if path.lower().endswith(".docx"):
        import docx
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for t in d.tables:
            for row in t.rows:
                cells = list(dict.fromkeys(c.text.strip() for c in row.cells if c.text.strip()))
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    parts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = list(dict.fromkeys(str(c).strip() for c in row if c not in (None, "") and str(c).strip()))
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


_FORM_END = r"(?:CONTRACTOR.S (?:REPRESENTATIVE|SIGNATURE)|PART 2|consultant RESPONSE|Attachment\s*:|$)"
# Most specific heading first - "REQUEST FOR INFORMATION" is the form's title, not its body.
_FORM_REQUEST_RES = [
    re.compile(r"INFORMATION REQUESTED\s*:?(.*?)" + _FORM_END, re.I | re.S),
    re.compile(r"(?m)^\s*(?:REQUEST|DESCRIPTION|QUERY)\s*:(.*?)" + _FORM_END, re.I | re.S),
]


def read_forms(root: str) -> list:
    """
    One entry per RFI folder, read from its own RFI form (.docx / .xlsx outside
    the attachment folders) - for projects that keep no central RFI log.
    """
    warnings.filterwarnings("ignore")
    entries = []
    for folder in find_rfi_folders(root):
        forms = []
        for dp, _dirs, fs in os.walk(folder):
            if "attach" in dp.lower():
                continue
            forms += [os.path.join(dp, f) for f in fs
                      if f.lower().endswith((".docx", ".xlsx")) and not f.startswith("~$")]
        text = ""
        for form in sorted(forms, key=lambda p: (not p.lower().endswith(".docx"), len(p))):
            try:
                text = _form_text(form)
            except Exception:
                continue
            if len(text) > 100:
                break
        subject = re.search(r"SUBJECT\s*:?\s*\|?\s*(.+)", text, re.I)
        discipline = re.search(r"DISCIPLINE\s*:?\s*\|?\s*(.+)", text, re.I)
        request = next((m for m in (r.search(text) for r in _FORM_REQUEST_RES) if m), None)
        if not subject and not request:
            continue                                  # a reply or a stray file, not an RFI form
        rel = os.path.relpath(folder, root).split(os.sep)
        entries.append({
            "number": os.path.basename(folder),
            "revision": "",
            "discipline": (discipline.group(1).strip() if discipline else rel[0]),
            "subject": subject.group(1).strip() if subject else "",
            "description": (request.group(1).strip() if request else text)[:4000],
            "area": "",
            "attach": f"{len(_attachment_files(folder))} files",
        })
    return entries


def scan_folders(root: str) -> dict:
    """Attachment files and text-layer coverage per RFI folder (found at any depth)."""
    try:
        import pymupdf
    except ImportError:
        pymupdf = None
    per_rfi, pages, text_pages = [], 0, 0
    for folder in find_rfi_folders(root):
        files = []
        for dp, _dirs, fs in os.walk(folder):
            if "attach" not in dp.lower():
                continue
            files += [os.path.join(dp, f) for f in fs if not f.startswith("~$")]
        per_rfi.append(len(files))
        if pymupdf:
            for f in files:
                if not f.lower().endswith(".pdf"):
                    continue
                try:
                    with pymupdf.open(f) as doc:
                        for pg in doc:
                            pages += 1
                            text_pages += len(pg.get_text().strip()) > 50
                except Exception:
                    pass
    if not per_rfi:
        return {}
    return {
        "rfi_folders": len(per_rfi),
        "median_files": statistics.median(per_rfi),
        "max_files": max(per_rfi),
        "share_with_more_than_one_file": round(sum(n > 1 for n in per_rfi) / len(per_rfi), 2),
        "attachment_pdf_pages": pages,
        "share_pages_with_text_layer": round(text_pages / pages, 2) if pages else None,
    }


def analyse(entries):
    class_counts, discipline_counts, unclassified = Counter(), Counter(), []
    attach_totals = []
    for e in entries:
        text = f"{e['subject']}\n{e['description']}"
        ranked = classify_text(text, top=4)
        e["issue_classes"] = [k for k, _hits in ranked]
        class_counts.update(e["issue_classes"])
        if not ranked:
            unclassified.append(e)
        for part in re.split(r"[&/,]", e["discipline"].lower()):
            for prefix, family in DISCIPLINE_FAMILIES.items():
                if part.strip().startswith(prefix):
                    discipline_counts[family] += 1
                    break
        attach_totals.append(parse_attachment_text(e["attach"])["total"])
    n = len(entries) or 1
    return {
        "rfi_count": len(entries),
        "issue_class_share": {k: round(class_counts[k] / n, 2) for k, _ in class_counts.most_common()},
        "discipline_share": {k: round(v / n, 2) for k, v in discipline_counts.most_common()},
        "logged_attachments": {
            "median": statistics.median(attach_totals) if attach_totals else 0,
            "max": max(attach_totals) if attach_totals else 0,
            "share_with_more_than_one": round(sum(t > 1 for t in attach_totals) / n, 2),
        },
        "unclassified_count": len(unclassified),
    }, unclassified


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", nargs="?", help="RFI log workbook (.xlsx); omit with --forms")
    ap.add_argument("--rfi-folder", help="folder holding the RFI folders (attachments counted from */Attachment*/, any depth)")
    ap.add_argument("--forms", action="store_true",
                    help="no log: read each RFI's own form (.docx/.xlsx) found under --rfi-folder")
    ap.add_argument("--out", help="write the statistics to this JSON file")
    ap.add_argument("--show-unclassified", action="store_true")
    args = ap.parse_args()

    if args.forms:
        if not args.rfi_folder:
            sys.exit("--forms needs --rfi-folder")
        entries = read_forms(args.rfi_folder)
    elif args.log:
        entries = read_log(args.log)
    else:
        sys.exit("Give an RFI log workbook, or --forms with --rfi-folder.")
    if not entries:
        sys.exit("No RFIs found - a log needs Subject and Description columns; a form needs a SUBJECT line.")
    stats, unclassified = analyse(entries)
    if args.rfi_folder:
        stats["attachment_files"] = scan_folders(args.rfi_folder)

    titles = {c["key"]: c["title"] for c in issue_classes(detectable_only=False)}
    print(f"\nRFIs analysed: {stats['rfi_count']}")
    print("\nIssue classes (an RFI may carry several):")
    for key, share in stats["issue_class_share"].items():
        print(f"  {share:5.0%}  {key:26s} {titles.get(key, '')}")
    print(f"  unclassified: {stats['unclassified_count']}")
    print("\nDisciplines:", ", ".join(f"{k} {v:.0%}" for k, v in stats["discipline_share"].items()))
    print("Logged attachments:", stats["logged_attachments"])
    if stats.get("attachment_files"):
        print("Attachment files:", stats["attachment_files"])
    if args.show_unclassified:
        for e in unclassified:
            print(f"\n- {e['number']} {e['subject']}\n  {e['description'][:300]}")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
        print(f"\nWritten: {args.out}")


if __name__ == "__main__":
    main()
