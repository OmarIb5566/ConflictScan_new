"""
Project RFI log - from draft to closed
======================================
Every RFI that leaves the company goes through the project's log:

    draft -> in_review -> approved -> issued -> responded -> closed     (void at any point)

- house numbering from the project settings ({code}-RFI-{disc}-{seq:04d}),
  continuing after an imported log; the number is fixed when the RFI enters the
  log and is printed on its Word form and PDF;
- due date when issued: the project's response time in working days
  (Friday/Saturday weekend), overdue flags afterwards;
- the client's response (text + files) is recorded; it can start a new
  revision with the response files added, and it joins the RFI memory;
- approvals and issuing are for project managers; engineers draft, submit for
  review and record responses.

    POST  /api/runs/<rid>/rfis/<no>/register      put a revision's RFI into the log (gets its number)
    GET   /api/projects/<pid>/rfi-log             the log (+ counts, overdue)
    POST  /api/projects/<pid>/rfi-log             a manual entry
    GET   /api/projects/<pid>/rfi-log.xlsx        Excel export of the log
    POST  /api/projects/<pid>/rfi-log/import      import an existing RFI log (.xlsx)
    GET/PATCH /api/rfi-log/<id>                   one entry; status changes, due date, assignee
    POST  /api/rfi-log/<id>/sync                  refresh its text from the latest revision's RFI
    POST  /api/rfi-log/<id>/response              record the client's response (+ files)
    POST  /api/rfi-log/<id>/new-revision          new revision with the response files added
    GET   /api/rfi-log/<id>/pdf | /docx           the RFI as PDF / filled Word form
    GET   /api/runs/<rid>/rfis/<no>/pdf           any revision RFI as PDF

Windows compatible.
"""

import io
import os
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from flask import abort, jsonify, request, send_file
from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

import storage
from adapters import files as file_store
from db import rfi_log, rfi_log_files
from webcore import audit, load_project, load_run, login_required, mutate_run

STATUSES = ("draft", "in_review", "approved", "issued", "responded", "closed", "void")
STATUS_LABEL = {"draft": "Draft", "in_review": "In review", "approved": "Approved", "issued": "Issued",
                "responded": "Response received", "closed": "Closed", "void": "Void"}
# from -> {to: role needed}
TRANSITIONS = {
    "draft": {"in_review": "engineer", "void": "manager"},
    "in_review": {"draft": "engineer", "approved": "manager", "void": "manager"},
    "approved": {"issued": "manager", "in_review": "manager", "void": "manager"},
    "issued": {"responded": "engineer", "void": "manager"},
    "responded": {"closed": "manager", "issued": "manager"},
    "closed": {"responded": "manager"},
    "void": {"draft": "manager"},
}
CONTENT_KEYS = ("subject", "area", "background", "discrepancy_summary", "question", "proposed_solution",
                "commercial_note", "cost_impact", "schedule_impact", "attachments", "discipline", "priority",
                "document_a_reference", "document_b_reference", "description", "points", "request_type")
WEEKEND = (4, 5)          # Friday, Saturday


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _d(row):
    return dict(row._mapping) if row is not None else None


def get_entry(entry_id: str) -> Optional[Dict[str, Any]]:
    with storage.tx() as conn:
        return _d(conn.execute(select(rfi_log).where(and_(rfi_log.c.id == entry_id, rfi_log.c.deleted_at.is_(None)))).first())


def list_entries(project_id: str) -> List[Dict[str, Any]]:
    with storage.tx() as conn:
        return [dict(r._mapping) for r in conn.execute(select(rfi_log).where(and_(
            rfi_log.c.project_id == project_id, rfi_log.c.deleted_at.is_(None))).order_by(rfi_log.c.seq))]


def entry_files(entry_id: str) -> List[Dict[str, Any]]:
    with storage.tx() as conn:
        return [dict(r._mapping) for r in conn.execute(select(rfi_log_files).where(rfi_log_files.c.log_id == entry_id)
                                                       .order_by(rfi_log_files.c.created_at))]


def update_entry(entry_id: str, **fields) -> None:
    fields["updated_at"] = storage.now()
    with storage.tx() as conn:
        conn.execute(update(rfi_log).where(rfi_log.c.id == entry_id).values(**fields))


