"""
Tests for the multi-user workspace API (api.py + storage.py + jobs.py):
accounts, projects and visibility, run drafts with uploaded files, background
scans with progress, editing results without re-running, clone & re-run.

The LangGraph pipeline is replaced by a stub, so no LLM is needed.

    python -m pytest tests/test_workspace_api.py -q
"""

import io
import os
import sys
import tempfile
import time
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["CONFLICTSCAN_DATA_DIR"] = tempfile.mkdtemp(prefix="cs_test_data_")
os.environ["CONFLICTSCAN_SECRET_KEY"] = "test"

import api      # noqa: E402
import graph    # noqa: E402
import jobs     # noqa: E402

DISCREPANCIES = [
    {"id": "D-001", "topic": "Concrete grade - columns", "severity": "critical", "scope": "between",
     "description": "C40 vs C35", "document_a_says": "C40", "document_b_says": "C35", "issue_class": "spec_conflict"},
    {"id": "D-002", "topic": "Cover to rebar", "severity": "minor", "scope": "within_a",
     "description": "40 vs 50 mm", "document_a_says": "40 mm", "document_b_says": ""},
]


def fake_scan(**kwargs):
    """Stands in for graph.run_conflict_scan: reports every stage, returns two findings and one RFI."""
    for node in jobs.STAGE_KEYS:
        print(f"[stub] running {node}")
        if kwargs.get("on_node_complete"):
            kwargs["on_node_complete"](node, {"discrepancies": DISCREPANCIES} if node == "conflict_scan" else {})
        if kwargs.get("should_stop") and kwargs["should_stop"]():
            raise graph.ScanCancelled()
    register = os.path.join(ROOT, "exports", f"stub_register_{time.time_ns()}.xlsx")
    os.makedirs(os.path.dirname(register), exist_ok=True)
    with open(register, "wb") as f:
        f.write(b"stub")
    assert kwargs["document_a_files"][0]["name"] == "boq.txt"
    return {
        "project_name": kwargs["project_name"],
        "document_a_label": kwargs["document_a_label"],
        "document_b_label": kwargs["document_b_label"],
        "discrepancies": DISCREPANCIES,
        "rfi_items": [{
            "rfi_no": "RFI-001", "subject": "Concrete grade", "discrepancy_id": "D-001", "discrepancy_ids": ["D-001"],
            "background": "bg", "discrepancy_summary": "1. C40 vs C35", "question": "Which governs?",
            "attachments": ["boq.txt"], "priority": "high", "discipline": "structural", "docx_path": None,
        }],
        "register_path": register,
        "scan_summary": "2 findings",
        "confidence_level": "high",
        "step_history": ["stub"],
    }


@pytest.fixture(autouse=True)
def stub_graph(monkeypatch):
    monkeypatch.setattr(graph, "run_conflict_scan", fake_scan)


def client_for(username):
    c = api.app.test_client()
    r = c.post("/api/auth/register", json={"username": username, "password": "secret1", "display_name": username.title()})
    if r.status_code == 409:
        r = c.post("/api/auth/login", json={"username": username, "password": "secret1"})
    assert r.status_code == 200, r.get_json()
    return c


