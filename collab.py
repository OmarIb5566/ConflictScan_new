"""
Collaboration and everyday conveniences
=======================================
    comments       on findings, RFIs, RFI-log entries and revisions; @mentions notify people
    notifications  in-app bell: scan finished/failed, assigned to you, mentioned, RFI waiting for
                   review, RFI overdue, RFI status changed
    activity       a project's history (from the audit log), for everyone who can see the project
    trash          deleted projects, revisions and RFIs can be restored for CONFLICTSCAN_RETAIN_TRASH_DAYS
    search         full text over projects, revisions, findings, RFIs, the RFI log and comments
    usage          admin dashboard: scans, pages and model time per person and project; fair-use limit
    evidence       a source document's page with the quoted words highlighted
    thumbnails     first-page previews of uploaded files
    uploads        .zip archives unpacked into a set; large files in resumable chunks
    bulk           triage many findings at once
    eta            how long a scan will take, from past scans on this server

Windows compatible.
"""

import base64
import io
import os
import re
import shutil
import statistics
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from flask import abort, jsonify, request, send_file
from sqlalchemy import and_, delete, func, insert, or_, select, update
from werkzeug.utils import secure_filename

import jobs
import storage
from adapters import files as file_store
from db import comments, model_calls, notifications, rfi_log, run_files, runs, upload_sessions
from webcore import admin_required, audit, load_project, load_run, login_required, mutate_run

MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9._-]{3,40})")
CHUNK_LIMIT = 16 * 1024 * 1024


def _rows(stmt) -> List[Dict[str, Any]]:
    with storage.tx() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt)]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(user_ids: Iterable[str], kind: str, title: str, body: str = "", link: str = "", project_id: str = None,
           dedupe: str = None, exclude: str = None) -> int:
    n = 0
    stamp = storage.now()
    with storage.tx() as conn:
        for uid in {u for u in user_ids if u and u != exclude}:
            if dedupe and conn.execute(select(func.count()).select_from(notifications).where(and_(
                    notifications.c.user_id == uid, notifications.c.dedupe == dedupe))).scalar_one():
                continue
            conn.execute(insert(notifications).values(id=storage.new_id(), user_id=uid, kind=kind, title=title[:300],
                                                      body=(body or "")[:1000], link=link[:300], project_id=project_id,
                                                      dedupe=dedupe, created_at=stamp))
            n += 1
    return n


def project_managers(project: Dict[str, Any]) -> List[str]:
    ids = {project["owner_id"]}
    ids |= {m["user_id"] for m in storage.list_members(project["id"]) if m["role"] == "manager"}
    return [u for u in ids if (storage.get_user(u) or {}).get("status") == "active"]


def _after_scan(run_id: str, outcome: str) -> None:
    run = storage.get_run(run_id)
    if not run:
        return
    project = storage.get_project(run["project_id"]) or {}
    job = storage.active_job_for(run_id) or {}
    starters = {run.get("created_by"), run.get("updated_by"), job.get("created_by")}
    counts = storage.run_counts(run.get("results"), run.get("review"))
    if outcome == "complete":
        verdict = (run.get("results") or {}).get("assessment") or {}
        title = f"Scan finished: {project.get('name')} Rev {run['seq']}" + (f" - {verdict['label']}" if verdict.get("label") else "")
        body = verdict.get("headline") or f"{counts['findings']} finding(s), {counts['rfis']} RFI(s)"
        notify(starters, "scan_done", title, body,
               f"#/run/{run_id}/{'scan' if verdict.get('verdict') == 'insufficient_input' else 'findings'}", project.get("id"))
    elif outcome == "failed":
        notify(starters, "scan_failed", f"Scan failed: {project.get('name')} Rev {run['seq']}",
               (run.get("error") or "")[:300], f"#/run/{run_id}/scan", project.get("id"))


jobs._AFTER_SCAN.append(_after_scan)


def _rfi_status_hook(new: Dict[str, Any], old: Dict[str, Any], user: Dict[str, Any]) -> None:
    project = storage.get_project(new["project_id"]) or {}
    link = f"#/rfilog/{new['project_id']}"
    if new.get("assignee_id") and new.get("assignee_id") != old.get("assignee_id"):
        notify([new["assignee_id"]], "assigned", f"{new['log_no']} was assigned to you", new["subject"], link,
               project.get("id"), exclude=user["id"])
    if new["status"] != old["status"]:
        if new["status"] == "in_review":
            notify(project_managers(project), "rfi_review", f"{new['log_no']} is waiting for your review", new["subject"],
                   link, project.get("id"), exclude=user["id"])
        else:
            import rfi_log
            notify({new.get("created_by"), new.get("assignee_id")}, "rfi_status",
                   f"{new['log_no']}: {rfi_log.STATUS_LABEL.get(new['status'], new['status'])}", new["subject"], link,
                   project.get("id"), exclude=user["id"])


def overdue_sweep() -> Dict[str, int]:
    """Daily: tell the assignee (or author) and the managers about issued RFIs past their due date."""
    import rfi_log as rl
    today = date.today().isoformat()
    sent = 0
    for e in _rows(select(rfi_log).where(and_(rfi_log.c.status == "issued", rfi_log.c.deleted_at.is_(None),
                                              rfi_log.c.due_date < today))):
        project = storage.get_project(e["project_id"])
        if not project:
            continue
        to = {e.get("assignee_id") or e.get("created_by")} | set(project_managers(project))
        sent += notify(to, "rfi_overdue", f"{e['log_no']} is overdue (due {e['due_date']})", e["subject"],
                       f"#/rfilog/{e['project_id']}", e["project_id"], dedupe=f"overdue:{e['id']}:{today}")
    return {"overdue_notices": sent}


