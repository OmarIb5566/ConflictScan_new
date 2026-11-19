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
from flask.testing import FlaskClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["CONFLICTSCAN_DATA_DIR"] = tempfile.mkdtemp(prefix="cs_test_data_")
os.environ["CONFLICTSCAN_SECRET_KEY"] = "test"
os.environ.setdefault("CONFLICTSCAN_ALLOW_SIGNUP", "1")
os.environ.setdefault("CONFLICTSCAN_SIGNUP_APPROVAL", "0")
os.environ.setdefault("CONFLICTSCAN_RETRY_SECONDS", "1")

import api      # noqa: E402
import graph    # noqa: E402
import jobs     # noqa: E402
import security  # noqa: E402
import upload_checks  # noqa: E402

PASSWORD = "secret12"
DXF = b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n"


class CsrfClient(FlaskClient):
    """A test client that behaves like the web page: echoes the cs_csrf cookie in X-CSRF-Token."""

    def open(self, *args, **kwargs):
        method = (kwargs.get("method") or "GET").upper()
        if method in security.UNSAFE_METHODS and not kwargs.pop("no_csrf", False):
            if self.get_cookie(security.CSRF_COOKIE) is None:
                super().open("/api/auth/status")
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault(security.CSRF_HEADER, self.get_cookie(security.CSRF_COOKIE).value)
            kwargs["headers"] = headers
        kwargs.pop("no_csrf", None)
        return super().open(*args, **kwargs)


api.app.test_client_class = CsrfClient

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
    assert kwargs["output_dir"] and not kwargs["output_dir"].startswith(ROOT)   # per-scan folder, not the repo
    register = os.path.join(kwargs["output_dir"], f"stub_register_{time.time_ns()}.xlsx")
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
    security.login_throttle.reset()


def client_for(username):
    c = api.app.test_client()
    r = c.post("/api/auth/register", json={"username": username, "password": PASSWORD, "display_name": username.title()})
    if r.status_code == 409:
        r = c.post("/api/auth/login", json={"username": username, "password": PASSWORD})
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


