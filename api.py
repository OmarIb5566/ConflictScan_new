"""
Flask API - ConflictScan / RFI Automation
==========================================
Compares two document SETS from different stages or disciplines of the same
project, reports the discrepancies between and within them, and drafts RFIs.

Each side accepts any number of files - real RFIs cite a median of two and up
to nineteen attachments - mixing text documents (BOQ, specification,
schedules, letters; PDF / Word / Excel / CSV / TXT, scanned or not) and
drawings (PDF sheets, images, DXF). The API saves the uploads; reading them
(text extraction, OCR of scans, drawing renders) happens inside the graph.

Multi-user workspace (used by the web UI)
-----------------------------------------
    /api/auth/*        sign up, sign in, sign out
    /api/projects      projects visible to the signed-in user (own + team)
    /api/runs/<id>     one scan revision: inputs, uploaded files, progress,
                       results, and the engineer's edits to the results
    /api/jobs          the user's queued / running scans (for notifications)

Scans run in background workers (jobs.py); everything is stored by storage.py
under ./data. Every endpoint requires sign-in; security.py adds the CSRF,
session-timeout, sign-in throttling and same-origin rules, and
upload_checks.py vets uploaded files before anything reads them.

Windows compatible.
"""

import io
import json
import os
import re
import secrets
import shutil
import threading
import traceback
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from threading import Lock

from flask import (
    Flask, request, jsonify, render_template, send_from_directory, send_file, session, abort,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import jobs
import security
import storage
import upload_checks
from nodes.file_ingest import DOCUMENT_EXTENSIONS, DRAWING_EXTENSIONS, DWG_HINT
from nodes.rfi_drafting import resolve_template_path, build_token_values
from nodes.run_paths import template_dir
from nodes.docx_template import fill_docx_template, list_template_tokens


app = Flask(__name__, static_folder="static", template_folder="templates")
os.makedirs(template_dir(), exist_ok=True)

storage.init_db()
jobs.install_log_capture()

MAX_FILES_PER_SET = int(os.environ.get("CONFLICTSCAN_MAX_FILES_PER_SET", "40"))
ALLOW_SIGNUP = os.environ.get("CONFLICTSCAN_ALLOW_SIGNUP", "1").lower() not in ("0", "false", "no")
MIN_PASSWORD = int(os.environ.get("CONFLICTSCAN_MIN_PASSWORD", "8"))
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("CONFLICTSCAN_MAX_UPLOAD_MB", "500")) * 1024 * 1024


def _secret_key() -> str:
    key = os.environ.get("CONFLICTSCAN_SECRET_KEY")
    if key:
        return key
    path = os.path.join(storage.DATA_DIR, "secret_key")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key)
    return key


app.secret_key = _secret_key()
security.init_app(app)

# Serialises read-modify-write of one run's results JSON (two engineers editing different RFIs)
_run_locks: dict = {}
_run_locks_guard = Lock()


def _run_lock(run_id: str) -> Lock:
    with _run_locks_guard:
        return _run_locks.setdefault(run_id, Lock())


# Project-level fields the RFI form needs beyond the two documents; one value
# per scan, reused on every RFI it produces.
RFI_FORM_META_FIELDS = [
    "consultant", "employer", "project_code", "to",
    "location", "boq_no", "dwg_no", "level", "specs_no",
]

KIND_CHOICES = ("auto", "document", "drawing")
EDITABLE_INPUT_STATUSES = ("draft", "failed", "cancelled")
INPUT_TEXT_FIELDS = (
    "document_a_label", "document_b_label", "document_a_text", "document_b_text",
    "review_focus", "document_precedence", "rfi_grouping",
)
RFI_EDITABLE_FIELDS = (
    "subject", "area", "background", "discrepancy_summary", "question", "proposed_solution",
    "commercial_note", "cost_impact", "schedule_impact", "attachments", "discipline", "priority",
    "status", "response_required_by", "document_a_reference", "document_b_reference",
)
RFI_STATUSES = ("draft", "ready", "issued", "closed")
FINDING_STATUSES = ("open", "accepted", "dismissed")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    user = storage.get_user(uid)
    if not user:
        session.clear()
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "Please sign in.", "auth_required": True}), 401
        request.user = user
        return fn(*args, **kwargs)
    return wrapper


def _public_user(user):
    return {"id": user["id"], "username": user["username"], "display_name": user["display_name"]}