def _delete_for_project(conn, project_id):
    ids = [r.id for r in conn.execute(select(rfi_log.c.id).where(rfi_log.c.project_id == project_id))]
    if ids:
        conn.execute(delete(rfi_log_files).where(rfi_log_files.c.log_id.in_(ids)))
        conn.execute(delete(rfi_log).where(rfi_log.c.id.in_(ids)))


storage._PROJECT_DELETE_HOOKS.append(_delete_for_project)


def add_working_days(start: date, days: int) -> date:
    d = start
    left = days
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() not in WEEKEND:
            left -= 1
    return d


def is_overdue(e: Dict[str, Any], today: Optional[date] = None) -> bool:
    return e["status"] == "issued" and bool(e.get("due_date")) and e["due_date"] < (today or date.today()).isoformat()


def _project_code(project: Dict[str, Any]) -> str:
    settings = project.get("settings") or {}
    return project.get("code") or (settings.get("rfi_form_meta") or {}).get("project_code") or "PRJ"


def create_entry(project: Dict[str, Any], user: Dict[str, Any], content: Dict[str, Any], source: str = "scan",
                 run_id: str = None, run_rfi_no: str = None, status: str = "draft", log_no: str = None,
                 seq: int = None, finding_ids=None) -> Dict[str, Any]:
    import project_settings
    settings = project.get("settings") or {}
    num = settings.get("numbering") or {}
    fmt = num.get("format") or project_settings.DEFAULT_FORMAT
    for attempt in range(6):
        with storage.tx() as conn:
            top = conn.execute(select(func.max(rfi_log.c.seq)).where(rfi_log.c.project_id == project["id"])).scalar() or 0
        this_seq = seq if seq is not None else max(top + 1, int(num.get("start") or 1))
        number = log_no or project_settings.format_number(fmt, this_seq, content.get("discipline") or "general",
                                                         _project_code(project), num.get("disciplines"))
        entry = {"id": storage.new_id(), "project_id": project["id"], "seq": this_seq, "log_no": number,
                 "run_id": run_id, "run_rfi_no": run_rfi_no, "subject": str(content.get("subject") or "")[:500],
                 "discipline": content.get("discipline"), "priority": content.get("priority"), "status": status,
                 "content": {k: content.get(k) for k in CONTENT_KEYS if k in content}, "finding_ids": finding_ids,
                 "source": source, "created_by": (user or {}).get("id"), "created_at": storage.now(),
                 "updated_at": storage.now()}
        try:
            with storage.tx() as conn:
                conn.execute(insert(rfi_log).values(**entry))
            return get_entry(entry["id"])
        except IntegrityError:
            if log_no:
                raise
            seq = None
    raise RuntimeError("Could not assign an RFI number - try again.")


def _payload(e: Dict[str, Any], users: Dict[str, str] = None) -> Dict[str, Any]:
    users = users or {}
    out = {k: v for k, v in e.items() if k != "deleted_at"}
    out["overdue"] = is_overdue(e)
    out["status_label"] = STATUS_LABEL.get(e["status"], e["status"])
    if e.get("due_date") and e["status"] == "issued":
        out["days_left"] = (date.fromisoformat(e["due_date"]) - date.today()).days
    if e.get("issued_at") and e.get("responded_at"):
        out["days_to_respond"] = (datetime.fromisoformat(e["responded_at"]) - datetime.fromisoformat(e["issued_at"])).days
    for k, name in (("assignee_id", "assignee_name"), ("created_by", "created_by_name"),
                    ("approved_by", "approved_by_name"), ("issued_by", "issued_by_name")):
        if e.get(k):
            out[name] = users.get(e[k])
    return out


def _users() -> Dict[str, str]:
    return {u["id"]: u["display_name"] for u in storage.list_users(include_inactive=True)}


# ---------------------------------------------------------------------------
# Documents: PDF and Word
# ---------------------------------------------------------------------------

def _html_escape(s: Any) -> str:
    import html
    return html.escape(str(s or "")).replace("\n", "<br>")