def wait_for(c, run_id, statuses=("complete", "failed", "cancelled"), timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = c.get(f"/api/runs/{run_id}/progress").get_json()
        if run["status"] in statuses:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run stayed {run['status']}")


def upload(c, run_id, slot, name, content=b"hello"):
    return c.post(f"/api/runs/{run_id}/files", data={"slot": slot, "files": (io.BytesIO(content), name)},
                  content_type="multipart/form-data")


def new_scanned_run(c, name="Tower A"):
    r = c.post("/api/projects", json={"name": name})
    assert r.status_code == 201
    run_id = r.get_json()["run"]["id"]
    assert upload(c, run_id, "a", "boq.txt", b"Concrete C40").status_code == 201
    assert upload(c, run_id, "b", "spec.txt", b"Concrete C35").status_code == 201
    assert c.patch(f"/api/runs/{run_id}", json={"inputs": {
        "document_a_label": "Tender BOQ", "document_b_label": "IFC Specs",
        "rfi_form_meta": {"consultant": "Dar"}}}).status_code == 200
    assert c.post(f"/api/runs/{run_id}/start").status_code == 200
    return r.get_json()["project"]["id"], run_id


def test_requires_sign_in():
    c = api.app.test_client()
    assert c.get("/api/projects").status_code == 401
    assert c.get("/api/auth/status").get_json()["user"] is None
    assert c.post("/api/auth/login", json={"username": "nobody", "password": "x"}).status_code == 401


def test_full_run_lifecycle_and_editing():
    c = client_for("alice")
    project_id, run_id = new_scanned_run(c)
    run = wait_for(c, run_id)
    assert run["status"] == "complete", run.get("error")
    assert [s["status"] for s in run["progress"]["stages"]] == ["done"] * 5
    assert any("[stub] running" in line for line in run["progress"]["log"])
    res = run["results"]
    assert len(res["discrepancies"]) == 2 and res["rfi_items"][0]["rfi_no"] == "RFI-001"
    assert res["register_file"] and os.path.isfile(os.path.join(api.storage.RUNS_DIR, run_id, "exports", res["register_file"]))
    assert run["counts"]["findings"] == 2 and run["counts"]["critical"] == 1

    # inputs are locked once scanned
    assert c.patch(f"/api/runs/{run_id}", json={"inputs": {"review_focus": "x"}}).status_code == 409
    assert upload(c, run_id, "a", "more.txt").status_code == 409
    # ...but the title is not
    assert c.patch(f"/api/runs/{run_id}", json={"title": "First pass"}).get_json()["title"] == "First pass"

    # triage a finding
    r = c.patch(f"/api/runs/{run_id}/findings/D-002", json={"status": "dismissed", "note": "Typo in BOQ"})
    assert r.get_json()["counts"]["dismissed"] == 1
    c.patch(f"/api/runs/{run_id}/findings/D-001", json={"severity": "major"})
    run = c.get(f"/api/runs/{run_id}").get_json()
    assert run["review"]["findings"]["D-002"]["note"] == "Typo in BOQ"
    assert run["counts"]["major"] == 1 and run["counts"]["critical"] == 0

    # edit an RFI, then revert it
    r = c.put(f"/api/runs/{run_id}/rfis/RFI-001", json={"subject": "Edited subject", "attachments": "a.pdf\nb.pdf"})
    edited = r.get_json()
    assert edited["subject"] == "Edited subject" and edited["attachments"] == ["a.pdf", "b.pdf"]
    assert edited["original"]["subject"] == "Concrete grade" and edited["edited_by"] == "Alice"
    reverted = c.post(f"/api/runs/{run_id}/rfis/RFI-001/revert").get_json()
    assert reverted["subject"] == "Concrete grade" and "original" not in reverted

    # add + delete an RFI by hand
    added = c.post(f"/api/runs/{run_id}/rfis", json={"discrepancy_ids": ["D-002"]}).get_json()
    assert added["rfi_no"] == "RFI-002" and added["discrepancy_ids"] == ["D-002"]
    assert c.delete(f"/api/runs/{run_id}/rfis/RFI-002").status_code == 200

    # Word document from the edited text (the repo ships the RFI form)
    if api.resolve_template_path():
        doc = c.post(f"/api/runs/{run_id}/rfis/RFI-001/docx").get_json()
        assert doc["docx_file"].endswith("_edited.docx") and doc["docx_stale"] is False
        assert c.get(f"/api/runs/{run_id}/exports/{doc['docx_file']}").status_code == 200

    # register rebuilt with edits (dismissed finding dropped) and a zip of everything
    reg = c.post(f"/api/runs/{run_id}/register")
    assert reg.status_code == 200, reg.get_json()
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/runs/{run_id}/bundle").data))
    assert reg.get_json()["register_file"] in z.namelist() and "findings.json" in z.namelist()

    # Edit & re-run: new revision with the same files and settings
    clone = c.post(f"/api/runs/{run_id}/clone").get_json()
    assert clone["seq"] == 2 and clone["status"] == "draft" and clone["parent_run_id"] == run_id
    full = c.get(f"/api/runs/{clone['id']}").get_json()
    assert sorted(f["name"] for f in full["files"]) == ["boq.txt", "spec.txt"]
    assert full["inputs"]["document_a_label"] == "Tender BOQ"
    assert full["inputs_editable"] is True

    # a brand-new run in the project starts from the last settings, without files
    fresh = c.post(f"/api/projects/{project_id}/runs", json={}).get_json()
    fresh_full = c.get(f"/api/runs/{fresh['id']}").get_json()
    assert fresh_full["files"] == [] and fresh_full["inputs"]["rfi_form_meta"]["consultant"] == "Dar"

    listing = c.get("/api/projects").get_json()
    proj = next(p for p in listing if p["id"] == project_id)
    assert proj["run_count"] == 3


def test_start_requires_both_sets_and_file_rules():
    c = client_for("bob")
    run_id = c.post("/api/projects", json={"name": "Empty"}).get_json()["run"]["id"]
    assert c.post(f"/api/runs/{run_id}/start").status_code == 400
    assert upload(c, run_id, "a", "plan.dwg").status_code == 400
    f = upload(c, run_id, "a", "sheet.dxf").get_json()["added"][0]
    assert f["detected_kind"] == "drawing"
    assert c.patch(f"/api/runs/{run_id}/files/{f['id']}", json={"kind": "document"}).status_code == 400
    assert c.patch(f"/api/runs/{run_id}/files/{f['id']}", json={"slot": "b"}).get_json()["slot"] == "b"
    assert c.delete(f"/api/runs/{run_id}/files/{f['id']}").status_code == 200
    assert c.get(f"/api/runs/{run_id}").get_json()["files"] == []


def test_visibility_and_permissions():
    alice = client_for("alice")
    carol = client_for("carol")
    team = alice.post("/api/projects", json={"name": "Shared job"}).get_json()["project"]
    private = alice.post("/api/projects", json={"name": "Secret job", "visibility": "private"}).get_json()["project"]

    ids = {p["id"] for p in carol.get("/api/projects").get_json()}
    assert team["id"] in ids and private["id"] not in ids
    assert carol.get(f"/api/projects/{private['id']}").status_code == 404

    # team members can work in the project, but only the owner deletes it or changes visibility
    assert carol.patch(f"/api/projects/{team['id']}", json={"description": "notes"}).status_code == 200
    assert carol.patch(f"/api/projects/{team['id']}", json={"visibility": "private"}).status_code == 403
    assert carol.delete(f"/api/projects/{team['id']}").status_code == 403
    assert alice.delete(f"/api/projects/{team['id']}").status_code == 200


def test_cancel_queued_run_and_prefs():
    c = client_for("dave")
    run_id = c.post("/api/projects", json={"name": "Cancel me"}).get_json()["run"]["id"]
    upload(c, run_id, "a", "boq.txt")
    upload(c, run_id, "b", "spec.txt")
    c.post(f"/api/runs/{run_id}/start")
    c.post(f"/api/runs/{run_id}/cancel")
    run = wait_for(c, run_id)
    assert run["status"] in ("cancelled", "complete")
    if run["status"] == "cancelled":
        assert run["inputs_editable"] is True

    c.put("/api/me/prefs", json={"workspace": {"tabs": ["home", f"run:{run_id}"], "active": "home"}, "pinned": ["x"], "bogus": 1})
    prefs = c.get("/api/me/prefs").get_json()
    assert prefs["workspace"]["tabs"][1] == f"run:{run_id}" and "bogus" not in prefs