@app.route("/api/auth/status")
def auth_status():
    user = current_user()
    return jsonify({
        "user": _public_user(user) if user else None,
        "signup_allowed": ALLOW_SIGNUP or storage.count_users() == 0,
        "first_user": storage.count_users() == 0,
        "min_password": MIN_PASSWORD,
        "csrf_token": security.csrf_token(),
    })


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    body = request.get_json(silent=True) or {}
    if not (ALLOW_SIGNUP or storage.count_users() == 0):
        return jsonify({"error": "Sign-up is turned off. Ask an administrator for an account."}), 403
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    display_name = (body.get("display_name") or "").strip() or username
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,40}", username):
        return jsonify({"error": "Username: 3-40 letters, digits, dots, dashes or underscores."}), 400
    if len(password) < MIN_PASSWORD:
        return jsonify({"error": f"Password must be at least {MIN_PASSWORD} characters."}), 400
    if storage.get_user_by_username(username):
        return jsonify({"error": "That username is taken."}), 409
    user = storage.create_user(username, display_name[:80], generate_password_hash(password))
    security.start_session(user["id"], remember=True)
    return jsonify({"user": _public_user(user), "csrf_token": security.csrf_token()})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    address = security.client_address()
    wait = security.login_throttle.retry_after(username, address)
    if wait:
        minutes = max(1, round(wait / 60))
        return jsonify({"error": f"Too many failed sign-ins. Try again in {minutes} minute"
                                 f"{'s' if minutes != 1 else ''}, or ask an administrator."}), 429
    user = storage.get_user_by_username(username)
    if not user or not check_password_hash(user["password_hash"], body.get("password") or ""):
        security.login_throttle.failed(username, address)
        return jsonify({"error": "Wrong username or password."}), 401
    security.login_throttle.succeeded(username)
    security.start_session(user["id"], remember=body.get("remember", True))
    storage.touch_user(user["id"])
    return jsonify({"user": _public_user(user), "csrf_token": security.csrf_token()})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"status": "signed out", "csrf_token": security.csrf_token()})


@app.route("/api/me", methods=["PATCH"])
@login_required
def update_me():
    body = request.get_json(silent=True) or {}
    user = request.user
    fields = {}
    if body.get("display_name"):
        fields["display_name"] = str(body["display_name"]).strip()[:80]
    if body.get("new_password"):
        if not check_password_hash(user["password_hash"], body.get("current_password") or ""):
            return jsonify({"error": "Current password is wrong."}), 400
        if len(body["new_password"]) < MIN_PASSWORD:
            return jsonify({"error": f"New password must be at least {MIN_PASSWORD} characters."}), 400
        fields["password_hash"] = generate_password_hash(body["new_password"])
    storage.update_user(user["id"], **fields)
    return jsonify({"user": _public_user(storage.get_user(user["id"]))})


@app.route("/api/me/prefs", methods=["GET", "PUT"])
@login_required
def my_prefs():
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        allowed = {k: v for k, v in body.items() if k in ("workspace", "pinned", "theme", "ui")}
        storage.set_prefs(request.user["id"], allowed)
    return jsonify(storage.get_prefs(request.user["id"]))


@app.route("/api/users")
@login_required
def users_list():
    return jsonify(storage.list_users())


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_project(project_id):
    project = storage.get_project(project_id)
    if not storage.can_view(project, request.user["id"]):
        abort(404)
    return project


def _load_run(run_id):
    run = storage.get_run(run_id)
    if not run:
        abort(404)
    project = _load_project(run["project_id"])
    return run, project


