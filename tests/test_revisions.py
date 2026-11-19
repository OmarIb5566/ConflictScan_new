"""
Phase 3 tests: project settings and the project's own RFI form, carry-forward
between revisions (triage, RFI numbers and edits), revision comparison, file
supersession and history, duplicate detection, incremental re-scans.
"""

import io
import os

import pytest

from test_workspace_api import (  # noqa: F401
    DISCREPANCIES, api, client_for, fake_scan, graph, inspected, jobs, new_scanned_run, stub_graph, upload, wait_for,
)
import revisions
import storage


def _docx_form(tokens=("rfi_no", "subject", "consultant", "question")):
    import docx
    d = docx.Document()
    for t in tokens:
        d.add_paragraph(f"{t}: {{{{{t}}}}}")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def test_findings_match_across_revisions():
    prev = [{"id": "D-001", "topic": "Concrete grade - columns L3-L5", "description": "C40 vs C35", "issue_class": "doc_value_conflict",
             "document_a_says": "C40/50", "document_b_says": "C35/45", "location": "Block A"},
            {"id": "D-002", "topic": "Lift pit depth", "description": "1500 vs 1200 mm", "issue_class": "doc_value_conflict",
             "document_a_says": "1500 mm", "document_b_says": "1200 mm", "location": "Core 2"}]
    new = [{"id": "D-007", "topic": "Concrete grade of columns L3 to L5", "description": "C40 vs C35",
            "issue_class": "doc_value_conflict", "document_a_says": "C40/50", "document_b_says": "C35/45", "location": "Block A"},
           {"id": "D-008", "topic": "Waterproofing membrane missing", "description": "not in BOQ", "issue_class": "scope_gap_boq",
            "document_a_says": "Not addressed", "document_b_says": "2-layer SBS", "location": "Basement"}]
    m = revisions.match_findings(prev, new)
    assert m["D-007"][0] == "D-001" and "D-008" not in m
    cmp = revisions.compare({"id": "r1", "seq": 1, "results": {"discrepancies": prev}, "review": {}},
                            {"id": "r2", "seq": 2, "results": {"discrepancies": new}})
    assert [x["id"] for x in cmp["new"]] == ["D-008"]
    assert [x["id"] for x in cmp["resolved"]] == ["D-002"]
    assert [x["id"] for x in cmp["unchanged"]] == ["D-007"]


# ---------------------------------------------------------------------------
# Carry-forward through a real re-run
# ---------------------------------------------------------------------------

def scan_variant(monkeypatch, discrepancies, rfis):
    def variant(**kwargs):
        out = fake_scan(**kwargs)
        out["discrepancies"] = discrepancies
        out["rfi_items"] = rfis
        return out
    monkeypatch.setattr(graph, "run_conflict_scan", variant)


def test_rerun_carries_triage_rfi_numbers_and_edits(monkeypatch):
    c = client_for("rev_eng")
    project_id, run_id = new_scanned_run(c, "Carry forward")
    wait_for(c, run_id)
    c.patch(f"/api/runs/{run_id}/findings/D-001", json={"status": "accepted", "note": "Confirmed with QS"})
    c.patch(f"/api/runs/{run_id}/findings/D-002", json={"status": "dismissed"})
    c.put(f"/api/runs/{run_id}/rfis/RFI-001", json={"subject": "Concrete grade - my wording"})

    # Rev 2: D-001 unchanged (renumbered), D-002 gone, one new finding; the drafter numbers RFIs from 1 again
    d1 = dict(DISCREPANCIES[0], id="D-001")
    new = dict(id="D-002", topic="Fire rating of shaft walls", severity="minor", scope="between",
               description="2 hr vs 1 hr", document_a_says="2 hr", document_b_says="1 hr", issue_class="spec_conflict")
    scan_variant(monkeypatch, [d1, new], [
        {"rfi_no": "RFI-001", "subject": "fresh draft", "discrepancy_ids": ["D-001"], "discrepancy_id": "D-001"},
        {"rfi_no": "RFI-002", "subject": "shaft walls", "discrepancy_ids": ["D-002"], "discrepancy_id": "D-002"}])
    rev2 = c.post(f"/api/runs/{run_id}/clone").get_json()
    c.post(f"/api/runs/{rev2['id']}/start")
    run2 = wait_for(c, rev2["id"])
    assert run2["status"] == "complete", run2.get("error")
    full = c.get(f"/api/runs/{rev2['id']}").get_json()
    res = full["results"]
    assert res["changes"]["unchanged"] == 1 and res["changes"]["new"] == 1 and res["changes"]["resolved"] == 1
    assert [f["id"] for f in res["resolved_findings"]] == ["D-002"] and res["resolved_findings"][0]["prev_status"] == "dismissed"
    by_id = {d["id"]: d for d in res["discrepancies"]}
    assert by_id["D-001"]["change"] == "unchanged" and by_id["D-002"]["change"] == "new"
    review = full["review"]["findings"]
    assert review["D-001"]["status"] == "accepted" and review["D-001"]["note"] == "Confirmed with QS"
    assert review["D-001"]["carried_from"] == "Rev 1" and "D-002" not in review
    rfis = {r["subject"]: r for r in res["rfi_items"]}
    kept = rfis["Concrete grade - my wording"]                  # edited text carried, same number
    assert kept["rfi_no"] == "RFI-001" and kept["carried_edits"]
    assert rfis["shaft walls"]["rfi_no"] == "RFI-002" and rfis["shaft walls"]["change"] == "new"
    cmp = c.get(f"/api/runs/{rev2['id']}/compare").get_json()
    assert cmp["base_seq"] == 1 and len(cmp["resolved"]) == 1


