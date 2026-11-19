"""
Phase 5 tests: comments and @mentions, notifications (scan finished, assigned,
mentioned, waiting for review, overdue), activity, trash and restore, full-text
search, usage and the fair-use limit, scan time estimate, bulk triage,
evidence pages with highlights, thumbnails, .zip uploads and resumable uploads.
"""

import base64
import io
import os
import zipfile

import pytest

from test_workspace_api import api, client_for, inspected, new_scanned_run, stub_graph, upload, wait_for  # noqa: F401
import collab
import storage


def _pdf(text_per_page):
    import pymupdf
    doc = pymupdf.open()
    for t in text_per_page:
        doc.new_page().insert_text((72, 100), t)
    data = doc.tobytes()
    doc.close()
    return data


def notes_for(c, wait_kind=None, timeout=5):
    """Notifications of a user; optionally wait for one kind (scan notices land just after the run completes)."""
    import time
    deadline = time.time() + timeout
    while True:
        got = c.get("/api/notifications").get_json()
        if not wait_kind or any(n["kind"] == wait_kind for n in got["items"]) or time.time() > deadline:
            return got
        time.sleep(0.05)


def test_comments_mentions_and_notifications():
    alice = client_for("co_alice")
    bob = client_for("co_bob")
    project_id, run_id = new_scanned_run(alice, "Comments")
    wait_for(alice, run_id)
    assert any(n["kind"] == "scan_done" for n in notes_for(alice, "scan_done")["items"])          # scan finished
    r = alice.post("/api/comments", json={"run_id": run_id, "target_type": "finding", "target_id": "D-001",
                                         "body": "@co_bob can you check the column schedule?"})
    assert r.status_code == 201
    got = notes_for(bob)
    mention = next(n for n in got["items"] if n["kind"] == "mention")
    assert "Co_Alice mentioned you" in mention["title"] and mention["link"] == f"#/run/{run_id}/findings"
    assert got["unread"] >= 1
    thread = bob.get(f"/api/comments?run_id={run_id}&target_type=finding&target_id=D-001").get_json()
    assert thread[0]["user_name"] == "Co_Alice"
    assert bob.get(f"/api/runs/{run_id}/comment-counts").get_json() == {"finding:D-001": 1}
    assert bob.patch(f"/api/comments/{thread[0]['id']}", json={"body": "hacked"}).status_code == 403
    assert alice.patch(f"/api/comments/{thread[0]['id']}", json={"body": "edited"}).get_json()["body"] == "edited"
    after = bob.post("/api/notifications/read", json={"all": True}).get_json()
    assert after["unread"] == 0


def test_assignment_review_and_overdue_notices():
    pm = client_for("co_pm")
    eng = client_for("co_eng")
    project_id, run_id = new_scanned_run(pm, "Assign")
    wait_for(pm, run_id)
    eng_id = storage.get_user_by_username("co_eng")["id"]
    pm.patch(f"/api/runs/{run_id}/findings/D-001", json={"assignee": eng_id})
    assert any(n["kind"] == "assigned" and "D-001" in n["title"] for n in notes_for(eng)["items"])
    e = eng.post(f"/api/runs/{run_id}/rfis/RFI-001/register", json={"submit": True}).get_json()
    assert any(n["kind"] == "rfi_review" for n in notes_for(pm)["items"])          # the owner manages
    pm.patch(f"/api/rfi-log/{e['id']}", json={"status": "approved"})
    pm.patch(f"/api/rfi-log/{e['id']}", json={"status": "issued", "assignee_id": eng_id})
    pm.patch(f"/api/rfi-log/{e['id']}", json={"due_date": "2020-01-01"})
    assert collab.overdue_sweep()["overdue_notices"] >= 1
    assert collab.overdue_sweep()["overdue_notices"] == 0                          # once a day
    assert any(n["kind"] == "rfi_overdue" for n in notes_for(eng)["items"])