@app.errorhandler(404)
def _not_found(_exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found, or you do not have access to it."}), 404
    return _exc


@app.errorhandler(413)
def _too_large(_exc):
    return jsonify({"error": "Upload too large."}), 413


def _file_payload(f):
    return {
        "id": f["id"], "slot": f["slot"], "name": f["name"], "kind": f["kind"],
        "detected_kind": f.get("detected_kind"), "detected_reason": f.get("detected_reason"),
        "size": f["size"], "created_at": f["created_at"],
        "pages": f.get("pages"), "problem": f.get("problem"), "inspecting": not f.get("inspected_at"),
    }


def _run_payload(run, project=None, full=True):
    out = {k: v for k, v in run.items() if k not in ("results", "inputs", "review", "progress")}
    out["counts"] = storage.run_counts(run.get("results"), run.get("review"))
    progress = run.get("progress") or {}
    live = jobs.live(run["id"]) if run["status"] in storage.ACTIVE_STATUSES else None
    out["progress"] = live or progress
    out["queue_position"] = jobs.queue_position(run["id"]) if run["status"] == "queued" else 0
    out["inputs_editable"] = run["status"] in EDITABLE_INPUT_STATUSES
    if project:
        out["project"] = {"id": project["id"], "name": project["name"], "owner_id": project["owner_id"]}
    if full:
        out["inputs"] = run.get("inputs") or {}
        out["results"] = run.get("results")
        out["review"] = run.get("review") or {}
        out["files"] = [_file_payload(f) for f in storage.list_run_files(run["id"])]
    return out


def _default_inputs(project_id):
    """A new run starts from the project's latest run settings (not its files)."""
    runs = storage.list_runs(project_id)
    if runs:
        prev = storage.get_run(runs[0]["id"]) or {}
        inputs = dict(prev.get("inputs") or {})
        inputs.pop("document_a_text", None)
        inputs.pop("document_b_text", None)
        return inputs
    return {"rfi_grouping": "consolidated", "rfi_form_meta": {}}


def _project_payload(project, runs, user_id, pinned=()):
    latest = runs[0] if runs else None
    # Headline numbers come from the newest revision that has results, so an
    # un-run draft revision doesn't blank out the project's findings.
    latest_complete = next((r for r in runs if r["status"] == "complete"), None)
    totals = {"findings": 0, "open": 0, "rfis": 0}
    if latest_complete and latest_complete.get("counts"):
        for k in totals:
            totals[k] = latest_complete["counts"].get(k, 0)
    return {
        **project,
        "is_owner": project["owner_id"] == user_id,
        "pinned": project["id"] in pinned,
        "run_count": len(runs),
        "latest_run": latest,
        "latest_complete": latest_complete,
        "runs": runs,
        "latest_counts": totals,
    }


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@app.route("/api/projects", methods=["GET", "POST"])
@login_required
def projects_collection():
    uid = request.user["id"]
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Give the project a name."}), 400
        project = storage.create_project(name[:120], uid, (body.get("description") or "").strip(),
                                         body.get("visibility") or "team")
        run = None
        if body.get("create_run", True):
            run = storage.create_run(project["id"], uid, {"rfi_grouping": "consolidated", "rfi_form_meta": {}})
        return jsonify({"project": storage.get_project(project["id"]),
                        "run": _run_payload(run, full=False) if run else None}), 201

    projects = storage.list_projects_for(uid)
    runs = storage.list_runs(project_ids=[p["id"] for p in projects], with_counts=True)
    by_project = {}
    for r in runs:
        by_project.setdefault(r["project_id"], []).append(r)
    pinned = set(storage.get_prefs(uid).get("pinned") or [])
    return jsonify([_project_payload(p, by_project.get(p["id"], []), uid, pinned) for p in projects])


@app.route("/api/projects/<project_id>", methods=["GET", "PATCH", "DELETE"])
@login_required
def project_item(project_id):
    uid = request.user["id"]
    project = _load_project(project_id)
    if request.method == "DELETE":
        if project["owner_id"] != uid:
            return jsonify({"error": "Only the project owner can delete it."}), 403
        if any(r["status"] in storage.ACTIVE_STATUSES for r in storage.list_runs(project_id)):
            return jsonify({"error": "A scan in this project is still running. Cancel it first."}), 409
        storage.delete_project(project_id)
        return jsonify({"status": "deleted"})
    if request.method == "PATCH":
        body = request.get_json(silent=True) or {}
        fields = {}
        if "name" in body and str(body["name"]).strip():
            fields["name"] = str(body["name"]).strip()[:120]
        if "description" in body:
            fields["description"] = str(body["description"] or "").strip()
        if "archived" in body:
            fields["archived"] = 1 if body["archived"] else 0
        if "visibility" in body:
            if project["owner_id"] != uid:
                return jsonify({"error": "Only the project owner can change who sees it."}), 403
            fields["visibility"] = "private" if body["visibility"] == "private" else "team"
        storage.update_project(project_id, **fields)
        project = storage.get_project(project_id)
    pinned = set(storage.get_prefs(uid).get("pinned") or [])
    return jsonify(_project_payload(project, storage.list_runs(project_id, with_counts=True), uid, pinned))


@app.route("/api/projects/<project_id>/runs", methods=["POST"])
@login_required
def project_new_run(project_id):
    _load_project(project_id)
    body = request.get_json(silent=True) or {}
    if body.get("from_run_id"):
        return clone_run(body["from_run_id"])
    run = storage.create_run(project_id, request.user["id"], _default_inputs(project_id),
                             title=(body.get("title") or "").strip())
    return jsonify(_run_payload(run, full=False)), 201


# ---------------------------------------------------------------------------
# Runs: inputs, files, lifecycle
# ---------------------------------------------------------------------------

@app.route("/api/runs/<run_id>", methods=["GET", "PATCH", "DELETE"])
@login_required
def run_item(run_id):
    run, project = _load_run(run_id)
    uid = request.user["id"]

    if request.method == "DELETE":
        if run["status"] in storage.ACTIVE_STATUSES:
            return jsonify({"error": "This scan is still running. Cancel it first."}), 409
        if run["created_by"] != uid and project["owner_id"] != uid:
            return jsonify({"error": "Only the person who created this run, or the project owner, can delete it."}), 403
        storage.delete_run(run_id)
        return jsonify({"status": "deleted"})

    if request.method == "PATCH":
        body = request.get_json(silent=True) or {}
        fields = {}
        if "title" in body:
            fields["title"] = str(body["title"] or "").strip()[:160]
        if "inputs" in body:
            if run["status"] not in EDITABLE_INPUT_STATUSES:
                return jsonify({"error": "This run has already been scanned - its inputs are locked. "
                                         "Use 'Edit & re-run' to make a new revision."}), 409
            inputs = dict(run.get("inputs") or {})
            incoming = body["inputs"] or {}
            for key in INPUT_TEXT_FIELDS:
                if key in incoming:
                    inputs[key] = str(incoming[key] or "")
            if "rfi_form_meta" in incoming:
                meta = dict(inputs.get("rfi_form_meta") or {})
                for field in RFI_FORM_META_FIELDS:
                    if field in (incoming["rfi_form_meta"] or {}):
                        meta[field] = str(incoming["rfi_form_meta"][field] or "")
                inputs["rfi_form_meta"] = meta
            if inputs.get("rfi_grouping") not in ("consolidated", "single"):
                inputs["rfi_grouping"] = "consolidated"
            fields["inputs"] = inputs
            if run["status"] in ("failed", "cancelled"):
                fields["status"] = "draft"
        storage.update_run(run_id, user_id=uid, **fields)
        run = storage.get_run(run_id)

    return jsonify(_run_payload(run, project))


@app.route("/api/runs/<run_id>/progress")
@login_required
def run_progress(run_id):
    run, _project = _load_run(run_id)
    return jsonify(_run_payload(run, full=run["status"] not in storage.ACTIVE_STATUSES))


def _validate_upload(filename, kind, slot):
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".dwg":
        return f"'{filename}': {DWG_HINT}"
    if ext not in DOCUMENT_EXTENSIONS | DRAWING_EXTENSIONS:
        return f"'{filename}' is not a supported file type."
    if kind == "drawing" and ext not in DRAWING_EXTENSIONS:
        return f"'{filename}' was marked as a drawing, but drawings must be PDF, image or DXF."
    if kind == "document" and ext not in DOCUMENT_EXTENSIONS:
        return f"'{filename}': {ext} files can only be read as drawings."
    return None


@app.route("/api/runs/<run_id>/files", methods=["POST"])
@login_required
def run_upload(run_id):
    run, _project = _load_run(run_id)
    if run["status"] not in EDITABLE_INPUT_STATUSES:
        return jsonify({"error": "This run's inputs are locked. Use 'Edit & re-run' to change files."}), 409
    slot = (request.form.get("slot") or "").lower()
    if slot not in ("a", "b"):
        return jsonify({"error": "slot must be 'a' or 'b'."}), 400
    uploads = [f for f in request.files.getlist("files") if f and f.filename]
    existing = sum(1 for f in storage.list_run_files(run_id) if f["slot"] == slot)
    if existing + len(uploads) > MAX_FILES_PER_SET:
        return jsonify({"error": f"Set {slot.upper()} would have {existing + len(uploads)} files; "
                                 f"the limit is {MAX_FILES_PER_SET}."}), 400

    added, errors = [], []
    inputs_dir = storage.run_dir(run_id, "inputs")
    for storage_obj in uploads:
        filename = secure_filename(storage_obj.filename) or f"file_{uuid.uuid4().hex[:6]}"
        err = _validate_upload(filename, "auto", slot)
        if err:
            errors.append(err)
            continue
        path = os.path.join(inputs_dir, jobs.new_upload_name(filename))
        storage_obj.save(path)
        problem = upload_checks.sniff(path, os.path.splitext(filename)[1])
        if problem:
            os.remove(path)
            errors.append(f"'{filename}' {problem}.")
            continue
        record = storage.add_run_file(run_id, slot, filename, path, "auto", os.path.getsize(path))
        jobs.inspect_file_later(record["id"])      # page count, encryption, document/drawing guess
        added.append(_file_payload(record))
    if added:
        storage.update_run(run_id, user_id=request.user["id"], status="draft")
    return jsonify({"added": added, "errors": errors}), (201 if added else 400)


@app.route("/api/runs/<run_id>/files/<file_id>", methods=["PATCH", "DELETE"])
@login_required
def run_file_item(run_id, file_id):
    run, _project = _load_run(run_id)
    f = storage.get_run_file(file_id)
    if not f or f["run_id"] != run_id:
        abort(404)
    if run["status"] not in EDITABLE_INPUT_STATUSES:
        return jsonify({"error": "This run's inputs are locked. Use 'Edit & re-run' to change files."}), 409
    if request.method == "DELETE":
        storage.delete_run_file(file_id)
        return jsonify({"status": "deleted"})
    body = request.get_json(silent=True) or {}
    fields = {}
    if body.get("kind") in KIND_CHOICES:
        err = _validate_upload(f["name"], body["kind"], f["slot"])
        if err:
            return jsonify({"error": err}), 400
        fields["kind"] = body["kind"]
    if body.get("slot") in ("a", "b"):
        fields["slot"] = body["slot"]
    storage.update_run_file(file_id, **fields)
    return jsonify(_file_payload(storage.get_run_file(file_id)))


@app.route("/api/runs/<run_id>/files/<file_id>/download")
@login_required
def run_file_download(run_id, file_id):
    _load_run(run_id)
    f = storage.get_run_file(file_id)
    if not f or f["run_id"] != run_id or not os.path.isfile(f["path"]):
        abort(404)
    return send_file(f["path"], as_attachment=True, download_name=f["name"])


@app.route("/api/runs/<run_id>/start", methods=["POST"])
@login_required
def run_start(run_id):
    run, _project = _load_run(run_id)
    if run["status"] in storage.ACTIVE_STATUSES:
        return jsonify({"error": "This scan is already queued or running."}), 409
    if run["status"] not in EDITABLE_INPUT_STATUSES:
        return jsonify({"error": "This run is complete. Use 'Edit & re-run' to scan again."}), 409
    inputs = run.get("inputs") or {}
    files = storage.list_run_files(run_id)
    bad = [f"{f['name']} {f['problem']}" for f in files if f.get("problem")]
    if bad:
        return jsonify({"error": "Remove or replace these files first: " + "; ".join(bad) + "."}), 400
    for slot in ("a", "b"):
        if not any(f["slot"] == slot for f in files) and not (inputs.get(f"document_{slot}_text") or "").strip():
            return jsonify({"error": f"Set {slot.upper()} is empty - add one or more files or paste text."}), 400
    jobs.submit(run_id)
    storage.update_run(run_id, user_id=request.user["id"])
    return jsonify(_run_payload(storage.get_run(run_id), full=False))


@app.route("/api/runs/<run_id>/cancel", methods=["POST"])
@login_required
def run_cancel(run_id):
    _load_run(run_id)
    if not jobs.cancel(run_id):
        return jsonify({"error": "This scan is not running."}), 409
    return jsonify(_run_payload(storage.get_run(run_id), full=False))


@app.route("/api/runs/<run_id>/clone", methods=["POST"])
@login_required
def clone_run(run_id):
    """'Edit & re-run': a new draft revision with the same settings and files."""
    run, project = _load_run(run_id)
    inputs = dict(run.get("inputs") or {})
    title = f"Edited from Rev {run['seq']}" + (f" - {run['title']}" if run.get("title") else "")
    new = storage.create_run(project["id"], request.user["id"], inputs, title=title[:160],
                             parent_run_id=run_id)
    storage.copy_run_files(run_id, new["id"])
    return jsonify(_run_payload(storage.get_run(new["id"]), full=False)), 201


@app.route("/api/jobs")
@login_required
def my_jobs():
    """Queued/running scans the user can see, plus ones that finished in the last few minutes."""
    uid = request.user["id"]
    visible = {p["id"]: p for p in storage.list_projects_for(uid)}
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    runs = storage.runs_with_status(storage.ACTIVE_STATUSES + ("complete", "failed", "cancelled"))
    out = []
    for r in runs:
        if r["project_id"] not in visible:
            continue
        if r["status"] not in storage.ACTIVE_STATUSES:
            if (r.get("finished_at") or "") < cutoff:
                continue
            r = storage.get_run(r["id"]) or r     # with results, for the finding/RFI counts
        payload = _run_payload(r, visible[r["project_id"]], full=False)
        payload["progress"] = {k: v for k, v in (payload.get("progress") or {}).items() if k != "log"}
        out.append(payload)
    return jsonify(out)


# ---------------------------------------------------------------------------
# Runs: editing results without re-running
# ---------------------------------------------------------------------------

def _require_results(run):
    if not run.get("results"):
        abort(409, description="This run has no results yet.")
    return run["results"]


@app.errorhandler(409)
def _conflict(exc):
    return jsonify({"error": getattr(exc, "description", "Conflict")}), 409


def _find_rfi(results, rfi_no):
    for i, rfi in enumerate(results.get("rfi_items") or []):
        if str(rfi.get("rfi_no")) == str(rfi_no):
            return i, rfi
    abort(404)


def _rebuild_description(rfi):
    rfi["description"] = "\n\n".join(
        str(rfi.get(k) or "").strip() for k in ("background", "discrepancy_summary", "commercial_note")
        if str(rfi.get(k) or "").strip()
    )


@app.route("/api/runs/<run_id>/findings/<disc_id>", methods=["PATCH"])
@login_required
def finding_review(run_id, disc_id):
    run, _project = _load_run(run_id)
    _require_results(run)
    body = request.get_json(silent=True) or {}
    with _run_lock(run_id):
        run = storage.get_run(run_id)
        review = run.get("review") or {}
        findings = review.setdefault("findings", {})
        entry = findings.setdefault(disc_id, {})
        if body.get("status") in FINDING_STATUSES:
            entry["status"] = body["status"]
        if "note" in body:
            entry["note"] = str(body["note"] or "")[:4000]
        if body.get("severity") in ("critical", "major", "minor", None, ""):
            if "severity" in body:
                entry["severity"] = body["severity"] or None
        entry["by"] = request.user["display_name"]
        entry["at"] = storage.now()
        storage.update_run(run_id, user_id=request.user["id"], review=review)
    return jsonify({"finding": entry, "counts": storage.run_counts(run.get("results"), review)})


@app.route("/api/runs/<run_id>/rfis", methods=["POST"])
@login_required
def rfi_add(run_id):
    """Add an RFI by hand (e.g. for an issue the scan missed), optionally linked to findings."""
    run, _project = _load_run(run_id)
    _require_results(run)
    body = request.get_json(silent=True) or {}
    with _run_lock(run_id):
        run = storage.get_run(run_id)
        results = run["results"]
        items = results.setdefault("rfi_items", [])
        nums = [int(m.group(1)) for r in items for m in [re.search(r"(\d+)$", str(r.get("rfi_no", "")))] if m]
        rfi_no = f"RFI-{(max(nums) + 1) if nums else 1:03d}"
        ids = [str(x) for x in (body.get("discrepancy_ids") or [])]
        discs = {d.get("id"): d for d in results.get("discrepancies") or []}
        linked = [discs[i] for i in ids if i in discs]
        rfi = {
            "rfi_no": rfi_no,
            "subject": (body.get("subject") or (linked[0].get("topic") if linked else "") or "New RFI").strip(),
            "discrepancy_ids": ids,
            "discrepancy_id": ", ".join(ids),
            "points": [],
            "area": linked[0].get("location", "") if linked else "",
            "discipline": (linked[0].get("discipline") if linked else "") or "other",
            "priority": "medium",
            "background": "",
            "discrepancy_summary": "\n".join(f"{n}. {d.get('description', '')}" for n, d in enumerate(linked, 1)),
            "question": "\n".join(f"{n}. {d.get('suggested_question', '')}" for n, d in enumerate(linked, 1)
                                  if d.get("suggested_question")),
            "proposed_solution": "", "commercial_note": "",
            "cost_impact": "To be assessed on receipt of clarification",
            "schedule_impact": "To be assessed on receipt of clarification",
            "attachments": [],
            "response_required_by": (datetime.now() + timedelta(days=7)).strftime("%d %B %Y"),
            "status": "draft", "manual": True, "docx_file": None,
            "edited_by": request.user["display_name"], "edited_at": storage.now(),
        }
        _rebuild_description(rfi)
        items.append(rfi)
        storage.update_run(run_id, user_id=request.user["id"], results=results)
    return jsonify(rfi), 201


@app.route("/api/runs/<run_id>/rfis/<rfi_no>", methods=["PUT", "DELETE"])
@login_required
def rfi_item(run_id, rfi_no):
    run, _project = _load_run(run_id)
    _require_results(run)
    with _run_lock(run_id):
        run = storage.get_run(run_id)
        results = run["results"]
        idx, rfi = _find_rfi(results, rfi_no)
        if request.method == "DELETE":
            results["rfi_items"].pop(idx)
            storage.update_run(run_id, user_id=request.user["id"], results=results)
            return jsonify({"status": "deleted"})

        body = request.get_json(silent=True) or {}
        if "original" not in rfi and not rfi.get("manual"):
            rfi["original"] = {k: rfi.get(k) for k in RFI_EDITABLE_FIELDS}
        for key in RFI_EDITABLE_FIELDS:
            if key not in body:
                continue
            value = body[key]
            if key == "attachments" and isinstance(value, str):
                value = [a.strip() for a in value.splitlines() if a.strip()]
            if key == "status" and value not in RFI_STATUSES:
                continue
            rfi[key] = value
        _rebuild_description(rfi)
        rfi["edited_by"] = request.user["display_name"]
        rfi["edited_at"] = storage.now()
        rfi["docx_stale"] = True
        results["rfi_items"][idx] = rfi
        storage.update_run(run_id, user_id=request.user["id"], results=results)
    return jsonify(rfi)


@app.route("/api/runs/<run_id>/rfis/<rfi_no>/revert", methods=["POST"])
@login_required
def rfi_revert(run_id, rfi_no):
    run, _project = _load_run(run_id)
    _require_results(run)
    with _run_lock(run_id):
        run = storage.get_run(run_id)
        results = run["results"]
        idx, rfi = _find_rfi(results, rfi_no)
        original = rfi.pop("original", None)
        if not original:
            return jsonify({"error": "There is no AI draft to revert to."}), 400
        rfi.update(original)
        _rebuild_description(rfi)
        for k in ("edited_by", "edited_at"):
            rfi.pop(k, None)
        rfi["docx_stale"] = True
        results["rfi_items"][idx] = rfi
        storage.update_run(run_id, user_id=request.user["id"], results=results)
    return jsonify(rfi)


def _write_rfi_docx(run, project, rfi, template_path):
    inputs = run.get("inputs") or {}
    results = run.get("results") or {}
    state_like = {
        "project_name": project["name"],
        "user_id": request.user["display_name"],
        "rfi_form_meta": inputs.get("rfi_form_meta") or {},
    }
    label_a = (results.get("document_a") or {}).get("label") or inputs.get("document_a_label") or "Document A"
    label_b = (results.get("document_b") or {}).get("label") or inputs.get("document_b_label") or "Document B"
    rfi = dict(rfi)
    if isinstance(rfi.get("attachments"), str):
        rfi["attachments"] = [a.strip() for a in rfi["attachments"].splitlines() if a.strip()]
    safe_no = secure_filename(str(rfi["rfi_no"])) or "RFI"
    safe_subject = secure_filename(str(rfi.get("subject") or ""))[:50]
    name = f"{safe_no}_{safe_subject}_edited.docx" if safe_subject else f"{safe_no}_edited.docx"
    fill_docx_template(template_path, os.path.join(storage.run_dir(run["id"], "exports"), name),
                       build_token_values(rfi, state_like, label_a, label_b))
    return name


@app.route("/api/runs/<run_id>/rfis/<rfi_no>/docx", methods=["POST"])
@login_required
def rfi_docx(run_id, rfi_no):
    run, project = _load_run(run_id)
    _require_results(run)
    template_path = resolve_template_path()
    if not template_path:
        return jsonify({"error": "No RFI form is loaded. Ask an administrator to add the project's .docx form."}), 400
    with _run_lock(run_id):
        run = storage.get_run(run_id)
        results = run["results"]
        idx, rfi = _find_rfi(results, rfi_no)
        try:
            name = _write_rfi_docx(run, project, rfi, template_path)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return jsonify({"error": f"Could not write the RFI document: {exc}"}), 500
        if rfi.get("docx_file") and rfi["docx_file"] != name and not rfi.get("original_docx_file"):
            rfi["original_docx_file"] = rfi["docx_file"]
        rfi["docx_file"] = name
        rfi["docx_stale"] = False
        results["rfi_items"][idx] = rfi
        storage.update_run(run_id, user_id=request.user["id"], results=results)
    return jsonify(rfi)


@app.route("/api/runs/<run_id>/docx-all", methods=["POST"])
@login_required
def rfi_docx_all(run_id):
    run, project = _load_run(run_id)
    _require_results(run)
    template_path = resolve_template_path()
    if not template_path:
        return jsonify({"error": "No RFI form is loaded. Ask an administrator to add the project's .docx form."}), 400
    errors = []
    with _run_lock(run_id):
        run = storage.get_run(run_id)
        results = run["results"]
        for rfi in results.get("rfi_items") or []:
            try:
                name = _write_rfi_docx(run, project, rfi, template_path)
                if rfi.get("docx_file") and rfi["docx_file"] != name and not rfi.get("original_docx_file"):
                    rfi["original_docx_file"] = rfi["docx_file"]
                rfi["docx_file"] = name
                rfi["docx_stale"] = False
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rfi.get('rfi_no')}: {exc}")
        storage.update_run(run_id, user_id=request.user["id"], results=results)
    return jsonify({"rfi_items": results.get("rfi_items") or [], "errors": errors})