def render_pdf(project: Dict[str, Any], rfi: Dict[str, Any], meta: Dict[str, Any], number: str,
               status: str = "", response: str = "", issued: str = "", due: str = "") -> bytes:
    """The RFI as a one-or-more page A4 PDF (PyMuPDF, no office software needed; Arabic supported)."""
    import pymupdf
    rows = [("Project", project["name"]), ("RFI No.", number), ("Date", issued or datetime.now().strftime("%d %B %Y")),
            ("To", meta.get("to")), ("Consultant", meta.get("consultant")), ("Employer", meta.get("employer")),
            ("Discipline", (rfi.get("discipline") or "").title()), ("Location / area", rfi.get("area") or meta.get("location")),
            ("Response required by", due or rfi.get("response_required_by"))]
    head = "".join(f"<tr><th>{_html_escape(k)}</th><td dir='auto'>{_html_escape(v)}</td></tr>" for k, v in rows if v)
    attachments = rfi.get("attachments") or []
    if isinstance(attachments, str):
        attachments = [a for a in attachments.splitlines() if a.strip()]
    sections = [("Subject", rfi.get("subject")), ("Background", rfi.get("background")),
                ("Discrepancies", rfi.get("discrepancy_summary")), ("Information requested", rfi.get("question")),
                ("Proposed solution", rfi.get("proposed_solution")), ("Commercial note", rfi.get("commercial_note")),
                ("Cost impact", rfi.get("cost_impact")), ("Schedule impact", rfi.get("schedule_impact")),
                ("Attachments", "\n".join(f"- {a}" for a in attachments)), ("Response", response)]
    body = "".join(f"<h3>{_html_escape(t)}</h3><p dir='auto'>{_html_escape(v)}</p>" for t, v in sections if v)
    html = f"""<h1>REQUEST FOR INFORMATION</h1><p class='st'>{_html_escape(status)}</p>
      <table>{head}</table>{body}"""
    css = """body {font-family: sans-serif; font-size: 10pt;} h1 {font-size: 15pt; margin: 0 0 4pt 0;}
      .st {color: #666; font-size: 9pt; margin: 0 0 8pt 0;} table {border-collapse: collapse; width: 100%; margin-bottom: 10pt;}
      th {text-align: left; width: 32%; background: #eef1f5; padding: 3pt 5pt; border: 1px solid #ccd; font-weight: bold;}
      td {padding: 3pt 5pt; border: 1px solid #ccd;} h3 {font-size: 11pt; margin: 10pt 0 2pt 0; color: #1f3a5f;}
      p {margin: 0 0 4pt 0; line-height: 1.35;}"""
    story = pymupdf.Story(html=html, user_css=css)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    page = pymupdf.paper_rect("a4")
    where = page + (42, 42, -42, -52)
    more = 1
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    data = buf.getvalue()
    # page numbers + reference in the footer
    doc = pymupdf.open(stream=data, filetype="pdf")
    for i, pg in enumerate(doc, start=1):
        pg.insert_text((42, pg.rect.height - 28), f"{number}  -  page {i} of {doc.page_count}", fontsize=8, color=(0.4, 0.4, 0.4))
    out = doc.tobytes()
    doc.close()
    return out


def _entry_meta(project, entry) -> Dict[str, Any]:
    meta = dict((project.get("settings") or {}).get("rfi_form_meta") or {})
    if entry.get("run_id"):
        run = storage.get_run(entry["run_id"])
        meta.update({k: v for k, v in (((run or {}).get("inputs") or {}).get("rfi_form_meta") or {}).items() if v})
    return meta