def test_activity_trash_and_restore():
    pm = client_for("co_trash")
    project_id, run_id = new_scanned_run(pm, "Trash me")
    wait_for(pm, run_id)
    activity = pm.get(f"/api/projects/{project_id}/activity").get_json()
    assert {"project.create", "run.start"} <= {a["action"] for a in activity} and "ip" not in activity[0]

    # an RFI, a revision and the project go to the trash and come back
    pm.delete(f"/api/runs/{run_id}/rfis/RFI-001")
    rev2 = pm.post(f"/api/runs/{run_id}/clone").get_json()
    assert pm.delete(f"/api/runs/{rev2['id']}").status_code == 200
    assert pm.get(f"/api/runs/{rev2['id']}").status_code == 404
    assert pm.delete(f"/api/projects/{project_id}").status_code == 200
    assert project_id not in {p["id"] for p in pm.get("/api/projects").get_json()}
    trash = pm.get("/api/trash").get_json()
    kinds = {(i["type"], i["id"]) for i in trash["items"]}
    assert ("project", project_id) in kinds and trash["days"] == 30
    assert pm.post(f"/api/trash/project/{project_id}/restore").status_code == 200
    trash = pm.get("/api/trash").get_json()
    kinds = {(i["type"], i["id"]) for i in trash["items"]}
    assert ("revision", rev2["id"]) in kinds and ("rfi", f"{run_id}:RFI-001") in kinds
    pm.post(f"/api/trash/revision/{rev2['id']}/restore")
    pm.post(f"/api/trash/rfi/{run_id}:RFI-001/restore")
    assert pm.get(f"/api/runs/{rev2['id']}").status_code == 200
    assert [r["rfi_no"] for r in pm.get(f"/api/runs/{run_id}").get_json()["results"]["rfi_items"]] == ["RFI-001"]
    # others cannot restore what they cannot manage
    other = client_for("co_other")
    pm.delete(f"/api/projects/{project_id}")
    assert other.post(f"/api/trash/project/{project_id}/restore").status_code == 404
    # purging after the retention period
    from sqlalchemy import update
    from db import projects
    with storage.tx() as conn:
        conn.execute(update(projects).where(projects.c.id == project_id).values(deleted_at=storage.ago(days=40)))
    assert collab.purge_trash()["projects_purged"] >= 1
    assert storage.get_project(project_id, include_deleted=True) is None


def test_full_text_search():
    c = client_for("co_search")
    project_id, run_id = new_scanned_run(c, "Searchable tower")
    wait_for(c, run_id)
    c.post("/api/comments", json={"run_id": run_id, "target_type": "finding", "target_id": "D-002", "body": "rebar spacer blocks"})
    types = {h["type"] for h in c.get("/api/search?q=concrete grade").get_json()}
    assert {"finding", "rfi"} <= types
    assert c.get("/api/search?q=searchable").get_json()[0]["type"] == "project"
    assert c.get("/api/search?q=spacer blocks").get_json()[0]["type"] == "comment"
    other = client_for("co_search2")
    c.patch(f"/api/projects/{project_id}", json={"visibility": "private"})
    assert other.get("/api/search?q=searchable").get_json() == []


def test_usage_fair_use_and_eta(monkeypatch):
    from test_platform import admin_client
    c = client_for("co_usage")
    _project_id, run_id = new_scanned_run(c, "Usage")
    wait_for(c, run_id)
    u = admin_client().get("/api/admin/usage?days=7").get_json()
    me = next(x for x in u["users"] if x["user"] == "Co_Usage")
    assert me["scans"] >= 1 and me["pages"] >= 2
    eta = c.get(f"/api/runs/{run_id}/eta").get_json()
    assert eta["pages"] >= 2 and eta["seconds"] > 60
    monkeypatch.setenv("CONFLICTSCAN_DAILY_PAGE_LIMIT", "3")
    run2 = c.post(f"/api/runs/{run_id}/clone").get_json()
    r = c.post(f"/api/runs/{run2['id']}/start")
    assert r.status_code == 429 and "daily" in r.get_json()["error"]