@app.route("/api/runs/<run_id>/register", methods=["POST"])
@login_required
def run_register(run_id):
    """Re-write the discrepancy register + RFI log from the edited results (dismissed findings left out)."""
    from nodes.export import export_discrepancy_register

    run, project = _load_run(run_id)
    results = _require_results(run)
    review = (run.get("review") or {}).get("findings", {})
    export_dir = storage.run_dir(run_id, "exports")
    discrepancies = []
    for d in results.get("discrepancies") or []:
        r = review.get(d.get("id"), {})
        if r.get("status") == "dismissed":
            continue
        d = dict(d)
        if r.get("severity"):
            d["severity"] = r["severity"]
        discrepancies.append(d)
    rfi_items = [
        {**rfi, "docx_path": os.path.join(export_dir, rfi["docx_file"]) if rfi.get("docx_file") else None}
        for rfi in results.get("rfi_items") or []
    ]
    delta = export_discrepancy_register({
        "discrepancies": discrepancies,
        "rfi_items": rfi_items,
        "document_a_label": (results.get("document_a") or {}).get("label"),
        "document_b_label": (results.get("document_b") or {}).get("label"),
        "project_name": project["name"],
        "export_format": "excel",
    })
    path = delta.get("register_path")
    if not path:
        return jsonify({"error": "Nothing to export - every finding is dismissed."}), 400
    name = os.path.basename(path)
    shutil.move(path, os.path.join(export_dir, name))
    with _run_lock(run_id):
        run = storage.get_run(run_id)
        run["results"]["register_file"] = name
        storage.update_run(run_id, user_id=request.user["id"], results=run["results"])
    return jsonify({"register_file": name})