def test_changed_findings_are_reopened(monkeypatch):
    c = client_for("rev_eng2")
    _project_id, run_id = new_scanned_run(c, "Reopen")
    wait_for(c, run_id)
    c.patch(f"/api/runs/{run_id}/findings/D-001", json={"status": "accepted"})
    changed = dict(DISCREPANCIES[0], document_b_says="C30/37", severity="major")
    scan_variant(monkeypatch, [changed], [])
    rev2 = c.post(f"/api/runs/{run_id}/clone").get_json()
    c.post(f"/api/runs/{rev2['id']}/start")
    wait_for(c, rev2["id"])
    full = c.get(f"/api/runs/{rev2['id']}").get_json()
    d = full["results"]["discrepancies"][0]
    assert d["change"] == "changed" and full["review"]["findings"]["D-001"]["status"] == "open"
    assert "changed since Rev 1" in full["review"]["findings"]["D-001"]["reopened"]


# ---------------------------------------------------------------------------
# Files: supersession, history, duplicates, nothing re-uploaded
# ---------------------------------------------------------------------------

def test_replace_a_drawing_keeps_its_history():
    c = client_for("rev_files")
    project_id, run_id = new_scanned_run(c, "Supersession")
    wait_for(c, run_id)
    rev2 = c.post(f"/api/runs/{run_id}/clone").get_json()
    files = c.get(f"/api/runs/{rev2['id']}").get_json()["files"]
    spec = next(f for f in files if f["name"] == "spec.txt")
    assert spec["unchanged_since_parent"] is None or spec["unchanged_since_parent"] in (True, False)
    r = c.post(f"/api/runs/{rev2['id']}/files/{spec['id']}/replace",
               data={"file": (io.BytesIO(b"Concrete C30 - revised spec"), "spec_revB.txt")}, content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    new = r.get_json()
    assert new["supersedes"] == "spec.txt" and new["slot"] == "b" and new["lineage_id"] == spec["lineage_id"]
    names = [f["name"] for f in c.get(f"/api/runs/{rev2['id']}").get_json()["files"]]
    assert "spec.txt" not in names and "spec_revB.txt" in names
    history = c.get(f"/api/projects/{project_id}/files").get_json()
    lineage = next(h for h in history if h["lineage_id"] == spec["lineage_id"])
    assert [v["name"] for v in lineage["versions"]] == ["spec.txt", "spec_revB.txt"]
    assert lineage["versions"][0]["revisions"] == [1] and lineage["versions"][1]["revisions"] == [2]
    boq = next(h for h in history if h["current_name"] == "boq.txt")
    assert boq["versions"][0]["revisions"] == [1, 2]                 # same file, never re-uploaded
    # Rev 1 is untouched
    assert sorted(f["name"] for f in c.get(f"/api/runs/{run_id}").get_json()["files"]) == ["boq.txt", "spec.txt"]


def test_cloned_files_are_unchanged_and_duplicates_flagged():
    c = client_for("rev_dupes")
    _project_id, run_id = new_scanned_run(c, "Dupes")
    wait_for(c, run_id)
    rev2 = c.post(f"/api/runs/{run_id}/clone").get_json()
    files = inspected(c, rev2["id"])
    assert all(f["unchanged_since_parent"] for f in files)
    upload(c, rev2["id"], "b", "boq_copy.txt", b"Concrete C40")       # same bytes as boq.txt in set A
    files = inspected(c, rev2["id"])
    copy = next(f for f in files if f["name"] == "boq_copy.txt")
    assert copy["duplicate_of"] == [{"name": "boq.txt", "slot": "a"}]


# ---------------------------------------------------------------------------
# Project settings and the project's RFI form
# ---------------------------------------------------------------------------

def test_project_settings_and_numbering():
    c = client_for("rev_pm")
    p = c.post("/api/projects", json={"name": "Settings", "create_run": False}).get_json()["project"]
    r = c.patch(f"/api/projects/{p['id']}", json={"code": "TRC02", "settings": {
        "rfi_form_meta": {"consultant": "Dar", "employer": "Owner Co"}, "set_a_label": "Tender BOQ",
        "numbering": {"format": "{code}-RFI-RME-{disc}-{seq:04d}", "start": 139}, "response_days": 10}})
    assert r.status_code == 200, r.get_json()
    s = r.get_json()["settings"]
    assert s["numbering"]["start"] == 139 and s["rfi_form_meta"]["consultant"] == "Dar"
    import project_settings
    assert project_settings.format_number(s["numbering"]["format"], 139, "electrical", "TRC02") == "TRC02-RFI-RME-ELEC-0139"
    bad = c.patch(f"/api/projects/{p['id']}", json={"settings": {"numbering": {"format": "{nope}"}}})
    assert bad.status_code == 400 and "Numbering format" in bad.get_json()["error"]
    run = c.post(f"/api/projects/{p['id']}/runs", json={}).get_json()
    inputs = c.get(f"/api/runs/{run['id']}").get_json()["inputs"]
    assert inputs["document_a_label"] == "Tender BOQ" and inputs["rfi_form_meta"]["employer"] == "Owner Co"
    # engineers cannot change project settings
    eng = client_for("rev_eng3")
    assert eng.patch(f"/api/projects/{p['id']}", json={"settings": {"response_days": 3}}).status_code == 403


def test_project_rfi_form_upload_preview_and_use():
    c = client_for("rev_form")
    project_id, run_id = new_scanned_run(c, "Own form")
    wait_for(c, run_id)
    bad = c.post(f"/api/projects/{project_id}/rfi-form", data={"file": (io.BytesIO(b"not a docx"), "form.docx")},
                 content_type="multipart/form-data")
    assert bad.status_code == 400
    r = c.post(f"/api/projects/{project_id}/rfi-form", data={"file": (io.BytesIO(_docx_form()), "Our RFI form.docx")},
               content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    assert set(r.get_json()["rfi_form"]["tokens"]) >= {"rfi_no", "subject", "consultant"}
    pv = c.get(f"/api/projects/{project_id}/rfi-form/preview").get_json()
    vals = {t["token"]: t["value"] for t in pv["tokens"]}
    assert pv["own_form"] and pv["sample_from_scan"] and vals["subject"] == "Concrete grade" and vals["consultant"] == "Dar"
    docx_resp = c.get(f"/api/projects/{project_id}/rfi-form/preview?format=docx")
    assert docx_resp.status_code == 200 and docx_resp.data[:2] == b"PK"
    # RFI documents of this project now use its own form
    doc = c.post(f"/api/runs/{run_id}/rfis/RFI-001/docx").get_json()
    import docx
    path = os.path.join(storage.run_dir(run_id, "exports"), doc["docx_file"])
    text = "\n".join(p.text for p in docx.Document(path).paragraphs)
    assert "subject: Concrete grade" in text
    # ...and the scan gets it too
    import project_settings
    assert project_settings._scan_extras({}, storage.get_project(project_id))["rfi_template_path"].endswith(".docx")
    assert c.delete(f"/api/projects/{project_id}/rfi-form").get_json()["rfi_form"] is None