def test_bulk_triage():
    c = client_for("co_bulk")
    _project_id, run_id = new_scanned_run(c, "Bulk")
    wait_for(c, run_id)
    r = c.patch(f"/api/runs/{run_id}/findings", json={"ids": ["D-001", "D-002", "nope"], "status": "accepted"})
    assert r.status_code == 200 and r.get_json()["counts"]["open"] == 0
    assert c.patch(f"/api/runs/{run_id}/findings", json={"ids": ["nope"], "status": "accepted"}).status_code == 400


def test_evidence_viewer_and_thumbnails():
    c = client_for("co_ev")
    run_id = c.post("/api/projects", json={"name": "Evidence"}).get_json()["run"]["id"]
    upload(c, run_id, "a", "specs.pdf", _pdf(["General notes", "Columns shall be concrete grade C40/50 throughout."]))
    upload(c, run_id, "b", "notes.txt", b"Drawing note: concrete C35/45 for columns.")
    files = inspected(c, run_id)
    ev = c.get(f"/api/runs/{run_id}/evidence?file=specs.pdf&quote=concrete grade C40/50").get_json()
    assert ev["kind"] == "page" and ev["page"] == 2 and ev["pages"] == 2 and ev["highlighted"] >= 1
    assert base64.b64decode(ev["image"])[:4] == b"\x89PNG"
    txt = c.get(f"/api/runs/{run_id}/evidence?file=notes.txt&quote=C35/45").get_json()
    assert txt["kind"] == "text" and txt["found"] and txt["match"] == "C35/45"
    pdf_id = next(f["id"] for f in files if f["name"] == "specs.pdf")
    thumb = c.get(f"/api/runs/{run_id}/files/{pdf_id}/thumb.png")
    assert thumb.status_code == 200 and thumb.data[:4] == b"\x89PNG"


def test_zip_and_resumable_uploads():
    c = client_for("co_up")
    run_id = c.post("/api/projects", json={"name": "Uploads"}).get_json()["run"]["id"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Drawings/S-101.pdf", _pdf(["Sheet S-101"]))
        zf.writestr("Drawings/notes.txt", "Concrete C35")
        zf.writestr("__MACOSX/._junk", "x")
        zf.writestr("readme.exe", "MZ")
        zf.writestr("../evil.txt", "escape attempt")
    r = c.post(f"/api/runs/{run_id}/zip", data={"slot": "b", "file": (io.BytesIO(buf.getvalue()), "set.zip")},
               content_type="multipart/form-data")
    body = r.get_json()
    names = sorted(f["name"] for f in body["added"])
    assert names == ["Drawings_S-101.pdf", "Drawings_notes.txt", "evil.txt"] and body["skipped"] == 1
    for f in storage.list_run_files(run_id):
        assert ".." not in f["path"]

    data = _pdf([f"page {i}" for i in range(30)])
    s = c.post("/api/uploads", json={"run_id": run_id, "slot": "a", "name": "big spec.pdf", "size": len(data)}).get_json()
    half = len(data) // 2
    assert c.put(f"/api/uploads/{s['id']}?offset=0", data=data[:half]).get_json()["received"] == half
    wrong = c.put(f"/api/uploads/{s['id']}?offset=0", data=data[half:])
    assert wrong.status_code == 409 and wrong.get_json()["received"] == half          # resume from the server's position
    assert c.get(f"/api/uploads/{s['id']}").get_json()["received"] == half
    assert c.post(f"/api/uploads/{s['id']}/complete").status_code == 409            # not all bytes yet
    c.put(f"/api/uploads/{s['id']}?offset={half}", data=data[half:])
    done = c.post(f"/api/uploads/{s['id']}/complete").get_json()
    assert done["added"][0]["name"] == "big_spec.pdf"
    assert next(f for f in inspected(c, run_id) if f["name"] == "big_spec.pdf")["pages"] == 30
    other = client_for("co_up2")
    s2 = c.post("/api/uploads", json={"run_id": run_id, "slot": "a", "name": "x.pdf", "size": 10}).get_json()
    assert other.get(f"/api/uploads/{s2['id']}").status_code == 404
