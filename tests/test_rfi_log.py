"""
Phase 4 tests: the project RFI log - registering revision RFIs with house
numbers, lifecycle and roles, due dates and overdue flags, client responses
and the revision they start, PDF / Word / Excel exports, importing a log.
"""

import io
from datetime import date

import pytest

from test_workspace_api import api, client_for, new_scanned_run, stub_graph, wait_for  # noqa: F401
import rfi_log
import storage


def project_with_numbering(c, name):
    project_id, run_id = new_scanned_run(c, name)
    wait_for(c, run_id)
    c.patch(f"/api/projects/{project_id}", json={"code": "TRC02", "settings": {
        "numbering": {"format": "{code}-RFI-RME-{disc}-{seq:04d}", "start": 139}, "response_days": 7}})
    return project_id, run_id


def test_register_lifecycle_and_roles():
    pm = client_for("log_pm")
    project_id, run_id = project_with_numbering(pm, "RFI log")
    eng = client_for("log_eng")
    r = eng.post(f"/api/runs/{run_id}/rfis/RFI-001/register", json={"submit": True})
    assert r.status_code == 201, r.get_json()
    e = r.get_json()
    assert e["log_no"] == "TRC02-RFI-RME-STR-0139" and e["status"] == "in_review" and e["subject"] == "Concrete grade"
    assert eng.post(f"/api/runs/{run_id}/rfis/RFI-001/register").status_code == 409        # only once
    rfi = eng.get(f"/api/runs/{run_id}").get_json()["results"]["rfi_items"][0]
    assert rfi["log_no"] == e["log_no"] and rfi["log_id"] == e["id"]

    # engineers cannot approve; managers can; issuing sets a working-day due date
    assert eng.patch(f"/api/rfi-log/{e['id']}", json={"status": "approved"}).status_code == 403
    assert pm.patch(f"/api/rfi-log/{e['id']}", json={"status": "issued"}).status_code == 409   # must be approved first
    assert pm.patch(f"/api/rfi-log/{e['id']}", json={"status": "approved"}).get_json()["approved_by_name"] == "Log_Pm"
    issued = pm.patch(f"/api/rfi-log/{e['id']}", json={"status": "issued"}).get_json()
    assert issued["due_date"] == rfi_log.add_working_days(date.today(), 7).isoformat() and not issued["overdue"]
    assert pm.patch(f"/api/rfi-log/{e['id']}", json={"content": {"subject": "x"}}).status_code == 409   # text fixed once issued

    # overdue
    pm.patch(f"/api/rfi-log/{e['id']}", json={"due_date": "2020-01-01"})
    log = pm.get(f"/api/projects/{project_id}/rfi-log").get_json()
    assert log["counts"]["overdue"] == 1 and log["entries"][0]["overdue"]
    assert log["next_number"] == "TRC02-RFI-RME-GEN-0140"

    # client response with a file -> responded -> new revision with the file added
    resp = eng.post(f"/api/rfi-log/{e['id']}/response", data={
        "response": "Use C40/50 throughout.", "response_from": "Dar",
        "files": (io.BytesIO(b"Engineer's reply: concrete C40/50"), "reply.txt")}, content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["status"] == "responded" and body["files"][0]["name"] == "reply.txt" and not body["overdue"]
    nr = eng.post(f"/api/rfi-log/{e['id']}/new-revision", json={"slot": "b"}).get_json()
    files = eng.get(f"/api/runs/{nr['run_id']}").get_json()["files"]
    assert nr["files_added"] == 1 and "reply.txt" in [f["name"] for f in files] and nr["seq"] == 2
    assert pm.patch(f"/api/rfi-log/{e['id']}", json={"status": "closed"}).get_json()["status"] == "closed"
    # the answered RFI joins the company memory
    from fleet import memory
    assert memory.count().get("issued", 0) >= 1


def test_exports_pdf_word_excel():
    pm = client_for("log_pm2")
    project_id, run_id = project_with_numbering(pm, "Exports")
    e = pm.post(f"/api/runs/{run_id}/rfis/RFI-001/register").get_json()
    pdf = pm.get(f"/api/rfi-log/{e['id']}/pdf")
    assert pdf.status_code == 200 and pdf.data[:5] == b"%PDF-"
    import pymupdf
    with pymupdf.open(stream=pdf.data, filetype="pdf") as doc:
        text = doc[0].get_text()
    assert "REQUEST FOR INFORMATION" in text and e["log_no"] in text and "Concrete grade" in text
    assert pm.get(f"/api/runs/{run_id}/rfis/RFI-001/pdf").data[:5] == b"%PDF-"
    if api.resolve_template_path():
        docx = pm.get(f"/api/rfi-log/{e['id']}/docx")
        assert docx.status_code == 200 and docx.data[:2] == b"PK"
    x = pm.get(f"/api/projects/{project_id}/rfi-log.xlsx")
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(x.data)).active
    assert ws["A3"].value == e["log_no"] and ws["B3"].value == "Concrete grade"


def test_import_existing_log_continues_numbering():
    pm = client_for("log_pm3")
    project_id, _run_id = project_with_numbering(pm, "Import")
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["RFI No.", "Subject", "Description", "Discipline"])
    ws.append(["TRC02-RFI-RME-ELEC-0150", "Feeder size to MDB-2", "Please confirm the feeder size.", "Electrical"])
    ws.append(["TRC02-RFI-RME-MECH-0151", "Chiller location", "Confirm the chiller location.", "Mechanical"])
    buf = io.BytesIO()
    wb.save(buf)
    r = pm.post(f"/api/projects/{project_id}/rfi-log/import", data={"file": (io.BytesIO(buf.getvalue()), "log.xlsx")},
                content_type="multipart/form-data")
    assert r.get_json() == {"imported": 2, "skipped": 0}
    again = pm.post(f"/api/projects/{project_id}/rfi-log/import", data={"file": (io.BytesIO(buf.getvalue()), "log.xlsx")},
                    content_type="multipart/form-data")
    assert again.get_json()["skipped"] == 2
    log = pm.get(f"/api/projects/{project_id}/rfi-log").get_json()
    assert log["next_number"].endswith("-0152")
    manual = pm.post(f"/api/projects/{project_id}/rfi-log", json={"subject": "Door schedule", "discipline": "architectural"})
    assert manual.get_json()["log_no"] == "TRC02-RFI-RME-ARCH-0152"
    eng = client_for("log_eng3")
    assert eng.post(f"/api/projects/{project_id}/rfi-log/import", data={"file": (io.BytesIO(buf.getvalue()), "log.xlsx")},
                    content_type="multipart/form-data").status_code == 403


def test_working_days_skip_the_weekend():
    thursday = date(2026, 11, 19)
    assert rfi_log.add_working_days(thursday, 1) == date(2026, 11, 22)      # Fri + Sat skipped -> Sunday
    assert rfi_log.add_working_days(thursday, 5) == date(2026, 11, 26)
