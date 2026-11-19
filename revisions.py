"""
Revisions - working on a project over time without starting again
=================================================================
When a new revision of a project finishes scanning, it is matched against the
revision it was made from (or the project's latest complete revision):

- carry forward: every finding the engineer already triaged keeps its status,
  severity change, note and assignee; RFIs that cover the same findings keep
  their number and, when their findings did not change, the engineer's edited
  text; new findings get new RFI numbers after the existing ones;
- changes: each finding is marked new / unchanged / changed (severity or the
  values quoted), and findings that no longer appear are listed as resolved.

Also here:
    GET  /api/runs/<id>/compare?base=<run id>   new / resolved / unchanged / changed
    POST /api/runs/<id>/files/<fid>/replace     upload a new version of a document (supersession)
    GET  /api/projects/<id>/files               every document's history across revisions

Unchanged files are never re-uploaded (a new revision links the same stored
files) and never re-read by the models (the AI fleet's response cache answers
every request it has seen before).

Windows compatible.
"""

import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from flask import abort, jsonify, request
from werkzeug.utils import secure_filename

import jobs
import storage
from webcore import audit, load_project, load_run, login_required

MATCH_THRESHOLD = 0.42
REVIEW_KEYS = ("status", "note", "severity", "assignee", "by", "at")
RFI_TEXT_KEYS = ("subject", "area", "background", "discrepancy_summary", "question", "proposed_solution",
                 "commercial_note", "cost_impact", "schedule_impact", "attachments", "discipline", "priority",
                 "status", "response_required_by", "document_a_reference", "document_b_reference", "description")


# ---------------------------------------------------------------------------
# Matching findings between two revisions
# ---------------------------------------------------------------------------

_STOP = {"the", "and", "for", "with", "between", "set", "document", "drawing", "drawings", "shown", "states", "not",
         "addressed", "value", "values", "differs", "different", "per", "from", "this", "that", "are", "has"}


def _tokens(*parts) -> set:
    text = " ".join(str(p or "") for p in parts).lower()
    return {w for w in re.findall(r"[a-z0-9؀-ۿ][a-z0-9./؀-ۿ-]{1,}", text) if w not in _STOP}