# ---------------------------------------------------------------------------
# Trash (soft delete, restore within the retention period)
# ---------------------------------------------------------------------------

def trash_days() -> int:
    try:
        return int(os.environ.get("CONFLICTSCAN_RETAIN_TRASH_DAYS", "30"))
    except ValueError:
        return 30


def _soft_delete_project(project, user):
    storage.update_project(project["id"], deleted_at=storage.now(), deleted_by=user["id"])
    audit("project.trash", "project", project["id"], project["id"], f"moved project {project['name'][:80]} to the trash")


def _soft_delete_run(run, project, user):
    storage.update_run(run["id"], user_id=user["id"], deleted_at=storage.now(), deleted_by=user["id"])
    audit("run.trash", "run", run["id"], project["id"], f"moved Rev {run['seq']} to the trash")


def _rfi_deleted(run, project, rfi):
    def change(r):
        results = r["results"]
        results.setdefault("deleted_rfis", []).append({**rfi, "deleted_at": storage.now(),
                                                       "deleted_by": request.user["id"]})
        return {"results": results}
    mutate_run(run["id"], change, request.user["id"])


def purge_trash() -> Dict[str, int]:
    days = trash_days()
    if not days:
        return {}
    cutoff = storage.ago(days=days)
    out = {"projects_purged": 0, "revisions_purged": 0}
    from db import projects
    for p in _rows(select(projects.c.id).where(and_(projects.c.deleted_at.isnot(None), projects.c.deleted_at < cutoff))):
        storage.delete_project(p["id"])
        out["projects_purged"] += 1
    for r in _rows(select(runs.c.id).where(and_(runs.c.deleted_at.isnot(None), runs.c.deleted_at < cutoff))):
        storage.delete_run(r["id"])
        out["revisions_purged"] += 1
    return out


# ---------------------------------------------------------------------------
# Usage, fair use, ETA
# ---------------------------------------------------------------------------

def _pages_of_run(run_id: str) -> int:
    with storage.tx() as conn:
        return int(conn.execute(select(func.coalesce(func.sum(func.coalesce(run_files.c.pages, 1)), 0))
                                .where(run_files.c.run_id == run_id)).scalar_one() or 0)


def daily_page_limit() -> int:
    try:
        return int(os.environ.get("CONFLICTSCAN_DAILY_PAGE_LIMIT", "0") or 0)
    except ValueError:
        return 0


def _fair_use(run, project, user, files) -> Optional[str]:
    limit = daily_page_limit()
    if not limit or user.get("role") == "admin":
        return None
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).isoformat(timespec="seconds")
    started = [a["target_id"] for a in storage.list_audit(user_id=user["id"], action="run.start", since=since, limit=1000)]
    used = sum(_pages_of_run(rid) for rid in set(started))
    this = sum(int(f.get("pages") or 1) for f in files)
    if used + this > limit:
        return (f"This scan has {this} page(s); you have used {used} of your {limit} pages today. "
                f"Ask an administrator to raise the daily limit, or scan fewer pages.")
    return None


def estimate(run_id: str) -> Dict[str, Any]:
    """Seconds this scan should take, from the seconds-per-page of recent scans on this server."""
    pages = max(1, _pages_of_run(run_id))
    rates = []
    for r in _rows(select(runs.c.id, runs.c.results).where(runs.c.status == "complete")
                   .order_by(runs.c.finished_at.desc()).limit(30)):
        dur = (r["results"] or {}).get("duration_seconds")
        p = _pages_of_run(r["id"])
        if dur and p:
            rates.append(dur / p)
    per_page = statistics.median(rates) if len(rates) >= 3 else 25.0
    seconds = int(60 + per_page * pages)
    ahead = jobs.queue_position(run_id)
    wait = 0
    if ahead > 1:
        queued = [j["target_id"] for j in storage.list_jobs(("queued",), kind="scan")][:ahead - 1]
        wait = int(sum(60 + per_page * max(1, _pages_of_run(t)) for t in queued) / max(1, jobs.WORKERS))
    return {"pages": pages, "seconds": seconds, "queue_wait_seconds": wait, "seconds_per_page": round(per_page, 1),
            "based_on": len(rates)}


# ---------------------------------------------------------------------------
# Evidence and thumbnails
# ---------------------------------------------------------------------------

def _run_file_by_name(run_id: str, name: str) -> Optional[Dict[str, Any]]:
    files = storage.list_run_files(run_id)
    name = (name or "").strip()
    for f in files:
        if f["name"] == name:
            return f
    low = name.lower()
    return next((f for f in files if f["name"].lower() == low or low.endswith(f["name"].lower())
                 or f["name"].lower().endswith(low)), None)


def _page_hint(reference: str) -> Optional[int]:
    m = re.search(r"(?:page|p\.?|pg|sheet)\s*(\d+)", reference or "", re.I)
    return int(m.group(1)) if m else None