def inspected(c, run_id, timeout=10):
    """The run's files once the background upload checks have finished."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        files = c.get(f"/api/runs/{run_id}").get_json()["files"]
        if not any(f["inspecting"] for f in files):
            return files
        time.sleep(0.05)
    raise AssertionError("upload checks did not finish")


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
    f = upload(c, run_id, "a", "sheet.dxf", DXF).get_json()["added"][0]
    assert f["inspecting"] is True
    f = inspected(c, run_id)[0]
    assert f["detected_kind"] == "drawing" and f["problem"] is None
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


# ---------------------------------------------------------------------------
# Phase 0: security
# ---------------------------------------------------------------------------

def test_cross_origin_requests_get_no_access():
    c = client_for("erin")
    r = c.get("/api/projects", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert "Access-Control-Allow-Origin" not in r.headers
    assert "Access-Control-Allow-Credentials" not in r.headers


def test_named_origin_can_be_allowed(monkeypatch):
    monkeypatch.setattr(security, "ALLOWED_ORIGINS", {"https://portal.rowad.local"})
    c = client_for("erin")
    r = c.get("/api/projects", headers={"Origin": "https://portal.rowad.local"})
    assert r.headers["Access-Control-Allow-Origin"] == "https://portal.rowad.local"
    assert "Access-Control-Allow-Origin" not in c.get("/api/projects", headers={"Origin": "https://x.example"}).headers


def test_state_changes_need_the_csrf_token():
    c = client_for("frank")
    r = c.post("/api/projects", json={"name": "Forged"}, no_csrf=True)
    assert r.status_code == 403 and r.get_json()["csrf_failed"]
    r = c.post("/api/projects", json={"name": "Forged"}, headers={security.CSRF_HEADER: "wrong"})
    assert r.status_code == 403
    assert c.post("/api/projects", json={"name": "Real"}).status_code == 201
    # a stranger's browser (no session) cannot sign in on someone's behalf either
    assert api.app.test_client().post("/api/auth/login", json={"username": "frank", "password": PASSWORD},
                                      no_csrf=True).status_code == 403


def test_sign_in_rotates_the_csrf_token():
    c = client_for("frank")
    before = c.get_cookie(security.CSRF_COOKIE).value
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login", json={"username": "frank", "password": PASSWORD}).status_code == 200
    assert c.get_cookie(security.CSRF_COOKIE).value != before


def test_repeated_wrong_passwords_lock_the_username():
    client_for("gina")
    c = api.app.test_client()
    for _ in range(security.LOGIN_MAX_FAILS):
        assert c.post("/api/auth/login", json={"username": "gina", "password": "nope"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "gina", "password": PASSWORD})
    assert r.status_code == 429 and "Too many" in r.get_json()["error"]
    # other people are not affected
    assert c.post("/api/auth/login", json={"username": "frank", "password": PASSWORD}).status_code in (200, 401)


def test_short_passwords_are_refused():
    c = api.app.test_client()
    r = c.post("/api/auth/register", json={"username": "shorty", "password": "1234567"})
    assert r.status_code == 400 and "8 characters" in r.get_json()["error"]


def test_idle_sessions_are_signed_out():
    c = client_for("hana")
    assert c.get("/api/projects").status_code == 200
    with c.session_transaction() as sess:
        sess["seen"] = int(time.time()) - security.IDLE_SECONDS - 5
    r = c.get("/api/projects")
    assert r.status_code == 401 and r.get_json()["expired"] is True
    assert c.get("/api/projects").status_code == 401        # stays signed out


def test_background_polling_does_not_keep_a_session_alive():
    c = client_for("hana")
    stamp = int(time.time()) - 3600
    with c.session_transaction() as sess:
        sess["seen"] = stamp
    c.get("/api/jobs")
    with c.session_transaction() as sess:
        assert sess["seen"] == stamp
    c.get("/api/projects")
    with c.session_transaction() as sess:
        assert sess["seen"] > stamp


def test_security_headers_and_no_legacy_endpoints():
    c = client_for("ivan")
    r = c.get("/")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert c.get("/api/projects").headers["Cache-Control"] == "no-store"
    assert c.post("/scan").status_code in (404, 405)
    assert c.get("/exports/anything.xlsx").status_code == 404
    assert api.app.test_client().get("/api/rfi-template").status_code == 401


# ---------------------------------------------------------------------------
# Phase 0: upload checks
# ---------------------------------------------------------------------------

def _pdf_bytes(pages):
    import fitz
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"Sheet {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


def test_files_whose_content_does_not_match_the_extension_are_refused():
    c = client_for("judy")
    run_id = c.post("/api/projects", json={"name": "Uploads"}).get_json()["run"]["id"]
    for name, content in (("fake.pdf", b"MZ\x90\x00 not a pdf"), ("photo.png", b"MZ\x90\x00"),
                          ("sheet.xlsx", b"plain text"), ("notes.txt", b"bin\x00ary"), ("empty.csv", b"")):
        r = upload(c, run_id, "a", name, content)
        assert r.status_code == 400, name
        assert name in r.get_json()["errors"][0]
    assert c.get(f"/api/runs/{run_id}").get_json()["files"] == []
    assert os.listdir(api.storage.run_dir(run_id, "inputs")) == []      # refused files are not kept


def test_oversized_files_are_refused(monkeypatch):
    monkeypatch.setattr(upload_checks, "MAX_FILE_MB", 0)
    c = client_for("judy")
    run_id = c.post("/api/projects", json={"name": "Big"}).get_json()["run"]["id"]
    r = upload(c, run_id, "a", "big.txt", b"x" * 10)
    assert r.status_code == 400 and "limit per file" in r.get_json()["errors"][0]


def test_pdf_page_limit_blocks_the_scan(monkeypatch):
    monkeypatch.setattr(upload_checks, "MAX_PDF_PAGES", 3)
    c = client_for("kate")
    run_id = c.post("/api/projects", json={"name": "Pages"}).get_json()["run"]["id"]
    assert upload(c, run_id, "a", "ok.pdf", _pdf_bytes(2)).status_code == 201
    assert upload(c, run_id, "b", "huge.pdf", _pdf_bytes(5)).status_code == 201
    files = {f["name"]: f for f in inspected(c, run_id)}
    assert files["ok.pdf"]["pages"] == 2 and files["ok.pdf"]["problem"] is None
    assert files["huge.pdf"]["pages"] == 5 and "limit is 3" in files["huge.pdf"]["problem"]
    r = c.post(f"/api/runs/{run_id}/start")
    assert r.status_code == 400 and "huge.pdf" in r.get_json()["error"]
    # removing the file clears the block
    c.delete(f"/api/runs/{run_id}/files/{files['huge.pdf']['id']}")
    upload(c, run_id, "b", "spec.txt", b"Concrete C35")
    inspected(c, run_id)
    assert c.post(f"/api/runs/{run_id}/start").status_code == 200


def test_password_protected_pdf_is_flagged():
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "secret")
    data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    doc.close()
    c = client_for("kate")
    run_id = c.post("/api/projects", json={"name": "Locked"}).get_json()["run"]["id"]
    assert upload(c, run_id, "a", "locked.pdf", data).status_code == 201
    assert "password-protected" in inspected(c, run_id)[0]["problem"]


def test_cloned_runs_keep_inspection_results():
    c = client_for("kate")
    _project_id, run_id = new_scanned_run(c, "Clone checks")
    wait_for(c, run_id)
    clone = c.post(f"/api/runs/{run_id}/clone").get_json()
    files = c.get(f"/api/runs/{clone['id']}").get_json()["files"]
    assert files and not any(f["inspecting"] for f in files)


def test_restart_finishes_interrupted_upload_checks():
    c = client_for("kate")
    run_id = c.post("/api/projects", json={"name": "Restart"}).get_json()["run"]["id"]
    fid = upload(c, run_id, "a", "boq.txt", b"Concrete C40").get_json()["added"][0]["id"]
    inspected(c, run_id)
    api.storage.update_run_file(fid, inspected_at=None, detected_kind=None)    # as if the server died mid-check
    stale = os.path.join(jobs._scratch_root(), "scan_leftover")
    os.makedirs(stale)
    jobs.recover_after_restart()
    assert inspected(c, run_id)[0]["detected_kind"] == "document"
    assert not os.path.exists(stale)