def _jac(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _says(d: Dict[str, Any]) -> str:
    ev = " ".join(str(e.get("says") or "") for e in d.get("evidence") or [])
    return f"{d.get('document_a_says') or ''} {d.get('document_b_says') or ''} {ev}"


def similarity(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    score = 0.45 * _jac(_tokens(a.get("topic"), a.get("description")), _tokens(b.get("topic"), b.get("description")))
    score += 0.30 * _jac(_tokens(_says(a)), _tokens(_says(b)))
    score += 0.15 * (1.0 if a.get("issue_class") and a.get("issue_class") == b.get("issue_class") else 0.0)
    score += 0.10 * _jac(_tokens(a.get("location")), _tokens(b.get("location")))
    return score


def match_findings(prev: List[Dict[str, Any]], new: List[Dict[str, Any]]) -> Dict[str, Tuple[str, float]]:
    """{new finding id: (previous finding id, score)} - one-to-one, best matches first."""
    cand = []
    for n in new:
        for p in prev:
            s = similarity(n, p)
            if s >= MATCH_THRESHOLD:
                cand.append((s, n.get("id"), p.get("id")))
    cand.sort(key=lambda c: -c[0])
    used_new, used_prev, out = set(), set(), {}
    for s, nid, pid in cand:
        if nid in used_new or pid in used_prev:
            continue
        used_new.add(nid)
        used_prev.add(pid)
        out[nid] = (pid, round(s, 3))
    return out


def _changed(prev: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    what = []
    if (prev.get("severity") or "") != (new.get("severity") or ""):
        what.append(f"severity {prev.get('severity')} -> {new.get('severity')}")
    if _tokens(_says(prev)) != _tokens(_says(new)):
        what.append("quoted values changed")
    return what


def compare(base: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    prev = (base.get("results") or {}).get("discrepancies") or []
    new = (current.get("results") or {}).get("discrepancies") or []
    mapping = match_findings(prev, new)
    prev_by_id = {d.get("id"): d for d in prev}
    prev_review = (base.get("review") or {}).get("findings", {})
    matched_prev = {pid for pid, _s in mapping.values()}
    out = {"base_run_id": base["id"], "base_seq": base["seq"], "run_id": current["id"], "seq": current["seq"],
           "new": [], "unchanged": [], "changed": [], "resolved": []}
    for d in new:
        m = mapping.get(d.get("id"))
        brief = {"id": d.get("id"), "topic": d.get("topic"), "severity": d.get("severity"), "location": d.get("location")}
        if not m:
            out["new"].append(brief)
            continue
        p = prev_by_id[m[0]]
        what = _changed(p, d)
        entry = {**brief, "prev_id": m[0], "score": m[1], "prev_status": (prev_review.get(m[0]) or {}).get("status", "open")}
        (out["changed"] if what else out["unchanged"]).append({**entry, "what": what} if what else entry)
    for p in prev:
        if p.get("id") not in matched_prev:
            out["resolved"].append({"id": p.get("id"), "topic": p.get("topic"), "severity": p.get("severity"),
                                    "location": p.get("location"),
                                    "prev_status": (prev_review.get(p.get("id")) or {}).get("status", "open")})
    return out


# ---------------------------------------------------------------------------
# Carry-forward when a revision finishes scanning
# ---------------------------------------------------------------------------

def base_revision(run: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The revision this one builds on: its parent if that has results, else the latest earlier complete one."""
    if run.get("parent_run_id"):
        parent = storage.get_run(run["parent_run_id"])
        if parent and parent.get("results"):
            return parent
    for r in storage.list_runs(run["project_id"]):
        if r["seq"] < run["seq"] and r["status"] == "complete":
            full = storage.get_run(r["id"])
            if full and full.get("results"):
                return full
    return None


def _num(rfi_no: str) -> int:
    m = re.search(r"(\d+)$", str(rfi_no or ""))
    return int(m.group(1)) if m else 0


def carry_forward(run: Dict[str, Any], project: Dict[str, Any], results: Dict[str, Any]) -> Dict[str, Any]:
    base = base_revision(run)
    if not base:
        return results
    prev_results = base.get("results") or {}
    prev = prev_results.get("discrepancies") or []
    new = results.get("discrepancies") or []
    mapping = match_findings(prev, new)
    prev_by_id = {d.get("id"): d for d in prev}
    prev_review = (base.get("review") or {}).get("findings", {})

    # 1. findings: change markers + triage carried over
    review = {"findings": {}}
    for d in new:
        m = mapping.get(d.get("id"))
        if not m:
            d["change"] = "new"
            continue
        pid = m[0]
        d["prev_id"] = pid
        what = _changed(prev_by_id[pid], d)
        d["change"] = "changed" if what else "unchanged"
        if what:
            d["change_detail"] = what
        old = prev_review.get(pid)
        if old:
            carried = {k: old[k] for k in REVIEW_KEYS if k in old}
            carried["carried_from"] = f"Rev {base['seq']}"
            if what and carried.get("status") in ("accepted", "dismissed"):
                carried["status"] = "open"               # it changed - look at it again
                carried["reopened"] = "changed since Rev %s" % base["seq"]
            review["findings"][d.get("id")] = carried

    # 2. RFIs: keep numbers (and untouched-finding edits) of RFIs covering the same findings
    prev_rfis = prev_results.get("rfi_items") or []
    prev_of_finding = {}
    for r in prev_rfis:
        for fid in r.get("discrepancy_ids") or []:
            prev_of_finding[fid] = r
    used_numbers = set()
    next_no = max([_num(r.get("rfi_no")) for r in prev_rfis] + [0]) + 1
    for r in results.get("rfi_items") or []:
        mapped_prev = [mapping[f][0] for f in (r.get("discrepancy_ids") or []) if f in mapping]
        candidates = [prev_of_finding[p] for p in mapped_prev if p in prev_of_finding]
        old = candidates[0] if candidates else None
        if old and old.get("rfi_no") not in used_numbers:
            used_numbers.add(old["rfi_no"])
            r["rfi_no"] = old["rfi_no"]
            r["carried_from"] = f"Rev {base['seq']}"
            for k in ("log_id", "log_no"):
                if old.get(k):
                    r[k] = old[k]
            same_findings = (sorted(mapped_prev) == sorted(old.get("discrepancy_ids") or [])
                             and len(mapped_prev) == len(r.get("discrepancy_ids") or []))
            unchanged = all(d.get("change") == "unchanged" for d in new if d.get("id") in (r.get("discrepancy_ids") or []))
            if old.get("edited_at") and same_findings and unchanged:
                for k in RFI_TEXT_KEYS:
                    if k in old:
                        r[k] = old[k]
                r["edited_by"], r["edited_at"] = old.get("edited_by"), old.get("edited_at")
                r["original"] = old.get("original")
                r["carried_edits"] = True
            elif old.get("edited_at"):
                r["previous_text"] = {k: old.get(k) for k in RFI_TEXT_KEYS if k in old}
                r["carried_note"] = (f"Findings changed since Rev {base['seq']} - this is a fresh draft; "
                                     f"your earlier wording is kept under 'Previous text'.")
        else:
            r["rfi_no"] = f"RFI-{next_no:03d}"
            next_no += 1
            r["change"] = "new"

    matched_prev = {pid for pid, _s in mapping.values()}
    results["resolved_findings"] = [
        {"id": p.get("id"), "topic": p.get("topic"), "severity": p.get("severity"), "location": p.get("location"),
         "description": p.get("description"),
         "prev_status": (prev_review.get(p.get("id")) or {}).get("status", "open")}
        for p in prev if p.get("id") not in matched_prev]
    results["changes"] = {"base_run_id": base["id"], "base_seq": base["seq"],
                          "new": sum(1 for d in new if d.get("change") == "new"),
                          "changed": sum(1 for d in new if d.get("change") == "changed"),
                          "unchanged": sum(1 for d in new if d.get("change") == "unchanged"),
                          "resolved": len(results["resolved_findings"]),
                          "triage_carried": len(review["findings"])}
    if review["findings"]:
        storage.update_run(run["id"], review=review, bump=False)
    print(f"[revisions] vs Rev {base['seq']}: {results['changes']}")
    return results


jobs._FINALISERS.append(carry_forward)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def register(app):
    import api

    @app.route("/api/runs/<run_id>/compare")
    @login_required
    def run_compare(run_id):
        run, project = load_run(run_id)
        if not run.get("results"):
            return jsonify({"error": "This revision has no results yet."}), 409
        base_id = request.args.get("base")
        base = storage.get_run(base_id) if base_id else base_revision(run)
        if not base or base["project_id"] != run["project_id"] or not base.get("results"):
            return jsonify({"error": "No earlier revision with results to compare with."}), 404
        return jsonify(compare(base, run))

    @app.route("/api/runs/<run_id>/files/<file_id>/replace", methods=["POST"])
    @login_required
    def run_file_replace(run_id, file_id):
        """A new version of a document (e.g. drawing Rev B for Rev A): same set and place, history kept."""
        run, project = load_run(run_id, "engineer")
        old = storage.get_run_file(file_id)
        if not old or old["run_id"] != run_id:
            abort(404)
        if run["status"] not in api.EDITABLE_INPUT_STATUSES:
            return jsonify({"error": "This run's inputs are locked. Use 'Edit & re-run' to change files."}), 409
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify({"error": "Choose the new version of the file."}), 400
        filename = secure_filename(upload.filename) or f"file_{uuid.uuid4().hex[:6]}"
        record, err = api.store_upload(run_id, old["slot"], upload, filename)
        if err:
            return jsonify({"error": err}), 400
        from db import run_files
        from sqlalchemy import update
        with storage.tx() as conn:
            conn.execute(update(run_files).where(run_files.c.id == record["id"]).values(
                lineage_id=old.get("lineage_id") or old["id"], supersedes=old["name"],
                supersedes_sha256=old.get("sha256"), position=old["position"], kind=old["kind"]))
        storage.delete_run_file(file_id)
        storage.update_run(run_id, user_id=request.user["id"], status="draft")
        audit("run.file_replaced", "run", run_id, project["id"], f"{old['name']} -> {filename}")
        return jsonify(api._file_payload(storage.get_run_file(record["id"]))), 201

    @app.route("/api/projects/<project_id>/files")
    @login_required
    def project_files(project_id):
        """Each document's history: which revisions used which version."""
        load_project(project_id)
        rows = storage.project_file_history(project_id)
        lineages: Dict[str, Dict[str, Any]] = {}
        for f in rows:
            key = f.get("lineage_id") or f["id"]
            entry = lineages.setdefault(key, {"lineage_id": key, "slot": f["slot"], "versions": []})
            v = entry["versions"]
            if v and v[-1]["sha256"] == f.get("sha256") and v[-1]["name"] == f["name"]:
                v[-1]["revisions"].append(f["seq"])
            else:
                v.append({"name": f["name"], "sha256": f.get("sha256"), "size": f["size"], "pages": f.get("pages"),
                          "revisions": [f["seq"]], "file_id": f["id"], "run_id": f["run_id"],
                          "supersedes": f.get("supersedes"), "uploaded_at": f["created_at"]})
            entry["current_name"] = f["name"]
            entry["last_rev"] = f["seq"]
        out = sorted(lineages.values(), key=lambda e: (e["slot"], e["current_name"].lower()))
        return jsonify(out)


def annotate_files(run: Dict[str, Any], files: List[Dict[str, Any]]) -> None:
    """Duplicates within the revision, and whether each file is unchanged since the revision it came from."""
    by_sha: Dict[str, List[Dict[str, Any]]] = {}
    for f in files:
        if f.get("sha256"):
            by_sha.setdefault(f["sha256"], []).append(f)
    parent_files = {}
    if run.get("parent_run_id"):
        parent_files = {pf.get("lineage_id") or pf["id"]: pf for pf in storage.list_run_files(run["parent_run_id"])}
    for f in files:
        same = [o for o in by_sha.get(f.get("sha256") or "", []) if o["id"] != f["id"]]
        if same:
            f["duplicate_of"] = [{"name": o["name"], "slot": o["slot"]} for o in same]
        pf = parent_files.get(f.get("lineage_id"))
        if pf is not None:
            f["unchanged_since_parent"] = bool(f.get("sha256") and pf.get("sha256") == f.get("sha256"))