def _render_page(path: str, page_no: int, quote: str, zoom: float = 1.6):
    import pymupdf
    with pymupdf.open(path) as doc:
        n = doc.page_count
        found_on = None
        quote = re.sub(r"\s+", " ", (quote or "").strip())
        needles = [quote[:60], quote[:30]] if quote else []
        if not page_no and needles:
            for i, pg in enumerate(doc, start=1):
                if any(nd and pg.search_for(nd) for nd in needles):
                    found_on = i
                    break
        page_no = min(max(1, page_no or found_on or 1), n)
        pg = doc[page_no - 1]
        hits = 0
        for nd in needles:
            quads = pg.search_for(nd, quads=True) if nd else []
            if quads:
                for q in quads:
                    pg.add_highlight_annot(q)
                hits = len(quads)
                break
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), annots=True)
        return {"page": page_no, "pages": n, "highlighted": hits, "png": pix.tobytes("png")}


def thumbnail(f: Dict[str, Any]) -> Optional[str]:
    store = file_store.store()
    key = storage.run_key(f["run_id"], "thumbs", f"{f['id']}.png")
    if store.exists(key):
        return store.local_path(key)
    src = store.local_path(f["path"])
    ext = os.path.splitext(f["name"])[1].lower()
    try:
        import pymupdf
        if ext == ".pdf":
            with pymupdf.open(src) as doc:
                pg = doc[0]
                pix = pg.get_pixmap(matrix=pymupdf.Matrix(160 / max(pg.rect.width, 1), 160 / max(pg.rect.width, 1)))
                data = pix.tobytes("png")
        elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"):
            from PIL import Image
            with Image.open(src) as img:
                img.thumbnail((160, 160))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "PNG")
                data = buf.getvalue()
        else:
            return None
    except Exception:  # noqa: BLE001
        return None
    path = store.local_path(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    store.sync(key)
    return path


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search(user: Dict[str, Any], q: str, limit: int = 40) -> List[Dict[str, Any]]:
    terms = [t for t in re.findall(r"\S+", q.lower()) if len(t) >= 2]
    if not terms:
        return []

    def hit(*parts) -> bool:
        hay = " ".join(str(p or "") for p in parts).lower()
        return all(t in hay for t in terms)

    out: List[Dict[str, Any]] = []
    projects = storage.list_projects_for(user)
    pids = [p["id"] for p in projects]
    names = {p["id"]: p["name"] for p in projects}
    for p in projects:
        if hit(p["name"], p.get("description"), p.get("code")):
            out.append({"type": "project", "title": p["name"], "sub": p.get("code") or "", "link": f"#/project/{p['id']}"})
    if not pids:
        return out
    seen_findings = set()
    for r in _rows(select(runs.c.id, runs.c.project_id, runs.c.seq, runs.c.title, runs.c.results)
                   .where(and_(runs.c.project_id.in_(pids), runs.c.deleted_at.is_(None))).order_by(runs.c.seq.desc())):
        pname = names.get(r["project_id"], "")
        if r["title"] and hit(r["title"], pname):
            out.append({"type": "revision", "title": f"{pname} · Rev {r['seq']}", "sub": r["title"], "link": f"#/run/{r['id']}"})
        res = r["results"] or {}
        for d in res.get("discrepancies") or []:
            key = (r["project_id"], d.get("topic"))
            if key in seen_findings:
                continue
            ev = " ".join(str(e.get("says") or "") for e in d.get("evidence") or [])
            if hit(d.get("id"), d.get("topic"), d.get("description"), d.get("location"), d.get("document_a_says"),
                   d.get("document_b_says"), ev):
                seen_findings.add(key)
                out.append({"type": "finding", "title": d.get("topic"), "sub": f"{pname} · Rev {r['seq']} · {d.get('id')}",
                            "link": f"#/run/{r['id']}/findings", "finding": d.get("id"), "run_id": r["id"]})
        for x in res.get("rfi_items") or []:
            if hit(x.get("rfi_no"), x.get("log_no"), x.get("subject"), x.get("question"), x.get("discrepancy_summary")):
                out.append({"type": "rfi", "title": x.get("subject"), "sub": f"{pname} · Rev {r['seq']} · {x.get('log_no') or x.get('rfi_no')}",
                            "link": f"#/run/{r['id']}/rfis", "rfi": x.get("rfi_no"), "run_id": r["id"]})
        if len(out) >= limit * 2:
            break
    for e in _rows(select(rfi_log).where(and_(rfi_log.c.project_id.in_(pids), rfi_log.c.deleted_at.is_(None)))):
        c = e.get("content") or {}
        if hit(e["log_no"], e["subject"], e.get("response"), c.get("question"), c.get("description")):
            out.append({"type": "rfi_log", "title": f"{e['log_no']} - {e['subject']}", "sub": names.get(e["project_id"], ""),
                        "link": f"#/rfilog/{e['project_id']}", "entry": e["id"], "project_id": e["project_id"]})
    for c in _rows(select(comments).where(and_(comments.c.project_id.in_(pids), comments.c.deleted_at.is_(None)))
                   .order_by(comments.c.created_at.desc()).limit(2000)):
        if hit(c["body"]):
            link = f"#/run/{c['run_id']}/findings" if c.get("run_id") else f"#/rfilog/{c['project_id']}"
            out.append({"type": "comment", "title": c["body"][:120], "sub": names.get(c["project_id"], ""), "link": link})
    order = {"project": 0, "rfi_log": 1, "rfi": 2, "finding": 3, "revision": 4, "comment": 5}
    out.sort(key=lambda x: order.get(x["type"], 9))
    return out[:limit]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _comment_payload(c, users):
    return {**{k: c.get(k) for k in ("id", "project_id", "run_id", "target_type", "target_id", "user_id", "body",
                                 "created_at", "edited_at")}, "user_name": users.get(c["user_id"], "?")}


def register(app):
    import api
    import maintenance
    import rfi_log as rl

    api._delete_project = _soft_delete_project
    api._delete_run = _soft_delete_run
    api.RFI_DELETE_HOOKS.append(_rfi_deleted)
    api.START_CHECKS.append(_fair_use)
    api.FINDING_FIELDS = ("assignee",)
    rl.STATUS_HOOKS.append(_rfi_status_hook)
    if overdue_sweep not in maintenance.EXTRA_STEPS:
        maintenance.EXTRA_STEPS.extend([purge_trash, overdue_sweep])

    def users_map():
        return {u["id"]: u["display_name"] for u in storage.list_users(include_inactive=True)}

    # ---- comments -----------------------------------------------------------
    def _target_project(body_or_args) -> Dict[str, Any]:
        if body_or_args.get("run_id"):
            run, project = load_run(body_or_args["run_id"])
            return project
        if body_or_args.get("target_type") == "rfi_log":
            e = rl.get_entry(body_or_args.get("target_id") or "")
            if not e:
                abort(404)
            project, _r = load_project(e["project_id"])
            return project
        project, _r = load_project(body_or_args.get("project_id") or "")
        return project

    @app.route("/api/comments", methods=["GET", "POST"])
    @login_required
    def comments_collection():
        if request.method == "GET":
            args = request.args
            project = _target_project(args)
            stmt = select(comments).where(and_(comments.c.project_id == project["id"], comments.c.deleted_at.is_(None)))
            if args.get("run_id"):
                stmt = stmt.where(comments.c.run_id == args["run_id"])
            if args.get("target_type"):
                stmt = stmt.where(comments.c.target_type == args["target_type"])
            if args.get("target_id"):
                stmt = stmt.where(comments.c.target_id == args["target_id"])
            users = users_map()
            return jsonify([_comment_payload(c, users) for c in _rows(stmt.order_by(comments.c.created_at))])
        body = request.get_json(silent=True) or {}
        text = (body.get("body") or "").strip()
        if not text:
            return jsonify({"error": "Write something first."}), 400
        if body.get("target_type") not in ("finding", "rfi", "rfi_log", "run"):
            return jsonify({"error": "Unknown comment target."}), 400
        project = _target_project(body)
        mentioned = []
        for name in set(MENTION.findall(text)):
            u = storage.get_user_by_username(name)
            if u and u["status"] == "active" and storage.project_role(project, u):
                mentioned.append(u)
        c = {"id": storage.new_id(), "project_id": project["id"], "run_id": body.get("run_id"),
             "target_type": body["target_type"], "target_id": str(body.get("target_id") or "")[:64],
             "user_id": request.user["id"], "body": text[:8000], "mentions": [u["id"] for u in mentioned],
             "created_at": storage.now()}
        with storage.tx() as conn:
            conn.execute(insert(comments).values(**c))
        link = {"finding": f"#/run/{c['run_id']}/findings", "rfi": f"#/run/{c['run_id']}/rfis",
                "run": f"#/run/{c['run_id']}", "rfi_log": f"#/rfilog/{project['id']}"}[c["target_type"]]
        notify([u["id"] for u in mentioned], "mention",
               f"{request.user['display_name']} mentioned you on {c['target_type'].replace('_', ' ')} {c['target_id']}",
               text[:300], link, project["id"], exclude=request.user["id"])
        audit("comment.add", c["target_type"], c["target_id"], project["id"], text[:120])
        return jsonify(_comment_payload(c, users_map())), 201

    @app.route("/api/comments/<comment_id>", methods=["PATCH", "DELETE"])
    @login_required
    def comment_item(comment_id):
        rows = _rows(select(comments).where(and_(comments.c.id == comment_id, comments.c.deleted_at.is_(None))))
        if not rows:
            abort(404)
        c = rows[0]
        project, role = load_project(c["project_id"])
        if c["user_id"] != request.user["id"] and not storage.can(role, "manager"):
            return jsonify({"error": "You can only change your own comments."}), 403
        with storage.tx() as conn:
            if request.method == "DELETE":
                conn.execute(update(comments).where(comments.c.id == comment_id).values(deleted_at=storage.now()))
                return jsonify({"status": "deleted"})
            text = ((request.get_json(silent=True) or {}).get("body") or "").strip()
            if not text:
                return jsonify({"error": "A comment cannot be empty."}), 400
            conn.execute(update(comments).where(comments.c.id == comment_id).values(body=text[:8000], edited_at=storage.now()))
        c.update(body=text, edited_at=storage.now())
        return jsonify(_comment_payload(c, users_map()))

    @app.route("/api/runs/<run_id>/comment-counts")
    @login_required
    def comment_counts(run_id):
        load_run(run_id)
        with storage.tx() as conn:
            rows = conn.execute(select(comments.c.target_type, comments.c.target_id, func.count())
                                .where(and_(comments.c.run_id == run_id, comments.c.deleted_at.is_(None)))
                                .group_by(comments.c.target_type, comments.c.target_id)).all()
        return jsonify({f"{r[0]}:{r[1]}": r[2] for r in rows})

    # ---- notifications ------------------------------------------------------
    @app.route("/api/notifications")
    @login_required
    def notifications_list():
        uid = request.user["id"]
        rows = _rows(select(notifications).where(notifications.c.user_id == uid)
                     .order_by(notifications.c.created_at.desc()).limit(60))
        with storage.tx() as conn:
            unread = conn.execute(select(func.count()).select_from(notifications).where(and_(
                notifications.c.user_id == uid, notifications.c.read_at.is_(None)))).scalar_one()
        return jsonify({"unread": unread, "items": [{k: n[k] for k in ("id", "kind", "title", "body", "link", "created_at", "read_at")}
                                                    for n in rows]})

    @app.route("/api/notifications/read", methods=["POST"])
    @login_required
    def notifications_read():
        body = request.get_json(silent=True) or {}
        cond = and_(notifications.c.user_id == request.user["id"], notifications.c.read_at.is_(None))
        if not body.get("all"):
            cond = and_(cond, notifications.c.id.in_(list(body.get("ids") or []) or [""]))
        with storage.tx() as conn:
            conn.execute(update(notifications).where(cond).values(read_at=storage.now()))
        return notifications_list()

    # ---- activity -----------------------------------------------------------
    @app.route("/api/projects/<project_id>/activity")
    @login_required
    def project_activity(project_id):
        load_project(project_id)
        rows = storage.list_audit(project_id=project_id, limit=int(request.args.get("limit", 100)),
                                  before=request.args.get("before") or None)
        return jsonify([{k: r[k] for k in ("id", "at", "username", "action", "summary", "target_type", "target_id")}
                        for r in rows])

    # ---- trash --------------------------------------------------------------
    @app.route("/api/trash")
    @login_required
    def trash_list():
        user = request.user
        admin = user["role"] == "admin"
        items = []
        for p in storage.list_projects_for(user, include_deleted=True):
            if admin or p["owner_id"] == user["id"] or p.get("deleted_by") == user["id"] or p["my_role"] == "manager":
                items.append({"type": "project", "id": p["id"], "title": p["name"], "deleted_at": p["deleted_at"],
                              "purge_after": _purge_date(p["deleted_at"])})
        visible = {p["id"]: p for p in storage.list_projects_for(user)}
        deleted_runs = _rows(select(runs.c.id, runs.c.project_id, runs.c.seq, runs.c.title, runs.c.deleted_at, runs.c.deleted_by)
                             .where(and_(runs.c.deleted_at.isnot(None), runs.c.project_id.in_(list(visible) or [""]))))
        for r in deleted_runs:
            p = visible[r["project_id"]]
            if admin or r["deleted_by"] == user["id"] or storage.can(p["my_role"], "manager"):
                items.append({"type": "revision", "id": r["id"], "title": f"{p['name']} · Rev {r['seq']}",
                              "sub": r["title"], "deleted_at": r["deleted_at"], "purge_after": _purge_date(r["deleted_at"])})
        for r in _rows(select(runs.c.id, runs.c.project_id, runs.c.seq, runs.c.results)
                       .where(and_(runs.c.deleted_at.is_(None), runs.c.project_id.in_(list(visible) or [""])))):
            for x in (r["results"] or {}).get("deleted_rfis") or []:
                if x.get("deleted_at", "") >= storage.ago(days=trash_days()) and storage.can(visible[r["project_id"]]["my_role"], "engineer"):
                    items.append({"type": "rfi", "id": f"{r['id']}:{x['rfi_no']}", "title": f"{x['rfi_no']} - {x.get('subject')}",
                                  "sub": f"{visible[r['project_id']]['name']} · Rev {r['seq']}", "deleted_at": x["deleted_at"],
                                  "purge_after": _purge_date(x["deleted_at"])})
        items.sort(key=lambda i: i["deleted_at"] or "", reverse=True)
        return jsonify({"days": trash_days(), "items": items})

    @app.route("/api/trash/<kind>/<path:item_id>/restore", methods=["POST"])
    @login_required
    def trash_restore(kind, item_id):
        user = request.user
        if kind == "project":
            p = storage.get_project(item_id, include_deleted=True)
            if not p or not p.get("deleted_at"):
                abort(404)
            role = storage.project_role(p, user)
            if not (user["role"] == "admin" or p.get("deleted_by") == user["id"] or storage.can(role, "manager")):
                abort(404)
            storage.update_project(item_id, deleted_at=None, deleted_by=None)
            audit("project.restore", "project", item_id, item_id, f"restored project {p['name'][:80]}")
        elif kind == "revision":
            r = storage.get_run(item_id, include_deleted=True)
            if not r or not r.get("deleted_at"):
                abort(404)
            project, role = load_project(r["project_id"], "engineer")
            storage.update_run(item_id, user_id=user["id"], deleted_at=None, deleted_by=None)
            audit("run.restore", "run", item_id, project["id"], f"restored Rev {r['seq']}")
        elif kind == "rfi":
            run_id, _, rfi_no = item_id.partition(":")
            run, project = load_run(run_id, "engineer")
            holder = {}

            def change(rr):
                res = rr["results"]
                gone = res.get("deleted_rfis") or []
                x = next((g for g in gone if g.get("rfi_no") == rfi_no), None)
                if not x:
                    abort(404)
                gone.remove(x)
                x.pop("deleted_at", None)
                x.pop("deleted_by", None)
                if any(i.get("rfi_no") == rfi_no for i in res.get("rfi_items") or []):
                    x["rfi_no"] = api._next_rfi_no(res.get("rfi_items") or [])
                res.setdefault("rfi_items", []).append(x)
                holder["rfi"] = x
                return {"results": res}

            mutate_run(run_id, change, user["id"])
            audit("rfi.restore", "rfi", rfi_no, project["id"], f"restored {holder['rfi']['rfi_no']}")
        else:
            abort(404)
        return jsonify({"status": "restored"})

    # ---- search, usage, eta ---------------------------------------------------
    @app.route("/api/search")
    @login_required
    def search_route():
        return jsonify(search(request.user, request.args.get("q", ""), int(request.args.get("limit", 40))))

    @app.route("/api/admin/usage")
    @admin_required
    def usage():
        days = max(1, min(365, int(request.args.get("days", 30))))
        since = storage.ago(days=days)
        users = users_map()
        started = storage.list_audit(action="run.start", since=since, limit=1000)
        per_user: Dict[str, Dict[str, Any]] = {}
        per_project: Dict[str, Dict[str, Any]] = {}
        run_owner = {}
        for a in started:
            run_owner[a["target_id"]] = a["user_id"]
            pages = _pages_of_run(a["target_id"])
            u = per_user.setdefault(a["user_id"], {"user": users.get(a["user_id"], "?"), "scans": 0, "pages": 0, "model_minutes": 0.0})
            u["scans"] += 1
            u["pages"] += pages
            p = per_project.setdefault(a["project_id"], {"project_id": a["project_id"], "scans": 0, "pages": 0, "model_minutes": 0.0})
            p["scans"] += 1
            p["pages"] += pages
        with storage.tx() as conn:
            calls = conn.execute(select(model_calls.c.run_id, func.sum(model_calls.c.ms), func.count())
                                 .where(and_(model_calls.c.at >= since, model_calls.c.run_id.isnot(None)))
                                 .group_by(model_calls.c.run_id)).all()
            daily = conn.execute(select(model_calls.c.at, model_calls.c.ms).where(model_calls.c.at >= since)).all()
        run_project = {r["id"]: r["project_id"] for r in _rows(select(runs.c.id, runs.c.project_id))}
        for run_id, ms, _n in calls:
            minutes = (ms or 0) / 60000
            owner = run_owner.get(run_id)
            if owner in per_user:
                per_user[owner]["model_minutes"] += minutes
            pid = run_project.get(run_id)
            if pid in per_project:
                per_project[pid]["model_minutes"] += minutes
        names = {}
        for pid in per_project:
            p = storage.get_project(pid, include_deleted=True)
            names[pid] = p["name"] if p else "(deleted)"
        series: Dict[str, float] = {}
        for at, ms in daily:
            day = str(at)[:10]
            series[day] = series.get(day, 0.0) + (ms or 0) / 60000
        for v in list(per_user.values()) + list(per_project.values()):
            v["model_minutes"] = round(v["model_minutes"], 1)
        return jsonify({
            "days": days, "daily_page_limit": daily_page_limit(),
            "users": sorted(per_user.values(), key=lambda x: -x["pages"]),
            "projects": sorted([{**v, "name": names[k]} for k, v in per_project.items()], key=lambda x: -x["pages"]),
            "daily_model_minutes": [{"day": d, "minutes": round(m, 1)} for d, m in sorted(series.items())],
            "totals": {"scans": len(started), "pages": sum(u["pages"] for u in per_user.values()),
                       "model_minutes": round(sum(m for m in series.values()), 1)},
        })

    @app.route("/api/runs/<run_id>/eta")
    @login_required
    def run_eta(run_id):
        load_run(run_id)
        return jsonify(estimate(run_id))

    # ---- bulk triage -----------------------------------------------------------
    @app.route("/api/runs/<run_id>/findings", methods=["PATCH"])
    @login_required
    def findings_bulk(run_id):
        run, project = load_run(run_id, "engineer")
        api._require_results(run)
        body = request.get_json(silent=True) or {}
        ids = [str(i) for i in body.get("ids") or []]
        valid = {d.get("id") for d in run["results"].get("discrepancies") or []}
        ids = [i for i in ids if i in valid]
        if not ids:
            return jsonify({"error": "Select one or more findings."}), 400
        assignee = body.get("assignee") if "assignee" in body else None
        holder = {}

        def change(r):
            review = r.get("review") or {}
            f = review.setdefault("findings", {})
            for i in ids:
                entry = f.setdefault(i, {})
                if body.get("status") in api.FINDING_STATUSES:
                    entry["status"] = body["status"]
                if body.get("severity") in ("critical", "major", "minor"):
                    entry["severity"] = body["severity"]
                if "assignee" in body:
                    entry["assignee"] = assignee or None
                entry["by"] = request.user["display_name"]
                entry["at"] = storage.now()
            holder["review"], holder["results"] = review, r.get("results")
            return {"review": review}

        mutate_run(run_id, change, request.user["id"])
        if assignee:
            notify([assignee], "assigned", f"{len(ids)} finding(s) of {project['name']} Rev {run['seq']} assigned to you",
                   ", ".join(ids[:8]), f"#/run/{run_id}/findings", project["id"], exclude=request.user["id"])
        audit("finding.bulk", "run", run_id, project["id"], f"Rev {run['seq']}: {len(ids)} finding(s) "
              + ", ".join(f"{k}={body[k]}" for k in ("status", "severity", "assignee") if k in body))
        return jsonify({"review": holder["review"], "counts": storage.run_counts(holder["results"], holder["review"])})

    # Single-finding assignment notifies too.
    _orig = app.view_functions["finding_review"]

    def finding_review_with_notice(run_id, disc_id):
        resp = _orig(run_id, disc_id)
        body = request.get_json(silent=True) or {}
        if body.get("assignee"):
            run = storage.get_run(run_id)
            project = storage.get_project(run["project_id"])
            notify([body["assignee"]], "assigned", f"{disc_id} of {project['name']} Rev {run['seq']} was assigned to you",
                   "", f"#/run/{run_id}/findings", project["id"], exclude=request.user["id"])
        return resp
    app.view_functions["finding_review"] = finding_review_with_notice

    # ---- evidence and thumbnails ----------------------------------------------
    @app.route("/api/runs/<run_id>/evidence")
    @login_required
    def evidence(run_id):
        run, project = load_run(run_id)
        f = _run_file_by_name(run_id, request.args.get("file", ""))
        if not f:
            return jsonify({"error": "That source file is not part of this revision."}), 404
        path = storage.file_path(f)
        quote = request.args.get("quote", "")
        page = int(request.args.get("page") or 0) or _page_hint(request.args.get("reference", ""))
        ext = os.path.splitext(f["name"])[1].lower()
        if ext == ".pdf":
            try:
                out = _render_page(path, page, quote)
            except Exception as exc:  # noqa: BLE001
                return jsonify({"error": f"Could not open the PDF ({exc})."}), 500
            return jsonify({"kind": "page", "file": f["name"], "file_id": f["id"], "page": out["page"], "pages": out["pages"],
                            "highlighted": out["highlighted"], "image": base64.b64encode(out["png"]).decode("ascii")})
        if ext in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"):
            with open(path, "rb") as fh:
                return jsonify({"kind": "image", "file": f["name"], "file_id": f["id"],
                                "image": base64.b64encode(fh.read()).decode("ascii")})
        try:
            text = _plain_text(path, ext)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Could not read the file ({exc})."}), 500
        idx = text.lower().find((quote or "")[:60].lower()) if quote else -1
        start = max(0, idx - 600) if idx >= 0 else 0
        return jsonify({"kind": "text", "file": f["name"], "file_id": f["id"], "found": idx >= 0,
                        "before": text[start:idx] if idx >= 0 else text[:1500],
                        "match": text[idx:idx + len(quote)] if idx >= 0 else "",
                        "after": text[idx + len(quote): idx + len(quote) + 900] if idx >= 0 else ""})

    @app.route("/api/runs/<run_id>/files/<file_id>/thumb.png")
    @login_required
    def file_thumb(run_id, file_id):
        load_run(run_id)
        f = storage.get_run_file(file_id)
        if not f or f["run_id"] != run_id:
            abort(404)
        path = thumbnail(f)
        if not path:
            abort(404)
        resp = send_file(path, mimetype="image/png", max_age=86400)
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    # ---- uploads: zip archives and resumable chunks ------------------------------
    @app.route("/api/runs/<run_id>/zip", methods=["POST"])
    @login_required
    def upload_zip(run_id):
        """Unpack a .zip into a set: every supported file inside (folders flattened into the name)."""
        run, project = load_run(run_id, "engineer")
        if run["status"] not in api.EDITABLE_INPUT_STATUSES:
            return jsonify({"error": "This run's inputs are locked."}), 409
        slot = (request.form.get("slot") or "").lower()
        up = request.files.get("file")
        if slot not in ("a", "b") or not up:
            return jsonify({"error": "Send a .zip file and the set (a or b)."}), 400
        tmp_dir = os.path.join(storage.DATA_DIR, "scratch", f"zip_{uuid.uuid4().hex[:8]}")
        os.makedirs(tmp_dir)
        try:
            zpath = os.path.join(tmp_dir, "upload.zip")
            up.save(zpath)
            return jsonify(_unpack_zip(api, run_id, slot, zpath, tmp_dir, project)), 201
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @app.route("/api/uploads", methods=["POST"])
    @login_required
    def upload_start():
        body = request.get_json(silent=True) or {}
        run, project = load_run(body.get("run_id") or "", "engineer")
        if run["status"] not in api.EDITABLE_INPUT_STATUSES:
            return jsonify({"error": "This run's inputs are locked."}), 409
        size = int(body.get("size") or 0)
        if body.get("slot") not in ("a", "b") or size <= 0:
            return jsonify({"error": "slot and size are required."}), 400
        import upload_checks
        if size > upload_checks.max_file_bytes() and not str(body.get("name", "")).lower().endswith(".zip"):
            return jsonify({"error": f"The file is {size / 1048576:.0f} MB; the limit per file is {upload_checks.MAX_FILE_MB} MB."}), 400
        sid = storage.new_id()
        key = f"uploads/{sid}.part"
        part = file_store.store().local_path(key)
        os.makedirs(os.path.dirname(part), exist_ok=True)
        open(part, "wb").close()
        with storage.tx() as conn:
            conn.execute(insert(upload_sessions).values(id=sid, run_id=run["id"], slot=body["slot"], name=str(body.get("name") or "file")[:300],
                                                        size=size, received=0, key=key, created_by=request.user["id"],
                                                        created_at=storage.now()))
        return jsonify({"id": sid, "received": 0, "chunk_size": 8 * 1024 * 1024}), 201

    def _session(sid):
        rows = _rows(select(upload_sessions).where(upload_sessions.c.id == sid))
        if not rows or rows[0]["created_by"] != request.user["id"]:
            abort(404)
        return rows[0]

    @app.route("/api/uploads/<sid>", methods=["GET", "PUT", "DELETE"])
    @login_required
    def upload_chunk(sid):
        s = _session(sid)
        path = file_store.store().local_path(s["key"])
        if request.method == "GET":
            return jsonify({"id": sid, "received": s["received"], "size": s["size"]})
        if request.method == "DELETE":
            with storage.tx() as conn:
                conn.execute(delete(upload_sessions).where(upload_sessions.c.id == sid))
            try:
                os.remove(path)
            except OSError:
                pass
            return jsonify({"status": "cancelled"})
        offset = int(request.args.get("offset", -1))
        if offset != s["received"]:
            return jsonify({"error": "Out of order - resume from the server's position.", "received": s["received"]}), 409
        data = request.get_data(cache=False)
        if len(data) > CHUNK_LIMIT or s["received"] + len(data) > s["size"]:
            return jsonify({"error": "Chunk too large."}), 413
        with open(path, "ab") as fh:
            fh.write(data)
        with storage.tx() as conn:
            conn.execute(update(upload_sessions).where(upload_sessions.c.id == sid).values(received=s["received"] + len(data)))
        return jsonify({"id": sid, "received": s["received"] + len(data), "size": s["size"]})

    @app.route("/api/uploads/<sid>/complete", methods=["POST"])
    @login_required
    def upload_complete(sid):
        s = _session(sid)
        run, project = load_run(s["run_id"], "engineer")
        path = file_store.store().local_path(s["key"])
        if s["received"] != s["size"]:
            return jsonify({"error": f"Only {s['received']} of {s['size']} bytes arrived - resume the upload."}), 409
        name = secure_filename(s["name"]) or f"file_{uuid.uuid4().hex[:6]}"
        try:
            if name.lower().endswith(".zip"):
                tmp_dir = os.path.join(storage.DATA_DIR, "scratch", f"zip_{uuid.uuid4().hex[:8]}")
                os.makedirs(tmp_dir)
                try:
                    result = _unpack_zip(api, s["run_id"], s["slot"], path, tmp_dir, project)
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
            else:
                record, err = api.store_upload(s["run_id"], s["slot"], path, name)
                if err:
                    return jsonify({"error": err}), 400
                storage.update_run(s["run_id"], user_id=request.user["id"], status="draft")
                audit("run.files_added", "run", s["run_id"], project["id"], f"added {name} to set {s['slot'].upper()} (resumable upload)")
                result = {"added": [api._file_payload(record)], "errors": []}
        finally:
            with storage.tx() as conn:
                conn.execute(delete(upload_sessions).where(upload_sessions.c.id == sid))
            try:
                os.remove(path)
            except OSError:
                pass
        return jsonify(result), 201


def _plain_text(path: str, ext: str) -> str:
    """Text of a document for the evidence viewer - quick, and never calls a model."""
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        lines = []
        for ws in wb.worksheets:
            lines.append(f"[Sheet {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c not in (None, "")]
                if cells:
                    lines.append(" | ".join(cells))
                if len(lines) > 20000:
                    break
        return "\n".join(lines)
    if ext == ".docx":
        import docx
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    with open(path, "rb") as fh:
        raw = fh.read(5_000_000)
    for enc in ("utf-8", "utf-16", "cp1256", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def _purge_date(deleted_at: Optional[str]) -> Optional[str]:
    if not deleted_at:
        return None
    return (datetime.fromisoformat(deleted_at) + timedelta(days=trash_days())).date().isoformat()


def _unpack_zip(api, run_id: str, slot: str, zpath: str, tmp_dir: str, project) -> Dict[str, Any]:
    from nodes.file_ingest import DOCUMENT_EXTENSIONS, DRAWING_EXTENSIONS
    import upload_checks
    ok_ext = DOCUMENT_EXTENSIONS | DRAWING_EXTENSIONS
    added, errors, skipped = [], [], 0
    try:
        zf = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile:
        return {"added": [], "errors": ["The .zip file is damaged."], "skipped": 0}
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > upload_checks.MAX_UNZIPPED_MB * 1048576:
            return {"added": [], "errors": ["The .zip expands to an unsafe size and was not opened."], "skipped": 0}
        existing = sum(1 for f in storage.list_run_files(run_id) if f["slot"] == slot)
        for info in infos:
            parts = [p for p in re.split(r"[\\/]", info.filename) if p and p not in (".", "..")]
            if not parts or parts[0] == "__MACOSX" or parts[-1].startswith((".", "~$")):
                continue
            ext = os.path.splitext(parts[-1])[1].lower()
            if ext not in ok_ext:
                skipped += 1
                continue
            if existing + len(added) >= api.MAX_FILES_PER_SET:
                errors.append(f"Stopped at {api.MAX_FILES_PER_SET} files - the limit per set.")
                break
            name = secure_filename("_".join(parts[-2:]) if len(parts) > 1 else parts[-1]) or f"file{ext}"
            out = os.path.join(tmp_dir, uuid.uuid4().hex[:8] + ext)
            with zf.open(info) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            record, err = api.store_upload(run_id, slot, out, name)
            if err:
                errors.append(err)
            else:
                added.append(api._file_payload(record))
    if added:
        storage.update_run(run_id, user_id=request.user["id"], status="draft")
        audit("run.files_added", "run", run_id, project["id"], f"unpacked {len(added)} file(s) from a .zip into set {slot.upper()}")
    return {"added": added, "errors": errors, "skipped": skipped}