@app.route("/api/runs/<run_id>/exports/<path:filename>")
@login_required
def run_export_download(run_id, filename):
    _load_run(run_id)
    return send_from_directory(storage.run_dir(run_id, "exports"), filename, as_attachment=True)


@app.route("/api/runs/<run_id>/bundle")
@login_required
def run_bundle(run_id):
    """Register + every current RFI document in one zip."""
    run, project = _load_run(run_id)
    results = _require_results(run)
    export_dir = storage.run_dir(run_id, "exports")
    wanted = [results.get("register_file")] + [r.get("docx_file") for r in results.get("rfi_items") or []]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in dict.fromkeys(n for n in wanted if n):
            path = os.path.join(export_dir, name)
            if os.path.isfile(path):
                zf.write(path, name)
        zf.writestr("findings.json", json.dumps({
            "discrepancies": results.get("discrepancies") or [],
            "review": (run.get("review") or {}).get("findings", {}),
        }, indent=2, ensure_ascii=False))
    buf.seek(0)
    base = secure_filename(f"{project['name']}_Rev{run['seq']}") or "conflictscan"
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"{base}.zip")


# ---------------------------------------------------------------------------
# General routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the main web interface."""
    return render_template("index.html")


@app.route("/static/<path:filename>")
def serve_static(filename):
    """Serve static files (CSS, images, JS)."""
    return send_from_directory(app.static_folder, filename)