def _fmt_date(iso: Optional[str]) -> str:
    return datetime.fromisoformat(iso).strftime("%d %B %Y") if iso else ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def register(app):
    import api

    def _load_entry(entry_id, need="viewer"):
        e = get_entry(entry_id)
        if not e:
            abort(404)
        project, role = load_project(e["project_id"], need)
        return e, project

    @app.route("/api/runs/<run_id>/rfis/<rfi_no>/register", methods=["POST"])
    @login_required
    def rfi_register(run_id, rfi_no):
        run, project = load_run(run_id, "engineer")
        api._require_results(run)
        _i, rfi = api._find_rfi(run["results"], rfi_no)
        if rfi.get("log_id") and get_entry(rfi["log_id"]):
            return jsonify({"error": f"{rfi_no} is already in the RFI log as {rfi.get('log_no')}.",
                            "log_id": rfi["log_id"]}), 409
        body = request.get_json(silent=True) or {}
        status = "in_review" if body.get("submit") else "draft"
        entry = create_entry(project, request.user, rfi, "scan", run_id, rfi_no, status,
                             finding_ids=rfi.get("discrepancy_ids"))

        def change(r):
            _idx, target = api._find_rfi(r["results"], rfi_no)
            target["log_id"], target["log_no"], target["docx_stale"] = entry["id"], entry["log_no"], True
            return {"results": r["results"]}

        mutate_run(run_id, change, request.user["id"])
        if status != "draft":
            for hook in STATUS_HOOKS:
                try:
                    hook(entry, {**entry, "status": "draft"}, request.user)
                except Exception as exc:  # noqa: BLE001
                    print(f"[rfi_log] hook failed: {exc}")
        audit("rfi_log.register", "rfi_log", entry["id"], project["id"], f"{entry['log_no']} from Rev {run['seq']} {rfi_no}")
        return jsonify(_payload(entry, _users())), 201

    @app.route("/api/projects/<project_id>/rfi-log", methods=["GET", "POST"])
    @login_required
    def project_rfi_log(project_id):
        project, role = load_project(project_id)
        if request.method == "POST":
            load_project(project_id, "engineer")
            body = request.get_json(silent=True) or {}
            if not (body.get("subject") or "").strip():
                return jsonify({"error": "Give the RFI a subject."}), 400
            entry = create_entry(project, request.user, {k: body.get(k) for k in CONTENT_KEYS if body.get(k)}, "manual")
            audit("rfi_log.create", "rfi_log", entry["id"], project_id, f"{entry['log_no']} {entry['subject'][:80]}")
            return jsonify(_payload(entry, _users())), 201
        users = _users()
        entries = [_payload(e, users) for e in list_entries(project_id)]
        counts = {s: sum(1 for e in entries if e["status"] == s) for s in STATUSES}
        counts["overdue"] = sum(1 for e in entries if e["overdue"])
        import project_settings
        num = (project.get("settings") or {}).get("numbering") or {}
        top = max([e["seq"] for e in entries] + [0])
        next_no = project_settings.format_number(num.get("format") or project_settings.DEFAULT_FORMAT,
                                                 max(top + 1, int(num.get("start") or 1)), "general",
                                                 _project_code(project), num.get("disciplines"))
        return jsonify({"entries": entries, "counts": counts, "my_role": role, "next_number": next_no,
                        "statuses": [{"key": s, "label": STATUS_LABEL[s]} for s in STATUSES],
                        "transitions": TRANSITIONS})

    @app.route("/api/rfi-log/<entry_id>", methods=["GET", "PATCH", "DELETE"])
    @login_required
    def rfi_log_item(entry_id):
        e, project = _load_entry(entry_id)
        if request.method == "DELETE":
            _load_entry(entry_id, "manager")
            if e["status"] not in ("draft", "void"):
                return jsonify({"error": "Only draft or void RFIs can be removed from the log - void it instead."}), 409
            update_entry(entry_id, deleted_at=storage.now())
            audit("rfi_log.delete", "rfi_log", entry_id, project["id"], e["log_no"])
            return jsonify({"status": "deleted"})
        if request.method == "PATCH":
            body = request.get_json(silent=True) or {}
            fields: Dict[str, Any] = {}
            new_status = body.get("status")
            if new_status and new_status != e["status"]:
                need = TRANSITIONS.get(e["status"], {}).get(new_status)
                if not need:
                    return jsonify({"error": f"An RFI that is '{STATUS_LABEL[e['status']]}' cannot become "
                                             f"'{STATUS_LABEL.get(new_status, new_status)}'."}), 409
                _load_entry(entry_id, need)
                fields["status"] = new_status
                now = storage.now()
                if new_status == "approved":
                    fields.update(approved_by=request.user["id"], approved_at=now)
                if new_status == "issued":
                    fields.update(issued_by=request.user["id"], issued_at=now)
                    if not e.get("due_date") and not body.get("due_date"):
                        days = int((project.get("settings") or {}).get("response_days") or 7)
                        fields["due_date"] = add_working_days(date.today(), days).isoformat()
                if new_status == "closed":
                    fields["closed_at"] = now
                if new_status in ("draft", "in_review"):
                    fields.update(approved_by=None, approved_at=None)
            else:
                _load_entry(entry_id, "engineer")
            if "due_date" in body:
                due = str(body["due_date"] or "")[:10]
                if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
                    return jsonify({"error": "Due date must be YYYY-MM-DD."}), 400
                fields["due_date"] = due or None
            if "assignee_id" in body:
                fields["assignee_id"] = body["assignee_id"] or None
            if "priority" in body and body["priority"] in ("high", "medium", "low"):
                fields["priority"] = body["priority"]
            if "content" in body and isinstance(body["content"], dict):
                if e["status"] not in ("draft", "in_review"):
                    return jsonify({"error": "The text of an approved or issued RFI is fixed."}), 409
                content = dict(e.get("content") or {})
                content.update({k: v for k, v in body["content"].items() if k in CONTENT_KEYS})
                fields["content"] = content
                fields["subject"] = str(content.get("subject") or e["subject"])[:500]
            if fields:
                update_entry(entry_id, **fields)
                audit("rfi_log.update", "rfi_log", entry_id, project["id"],
                      f"{e['log_no']}: " + ", ".join(f"{k}={v}" for k, v in fields.items() if k in ("status", "due_date", "assignee_id")))
                for hook in STATUS_HOOKS:
                    try:
                        hook(get_entry(entry_id), e, request.user)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[rfi_log] hook failed: {exc}")
            e = get_entry(entry_id)
        out = _payload(e, _users())
        out["files"] = [{k: f[k] for k in ("id", "kind", "name", "size", "created_at")} for f in entry_files(entry_id)]
        out["allowed"] = list(TRANSITIONS.get(e["status"], {}).keys())
        return jsonify(out)

    @app.route("/api/rfi-log/<entry_id>/sync", methods=["POST"])
    @login_required
    def rfi_log_sync(entry_id):
        """Refresh a draft/in-review entry from the newest revision RFI linked to it."""
        e, project = _load_entry(entry_id, "engineer")
        if e["status"] not in ("draft", "in_review"):
            return jsonify({"error": "Only draft or in-review RFIs follow the latest revision."}), 409
        for r in storage.list_runs(project["id"]):
            run = storage.get_run(r["id"])
            for rfi in ((run or {}).get("results") or {}).get("rfi_items") or []:
                if rfi.get("log_id") == entry_id:
                    content = {k: rfi.get(k) for k in CONTENT_KEYS if k in rfi}
                    update_entry(entry_id, content=content, subject=str(rfi.get("subject") or "")[:500],
                                 run_id=run["id"], run_rfi_no=rfi["rfi_no"], finding_ids=rfi.get("discrepancy_ids"))
                    return jsonify(_payload(get_entry(entry_id), _users()))
        return jsonify({"error": "No revision RFI is linked to this entry."}), 404

    @app.route("/api/rfi-log/<entry_id>/response", methods=["POST"])
    @login_required
    def rfi_log_response(entry_id):
        e, project = _load_entry(entry_id, "engineer")
        if e["status"] not in ("issued", "responded"):
            return jsonify({"error": "Record a response once the RFI has been issued."}), 409
        text = (request.form.get("response") or "").strip()
        if not text and not request.files.getlist("files"):
            return jsonify({"error": "Enter the response, attach its files, or both."}), 400
        store = file_store.store()
        import upload_checks
        added = []
        for up in request.files.getlist("files"):
            if not up or not up.filename:
                continue
            name = secure_filename(up.filename) or f"file_{uuid.uuid4().hex[:6]}"
            key = f"projects/{project['id']}/rfi_log/{entry_id}/{uuid.uuid4().hex[:8]}_{name}"
            path = store.local_path(key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            up.save(path)
            problem = upload_checks.sniff(path, os.path.splitext(name)[1]) if os.path.splitext(name)[1] else None
            if problem:
                os.remove(path)
                return jsonify({"error": f"'{name}' {problem}."}), 400
            store.sync(key)
            with storage.tx() as conn:
                conn.execute(insert(rfi_log_files).values(id=storage.new_id(), log_id=entry_id, kind="response",
                                                          name=name, path=key, size=os.path.getsize(path),
                                                          uploaded_by=request.user["id"], created_at=storage.now()))
            added.append(name)
        update_entry(entry_id, status="responded", responded_at=storage.now(),
                     response=text or e.get("response"), response_from=(request.form.get("response_from") or "")[:200] or None)
        audit("rfi_log.response", "rfi_log", entry_id, project["id"], f"{e['log_no']}: response recorded ({len(added)} file(s))")
        try:
            from fleet import memory
            content = e.get("content") or {}
            memory.add([{"project_name": project["name"], "rfi_no": e["log_no"], "subject": e["subject"],
                         "text": content.get("question") or content.get("description") or "",
                         "discipline": e.get("discipline"), "response": text}], source="issued")
        except Exception as exc:  # noqa: BLE001
            print(f"[rfi_log] RFI memory not updated: {exc}")
        for hook in STATUS_HOOKS:
            try:
                hook(get_entry(entry_id), e, request.user)
            except Exception as exc:  # noqa: BLE001
                print(f"[rfi_log] hook failed: {exc}")
        return rfi_log_item(entry_id)

    @app.route("/api/rfi-log/<entry_id>/files/<file_id>")
    @login_required
    def rfi_log_file(entry_id, file_id):
        e, project = _load_entry(entry_id)
        f = next((x for x in entry_files(entry_id) if x["id"] == file_id), None)
        if not f:
            abort(404)
        audit("file.download", "rfi_log", entry_id, project["id"], f["name"])
        return send_file(file_store.store().local_path(f["path"]), as_attachment=True, download_name=f["name"])

    @app.route("/api/rfi-log/<entry_id>/new-revision", methods=["POST"])
    @login_required
    def rfi_log_new_revision(entry_id):
        """Start a new revision from the latest complete one, with the response files added to a set."""
        e, project = _load_entry(entry_id, "engineer")
        slot = (request.get_json(silent=True) or {}).get("slot") or "b"
        base = next((r for r in storage.list_runs(project["id"]) if r["status"] == "complete"), None)
        if not base:
            return jsonify({"error": "The project has no completed revision to build on."}), 409
        base_run = storage.get_run(base["id"])
        inputs = dict(base_run.get("inputs") or {})
        inputs.pop("_rev", None)
        new = storage.create_run(project["id"], request.user["id"], inputs, parent_run_id=base["id"],
                                 title=f"After the response to {e['log_no']}"[:300])
        storage.copy_run_files(base["id"], new["id"])
        store = file_store.store()
        added = 0
        for f in entry_files(entry_id):
            if f["kind"] != "response":
                continue
            record, err = api.store_upload(new["id"], slot if slot in ("a", "b") else "b", store.local_path(f["path"]), f["name"])
            added += 0 if err else 1
        audit("run.from_response", "run", new["id"], project["id"], f"Rev {new['seq']} with the response to {e['log_no']}")
        return jsonify({"run_id": new["id"], "seq": new["seq"], "files_added": added}), 201

    def _pdf_response(project, content, number, entry=None):
        meta = _entry_meta(project, entry or {})
        pdf = render_pdf(project, content, meta, number, STATUS_LABEL.get((entry or {}).get("status"), "Draft"),
                         (entry or {}).get("response") or "", _fmt_date((entry or {}).get("issued_at")),
                         (entry or {}).get("due_date") or "")
        return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=request.args.get("inline") != "1",
                         download_name=f"{secure_filename(number) or 'RFI'}.pdf")

    @app.route("/api/rfi-log/<entry_id>/pdf")
    @login_required
    def rfi_log_pdf(entry_id):
        e, project = _load_entry(entry_id)
        audit("export.rfi_pdf", "rfi_log", entry_id, project["id"], e["log_no"])
        return _pdf_response(project, e.get("content") or {}, e["log_no"], e)

    @app.route("/api/runs/<run_id>/rfis/<rfi_no>/pdf")
    @login_required
    def run_rfi_pdf(run_id, rfi_no):
        run, project = load_run(run_id)
        api._require_results(run)
        _i, rfi = api._find_rfi(run["results"], rfi_no)
        entry = get_entry(rfi["log_id"]) if rfi.get("log_id") else None
        audit("export.rfi_pdf", "run", run_id, project["id"], rfi_no)
        return _pdf_response(project, rfi, rfi.get("log_no") or rfi_no, entry)

    @app.route("/api/rfi-log/<entry_id>/docx")
    @login_required
    def rfi_log_docx(entry_id):
        e, project = _load_entry(entry_id)
        template = api.template_for(project)
        if not template:
            return jsonify({"error": "No RFI form is loaded for this project."}), 400
        from nodes.docx_template import fill_docx_template
        from nodes.rfi_drafting import build_token_values
        content = dict(e.get("content") or {})
        content["rfi_no"] = e["log_no"]
        if e.get("due_date"):
            content["response_required_by"] = date.fromisoformat(e["due_date"]).strftime("%d %B %Y")
        state_like = {"project_name": project["name"], "user_id": request.user["display_name"],
                      "rfi_form_meta": _entry_meta(project, e)}
        values = build_token_values(content, state_like, "Set A", "Set B")
        values["rfi_no"] = e["log_no"]
        out = os.path.join(storage.DATA_DIR, "scratch", f"log_{uuid.uuid4().hex[:8]}.docx")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        fill_docx_template(template, out, values)
        audit("export.rfi_docx", "rfi_log", entry_id, project["id"], e["log_no"])
        return send_file(out, as_attachment=True, download_name=f"{secure_filename(e['log_no']) or 'RFI'}.docx")

    @app.route("/api/projects/<project_id>/rfi-log.xlsx")
    @login_required
    def rfi_log_xlsx(project_id):
        project, _role = load_project(project_id)
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "RFI Log"
        headers = ["RFI No.", "Subject", "Discipline", "Priority", "Status", "Issued", "Due", "Response received",
                   "Days to respond", "Overdue", "Response", "Revision", "Source"]
        ws.append([f"{project['name']} - RFI log ({datetime.now():%d %B %Y})"])
        ws["A1"].font = Font(bold=True, size=13)
        ws.append(headers)
        for c in ws[2]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1F3A5F")
        users = _users()
        runs = {r["id"]: r["seq"] for r in storage.list_runs(project_id)}
        for e in list_entries(project_id):
            p = _payload(e, users)
            ws.append([e["log_no"], e["subject"], (e.get("discipline") or "").title(), (e.get("priority") or "").title(),
                       STATUS_LABEL.get(e["status"]), _fmt_date(e.get("issued_at")),
                       date.fromisoformat(e["due_date"]).strftime("%d %B %Y") if e.get("due_date") else "",
                       _fmt_date(e.get("responded_at")), p.get("days_to_respond", ""), "YES" if p["overdue"] else "",
                       e.get("response") or "", f"Rev {runs[e['run_id']]}" if e.get("run_id") in runs else "", e["source"]])
        for col, width in zip("ABCDEFGHIJKLM", (30, 50, 14, 10, 18, 16, 16, 18, 10, 9, 60, 9, 10)):
            ws.column_dimensions[col].width = width
        for row in ws.iter_rows(min_row=3):
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A3"
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        audit("export.rfi_log", "project", project_id, project_id, "RFI log .xlsx")
        name = secure_filename(f"{project['name']}_RFI_log") or "RFI_log"
        return send_file(buf, as_attachment=True, download_name=f"{name}.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.route("/api/projects/<project_id>/rfi-log/import", methods=["POST"])
    @login_required
    def rfi_log_import(project_id):
        """Bring in the project's existing RFI log so numbering continues and the history is searchable."""
        project, _role = load_project(project_id, "manager")
        up = request.files.get("file")
        if not up or not up.filename.lower().endswith((".xlsx", ".xlsm")):
            return jsonify({"error": "Upload the RFI log as an Excel .xlsx file."}), 400
        tmp = os.path.join(storage.DATA_DIR, "scratch", f"import_{uuid.uuid4().hex[:8]}.xlsx")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        up.save(tmp)
        try:
            import sys
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
            from analyze_rfi_log import read_log
            rows = read_log(tmp)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Could not read the log ({exc})."}), 400
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not rows:
            return jsonify({"error": "No RFIs found - the log needs Subject and Description columns."}), 400
        existing = {e["log_no"] for e in list_entries(project_id)}
        added, skipped = 0, 0
        memory_items = []
        for r in rows:
            number = (r.get("number") or "").strip()
            if not number or number in existing:
                skipped += 1
                continue
            m = re.findall(r"(\d+)", number)
            seq = int(m[-1]) if m else None
            content = {"subject": r.get("subject"), "description": r.get("description"),
                       "question": r.get("description"), "discipline": (r.get("discipline") or "").lower() or None,
                       "area": r.get("area")}
            try:
                create_entry(project, request.user, content, "imported", status="issued", log_no=number[:120],
                             seq=seq if seq is not None else 0)
                existing.add(number)
                added += 1
                memory_items.append({"project_name": project["name"], "rfi_no": number, "subject": r.get("subject") or "",
                                     "text": r.get("description") or "", "discipline": r.get("discipline")})
            except IntegrityError:
                skipped += 1
        try:
            from fleet import memory
            memory.add(memory_items, source="imported")
        except Exception as exc:  # noqa: BLE001
            print(f"[rfi_log] imported RFIs not added to the memory: {exc}")
        audit("rfi_log.import", "project", project_id, project_id, f"imported {added} RFI(s), skipped {skipped}")
        return jsonify({"imported": added, "skipped": skipped})


STATUS_HOOKS: list = []      # notifications plug in here: hook(new_entry, old_entry, user)