@app.route("/api/health")
def health_check():
    """Health check endpoint."""
    template_path = resolve_template_path()
    return jsonify({
        "status": "running",
        "service": "ConflictScan / RFI Automation",
        "version": "5.0.0",
        "platform": "windows_compatible",
        "workers": jobs.WORKERS,
        "features": {
            "langgraph": True,
            "document_sets": True,
            "ocr_for_scans": True,
            "internal_consistency_check": True,
            "interface_clash_review": True,
            "rfi_consolidation": True,
            "rfi_template_loaded": bool(template_path),
            "multi_user": True,
            "background_jobs": True,
        },
    })


@app.route("/api/rfi-template", methods=["GET"])
@login_required
def rfi_template_status():
    """Report which RFI form is in use and which tokens it contains."""
    path = resolve_template_path()
    if not path:
        return jsonify({
            "loaded": False,
            "message": (
                "No RFI form found. An administrator can add the project's .docx RFI form "
                "(placeholders written as {{token}})."
            ),
        })
    try:
        tokens = list_template_tokens(path)
    except Exception as exc:
        tokens = []
        print(f"[api] Could not read tokens from template: {exc}")

    return jsonify({
        "loaded": True,
        "filename": os.path.basename(path),
        "tokens": tokens,
    })


_recovered = threading.Event()


def _recover_once():
    # With Flask's debug reloader the module loads twice; only the serving child should touch runs.
    if not _recovered.is_set() and (not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
        _recovered.set()
        jobs.recover_after_restart()


if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("STARTING CONFLICTSCAN / RFI AUTOMATION")
    print("=" * 80)
    print("Web Interface: http://localhost:5000")
    print("API Health:    http://localhost:5000/api/health")
    template = resolve_template_path()
    print(f"RFI form:      {os.path.basename(template) if template else 'none loaded (templates_rfi/)'}")
    print(f"Data folder:   {storage.DATA_DIR}")
    print(f"Scan workers:  {jobs.WORKERS}")
    print("=" * 80 + "\n")

    debug = os.environ.get("CONFLICTSCAN_DEBUG", "1").lower() not in ("0", "false", "no")
    app.debug = debug
    _recover_once()
    app.run(host="0.0.0.0", port=5000, debug=debug)
