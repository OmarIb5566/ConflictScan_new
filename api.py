"""
Flask API - ConflictScan / RFI Automation
==========================================
Compares two document SETS from different stages or disciplines of the same
project, reports the discrepancies between and within them, and drafts RFIs.

Each side accepts any number of files - real RFIs cite a median of two and up
to nineteen attachments - mixing text documents (BOQ, specification,
schedules, letters; PDF / Word / Excel / CSV / TXT, scanned or not) and
drawings (PDF sheets, images, DXF). The API saves the uploads; reading them
(text extraction, OCR of scans, drawing renders) happens in the workers.

    /api/auth/*        sign in (local or company directory), sign out
    /api/projects      projects the user can see, members and roles
    /api/runs/<id>     one scan revision: inputs, files, progress, results,
                       and the engineers' edits to the results
    /api/jobs          the user's queued / running scans (notifications)
    /api/presence      who else is looking at the same revision
    /api/reference/*   the company project register (read-only)
    /api/admin/*       users, scan queue, health, audit log (admin_api.py)

Storage is storage.py (any database, see db.py) plus adapters/files.py.
Security rules live in security.py; upload vetting in upload_checks.py.

Windows compatible.
"""

import config  # noqa: F401 - loads conflictscan.toml before anything reads settings

import io
import json
import os
import re
import secrets
import traceback
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, jsonify, render_template, request, send_file, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import jobs
import security
import storage
import upload_checks
from adapters import files as file_store
from adapters import identity, reference
from nodes.docx_template import fill_docx_template, list_template_tokens
from nodes.file_ingest import DOCUMENT_EXTENSIONS, DRAWING_EXTENSIONS, DWG_HINT
from nodes.rfi_drafting import build_token_values, resolve_template_path
from nodes.run_paths import template_dir
from webcore import (Conflict, admin_required, audit, current_user, load_project, load_run, login_required,
                     mutate_run, public_user)

app = Flask(__name__, static_folder="static", template_folder="templates")
os.makedirs(template_dir(), exist_ok=True)

storage.init_db()
jobs.install_log_capture()


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).lower() not in ("0", "false", "no", "")


MAX_FILES_PER_SET = int(os.environ.get("CONFLICTSCAN_MAX_FILES_PER_SET", "40"))
ALLOW_SIGNUP = _flag("CONFLICTSCAN_ALLOW_SIGNUP", "0")
SIGNUP_APPROVAL = _flag("CONFLICTSCAN_SIGNUP_APPROVAL", "1")
MIN_PASSWORD = int(os.environ.get("CONFLICTSCAN_MIN_PASSWORD", "8"))
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("CONFLICTSCAN_MAX_UPLOAD_MB", "500")) * 1024 * 1024
VERSION = config.VERSION


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

if _flag("CONFLICTSCAN_BEHIND_PROXY", "0"):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


@app.before_request
def _start_workers():
    # Lazily, on the first request, so a debug-reloader parent process never runs scans.
    jobs.start_embedded_worker()


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
# Errors
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def _not_found(exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found, or you do not have access to it."}), 404
    return exc


@app.errorhandler(400)
def _bad_request(exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": getattr(exc, "description", None) or "Bad request."}), 400
    return exc


@app.errorhandler(403)
def _forbidden(exc):
    return jsonify({"error": getattr(exc, "description", None) or "You do not have permission to do this."}), 403


@app.errorhandler(409)
def _conflict(exc):
    return jsonify({"error": getattr(exc, "description", "Conflict")}), 409


@app.errorhandler(Conflict)
def _edit_conflict(exc):
    return jsonify({"error": "Someone else changed this while you were editing.", "conflict": True,
                    **exc.payload}), 409


@app.errorhandler(500)
def _server_error(exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": getattr(exc, "description", None) or "Something went wrong on the server."}), 500
    return exc


@app.errorhandler(413)
def _too_large(_exc):
    return jsonify({"error": "Upload too large."}), 413


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _signup_allowed() -> bool:
    return storage.count_users() == 0 or (ALLOW_SIGNUP and "local" in identity.sources())


@app.route("/api/auth/status")
def auth_status():
    user = current_user()
    return jsonify({
        "user": public_user(user) if user else None,
        "signup_allowed": _signup_allowed(),
        "signup_needs_approval": SIGNUP_APPROVAL,
        "first_user": storage.count_users() == 0,
        "min_password": MIN_PASSWORD,
        "auth_mode": identity.mode(),
        "csrf_token": security.csrf_token(),
    })


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    body = request.get_json(silent=True) or {}
    first = storage.count_users() == 0
    if not _signup_allowed():
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
    status = "active" if first or not SIGNUP_APPROVAL else "pending"
    user = storage.create_user(username, display_name[:80], generate_password_hash(password),
                               role="admin" if first else "engineer", status=status)
    audit("user.register", "user", user["id"], summary=f"{username} registered ({status})", user=user)
    if status == "pending":
        return jsonify({"pending": True, "message": "Account requested. An administrator will approve it - "
                                                    "you can sign in once it is approved."}), 202
    security.start_session(user["id"], remember=True)
    return jsonify({"user": public_user(user), "csrf_token": security.csrf_token()})


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
    result = identity.authenticate(username, body.get("password") or "")
    if not result.user:
        security.login_throttle.failed(username, address)
        storage.audit("auth.login_failed", None, "user", None, summary=f"failed sign-in as {username[:60]}",
                      ip=address)
        return jsonify({"error": result.error}), 401
    user = result.user
    if user["status"] == "pending":
        return jsonify({"error": "Your account is waiting for an administrator's approval."}), 403
    if user["status"] == "disabled":
        return jsonify({"error": "Your account is disabled. Ask an administrator."}), 403
    security.login_throttle.succeeded(username)
    security.start_session(user["id"], remember=body.get("remember", True))
    storage.touch_user(user["id"])
    audit("auth.login", "user", user["id"], summary=f"{user['username']} signed in ({user['auth_source']})", user=user)
    return jsonify({"user": public_user(user), "csrf_token": security.csrf_token()})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    user = current_user()
    if user:
        audit("auth.logout", "user", user["id"], user=user)
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
        if user.get("auth_source") != "local":
            return jsonify({"error": "Your password is managed by the company directory."}), 400
        if not check_password_hash(user["password_hash"] or "", body.get("current_password") or ""):
            return jsonify({"error": "Current password is wrong."}), 400
        if len(body["new_password"]) < MIN_PASSWORD:
            return jsonify({"error": f"New password must be at least {MIN_PASSWORD} characters."}), 400
        fields["password_hash"] = generate_password_hash(body["new_password"])
        fields["must_change_password"] = False
        audit("user.password_changed", "user", user["id"])
    storage.update_user(user["id"], **fields)
    return jsonify({"user": public_user(storage.get_user(user["id"]))})


@app.route("/api/me/prefs", methods=["GET", "PUT"])
@login_required
def my_prefs():
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        allowed = {k: v for k, v in body.items() if k in ("workspace", "pinned", "theme", "ui", "views", "tour")}
        storage.set_prefs(request.user["id"], allowed)
    return jsonify(storage.get_prefs(request.user["id"]))


@app.route("/api/users")
@login_required
def users_list():
    return jsonify([{k: u[k] for k in ("id", "username", "display_name", "role")} for u in storage.list_users()])


# ---------------------------------------------------------------------------
# Presence ("Sara is viewing this")
# ---------------------------------------------------------------------------

@app.route("/api/presence", methods=["POST"])
@login_required
def presence_ping():
    body = request.get_json(silent=True) or {}
    target = str(body.get("target") or "")[:64]
    if not target:
        return jsonify([])
    if body.get("leave"):
        storage.clear_presence(request.user["id"], target)
        return jsonify([])
    storage.touch_presence(request.user["id"], target, str(body.get("detail") or ""))
    others = [p for p in storage.present_on(target) if p["user_id"] != request.user["id"]]
    return jsonify([{"user_id": p["user_id"], "display_name": p["display_name"], "detail": p["detail"]} for p in others])


# ---------------------------------------------------------------------------
# Reference data (company project register - read-only)
# ---------------------------------------------------------------------------

@app.route("/api/reference/projects")
@login_required
def reference_projects():
    try:
        return jsonify(reference.search_projects(request.args.get("q", "")))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"The project register is not available ({exc})."}), 502


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _file_payload(f):
    return {
        "id": f["id"], "slot": f["slot"], "name": f["name"], "kind": f["kind"],
        "detected_kind": f.get("detected_kind"), "detected_reason": f.get("detected_reason"),
        "size": f["size"], "created_at": f["created_at"],
        "pages": f.get("pages"), "problem": f.get("problem"), "inspecting": not f.get("inspected_at"),
        "sha256": f.get("sha256"), "lineage_id": f.get("lineage_id"), "supersedes": f.get("supersedes"),
        "duplicate_of": f.get("duplicate_of"), "unchanged_since_parent": f.get("unchanged_since_parent"),
    }


def _run_payload(run, project=None, full=True):
    out = {k: v for k, v in run.items() if k not in ("results", "inputs", "review", "progress")}
    out["counts"] = storage.run_counts(run.get("results"), run.get("review"))
    verdict = (run.get("results") or {}).get("assessment") or {}
    out["verdict"] = {"key": verdict.get("verdict"), "label": verdict.get("label")} if verdict.get("verdict") else None
    progress = run.get("progress") or {}
    live = jobs.live(run["id"]) if run["status"] in storage.ACTIVE_STATUSES else None
    out["progress"] = live or progress
    out["queue_position"] = jobs.queue_position(run["id"]) if run["status"] == "queued" else 0
    out["inputs_editable"] = run["status"] in EDITABLE_INPUT_STATUSES
    if project:
        out["project"] = {"id": project["id"], "name": project["name"], "owner_id": project["owner_id"],
                          "code": project.get("code")}
        if project.get("my_role"):
            out["my_role"] = project["my_role"]
    if full:
        out["inputs"] = run.get("inputs") or {}
        out["results"] = run.get("results")
        out["review"] = run.get("review") or {}
        files = storage.list_run_files(run["id"])
        try:
            import revisions
            revisions.annotate_files(run, files)
        except Exception as exc:  # noqa: BLE001
            print(f"[api] could not annotate files: {exc}")
        out["files"] = [_file_payload(f) for f in files]
    return out


def _default_inputs(project):
    """A new run starts from the project's latest run settings (not its files) and the project's form defaults."""
    runs = storage.list_runs(project["id"])
    if runs:
        prev = storage.get_run(runs[0]["id"]) or {}
        inputs = dict(prev.get("inputs") or {})
        inputs.pop("document_a_text", None)
        inputs.pop("document_b_text", None)
        inputs.pop("_rev", None)
    settings = project.get("settings") or {}
    if runs:
        # project defaults fill any header field the previous revision left empty
        meta = dict(settings.get("rfi_form_meta") or {})
        meta.update({k: v for k, v in (inputs.get("rfi_form_meta") or {}).items() if v})
        inputs["rfi_form_meta"] = meta
        return inputs
    return {"rfi_grouping": settings.get("rfi_grouping") or "consolidated",
            "document_a_label": settings.get("set_a_label") or "",
            "document_b_label": settings.get("set_b_label") or "",
            "rfi_form_meta": dict(settings.get("rfi_form_meta") or {})}


def _project_payload(project, runs, user, pinned=()):
    latest = runs[0] if runs else None
    # Headline numbers come from the newest revision that has results, so an
    # un-run draft revision doesn't blank out the project's findings.
    latest_complete = next((r for r in runs if r["status"] == "complete"), None)
    totals = {"findings": 0, "open": 0, "rfis": 0}
    if latest_complete and latest_complete.get("counts"):
        for k in totals:
            totals[k] = latest_complete["counts"].get(k, 0)
    role = project.get("my_role") or storage.project_role(project, user)
    return {
        **project,
        "my_role": role,
        "is_owner": project["owner_id"] == user["id"],
        "pinned": project["id"] in pinned,
        "run_count": len(runs),
        "latest_run": latest,
        "latest_complete": latest_complete,
        "runs": runs,
        "latest_counts": totals,
    }


# ---------------------------------------------------------------------------
# Projects and members
# ---------------------------------------------------------------------------

def _project_settings_from_reference(body):
    ref = body.get("reference") or {}
    meta = {}
    if ref.get("consultant"):
        meta["consultant"] = ref["consultant"]
    if ref.get("client"):
        meta["employer"] = ref["client"]
    if ref.get("code"):
        meta["project_code"] = ref["code"]
    if ref.get("location"):
        meta["location"] = ref["location"]
    return {"rfi_form_meta": meta} if meta else {}


@app.route("/api/projects", methods=["GET", "POST"])
@login_required
def projects_collection():
    user = request.user
    if request.method == "POST":
        if user["role"] == "viewer":
            return jsonify({"error": "Viewers cannot create projects."}), 403
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Give the project a name."}), 400
        code = (body.get("code") or (body.get("reference") or {}).get("code") or "").strip() or None
        settings = _project_settings_from_reference(body)
        if body.get("copy_settings_from"):
            src, _role = load_project(body["copy_settings_from"])
            settings = {**(src.get("settings") or {}), **settings}
        project = storage.create_project(name[:200], user["id"], (body.get("description") or "").strip(),
                                         body.get("visibility") or "team", code=code, settings=settings)
        audit("project.create", "project", project["id"], project["id"], f"created project {name[:80]}")
        run = None
        if body.get("create_run", True):
            run = storage.create_run(project["id"], user["id"], _default_inputs(project))
        project["my_role"] = "manager"
        return jsonify({"project": project, "run": _run_payload(run, full=False) if run else None}), 201

    projects = storage.list_projects_for(user)
    runs = storage.list_runs(project_ids=[p["id"] for p in projects], with_counts=True)
    by_project = {}
    for r in runs:
        by_project.setdefault(r["project_id"], []).append(r)
    pinned = set(storage.get_prefs(user["id"]).get("pinned") or [])
    return jsonify([_project_payload(p, by_project.get(p["id"], []), user, pinned) for p in projects])


@app.route("/api/projects/<project_id>", methods=["GET", "PATCH", "DELETE"])
@login_required
def project_item(project_id):
    user = request.user
    project, role = load_project(project_id)
    if request.method == "DELETE":
        load_project(project_id, "manager")
        if any(r["status"] in storage.ACTIVE_STATUSES for r in storage.list_runs(project_id)):
            return jsonify({"error": "A scan in this project is still running. Cancel it first."}), 409
        _delete_project(project, user)
        return jsonify({"status": "deleted"})
    if request.method == "PATCH":
        load_project(project_id, "engineer")
        body = request.get_json(silent=True) or {}
        fields = {}
        if "name" in body and str(body["name"]).strip():
            fields["name"] = str(body["name"]).strip()[:200]
        if "description" in body:
            fields["description"] = str(body["description"] or "").strip()
        if "archived" in body:
            fields["archived"] = bool(body["archived"])
        if "code" in body:
            fields["code"] = str(body["code"] or "").strip()[:80] or None
        if "visibility" in body:
            load_project(project_id, "manager")
            fields["visibility"] = "private" if body["visibility"] == "private" else "team"
        if "settings" in body and isinstance(body["settings"], dict):
            load_project(project_id, "manager")
            fields["settings"] = _clean_settings({**(project.get("settings") or {}), **body["settings"]})
        storage.update_project(project_id, **fields)
        audit("project.update", "project", project_id, project_id, "changed " + ", ".join(sorted(fields)),
              {k: v for k, v in fields.items() if k != "settings"})
        project, role = load_project(project_id)
    pinned = set(storage.get_prefs(user["id"]).get("pinned") or [])
    payload = _project_payload(project, storage.list_runs(project_id, with_counts=True), user, pinned)
    payload["members"] = storage.list_members(project_id)
    return jsonify(payload)


# Phase hooks: project settings validation and soft delete (trash) are filled in by later modules.
def _clean_settings(settings):
    for hook in SETTINGS_CLEANERS:
        settings = hook(settings) or settings
    return settings


SETTINGS_CLEANERS: list = []


def _delete_project(project, user):
    storage.delete_project(project["id"])
    audit("project.delete", "project", project["id"], project["id"], f"deleted project {project['name'][:80]}")


def _delete_run(run, project, user):
    storage.delete_run(run["id"])
    audit("run.delete", "run", run["id"], project["id"], f"deleted Rev {run['seq']}")


@app.route("/api/projects/<project_id>/members", methods=["GET"])
@login_required
def project_members(project_id):
    project, _role = load_project(project_id)
    owner = storage.get_user(project["owner_id"])
    return jsonify({"owner": public_user(owner) if owner else None, "members": storage.list_members(project_id)})


@app.route("/api/projects/<project_id>/members/<user_id>", methods=["PUT", "DELETE"])
@login_required
def project_member_item(project_id, user_id):
    project, _role = load_project(project_id, "manager")
    target = storage.get_user(user_id)
    if not target:
        abort(404)
    if request.method == "DELETE":
        storage.remove_member(project_id, user_id)
        audit("project.member_removed", "user", user_id, project_id, f"removed {target['username']}")
    else:
        role = (request.get_json(silent=True) or {}).get("role")
        if role not in storage.PROJECT_ROLES:
            return jsonify({"error": "Role must be manager, engineer or viewer."}), 400
        storage.add_member(project_id, user_id, role, request.user["id"])
        audit("project.member_set", "user", user_id, project_id, f"{target['username']} -> {role}")
    return jsonify({"members": storage.list_members(project_id)})


@app.route("/api/projects/<project_id>/runs", methods=["POST"])
@login_required
def project_new_run(project_id):
    project, _role = load_project(project_id, "engineer")
    body = request.get_json(silent=True) or {}
    if body.get("from_run_id"):
        return clone_run(body["from_run_id"])
    run = storage.create_run(project_id, request.user["id"], _default_inputs(project),
                             title=(body.get("title") or "").strip())
    audit("run.create", "run", run["id"], project_id, f"new Rev {run['seq']}")
    return jsonify(_run_payload(run, full=False)), 201


# ---------------------------------------------------------------------------
# Runs: inputs, files, lifecycle
# ---------------------------------------------------------------------------

@app.route("/api/runs/<run_id>", methods=["GET", "PATCH", "DELETE"])
@login_required
def run_item(run_id):
    run, project = load_run(run_id)
    uid = request.user["id"]

    if request.method == "DELETE":
        if run["status"] in storage.ACTIVE_STATUSES:
            return jsonify({"error": "This scan is still running. Cancel it first."}), 409
        if not (run["created_by"] == uid and storage.can(project["my_role"], "engineer")) \
                and not storage.can(project["my_role"], "manager"):
            return jsonify({"error": "Only the person who created this revision, or a project manager, "
                                     "can delete it."}), 403
        _delete_run(run, project, request.user)
        return jsonify({"status": "deleted"})

    if request.method == "PATCH":
        load_run(run_id, "engineer")
        body = request.get_json(silent=True) or {}
        fields = {}
        if "title" in body:
            fields["title"] = str(body["title"] or "").strip()[:300]
        if "inputs" in body:
            if run["status"] not in EDITABLE_INPUT_STATUSES:
                return jsonify({"error": "This run has already been scanned - its inputs are locked. "
                                         "Use 'Edit & re-run' to make a new revision."}), 409
            base = body.get("base_inputs_rev")
            current_rev = (run.get("inputs") or {}).get("_rev", 0)
            if base is not None and int(base) != current_rev:
                raise Conflict({"inputs": run.get("inputs") or {}, "updated_by_name": run.get("updated_by_name"),
                                "inputs_rev": current_rev})
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
            inputs["_rev"] = current_rev + 1
            fields["inputs"] = inputs
            if run["status"] in ("failed", "cancelled"):
                fields["status"] = "draft"
        storage.update_run(run_id, user_id=uid, **fields)
        run = storage.get_run(run_id)

    return jsonify(_run_payload(run, project))


@app.route("/api/runs/<run_id>/progress")
@login_required
def run_progress(run_id):
    run, project = load_run(run_id)
    return jsonify(_run_payload(run, project, full=run["status"] not in storage.ACTIVE_STATUSES))


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


def store_upload(run_id: str, slot: str, storage_obj, filename: str):
    """Save one uploaded file into a run (sniffed, stored, queued for inspection). Returns (record, error)."""
    err = _validate_upload(filename, "auto", slot)
    if err:
        return None, err
    store = file_store.store()
    key = storage.run_key(run_id, "inputs", jobs.new_upload_name(filename))
    path = store.local_path(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(storage_obj, str):
        import shutil
        shutil.copy2(storage_obj, path)
    else:
        storage_obj.save(path)
    problem = upload_checks.sniff(path, os.path.splitext(filename)[1])
    if problem:
        os.remove(path)
        return None, f"'{filename}' {problem}."
    store.sync(key)
    record = storage.add_run_file(run_id, slot, filename, key, "auto", os.path.getsize(path))
    jobs.inspect_file_later(record["id"])      # page count, encryption, fingerprint, document/drawing guess
    return record, None


@app.route("/api/runs/<run_id>/files", methods=["POST"])
@login_required
def run_upload(run_id):
    run, project = load_run(run_id, "engineer")
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
    for storage_obj in uploads:
        filename = secure_filename(storage_obj.filename) or f"file_{uuid.uuid4().hex[:6]}"
        record, err = store_upload(run_id, slot, storage_obj, filename)
        if err:
            errors.append(err)
        else:
            added.append(_file_payload(record))
    if added:
        storage.update_run(run_id, user_id=request.user["id"], status="draft")
        audit("run.files_added", "run", run_id, project["id"], f"added {len(added)} file(s) to set {slot.upper()}",
              {"files": [f["name"] for f in added]})
    return jsonify({"added": added, "errors": errors}), (201 if added else 400)


@app.route("/api/runs/<run_id>/files/<file_id>", methods=["PATCH", "DELETE"])
@login_required
def run_file_item(run_id, file_id):
    run, project = load_run(run_id, "engineer")
    f = storage.get_run_file(file_id)
    if not f or f["run_id"] != run_id:
        abort(404)
    if run["status"] not in EDITABLE_INPUT_STATUSES:
        return jsonify({"error": "This run's inputs are locked. Use 'Edit & re-run' to change files."}), 409
    if request.method == "DELETE":
        storage.delete_run_file(file_id)
        audit("run.file_removed", "run", run_id, project["id"], f"removed {f['name']}")
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
    _run, project = load_run(run_id)
    f = storage.get_run_file(file_id)
    if not f or f["run_id"] != run_id or not file_store.store().exists(f["path"]):
        abort(404)
    audit("file.download", "run", run_id, project["id"], f["name"])
    return send_file(storage.file_path(f), as_attachment=request.args.get("inline") != "1", download_name=f["name"])


@app.route("/api/runs/<run_id>/start", methods=["POST"])
@login_required
def run_start(run_id):
    run, project = load_run(run_id, "engineer")
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
    for check in START_CHECKS:
        problem = check(run, project, request.user, files)
        if problem:
            return jsonify({"error": problem}), 429
    jobs.submit(run_id, request.user["id"])
    storage.update_run(run_id, user_id=request.user["id"])
    audit("run.start", "run", run_id, project["id"], f"started a scan of Rev {run['seq']}")
    return jsonify(_run_payload(storage.get_run(run_id), project, full=False))


START_CHECKS: list = []      # fair-use limits plug in here


@app.route("/api/runs/<run_id>/cancel", methods=["POST"])
@login_required
def run_cancel(run_id):
    run, project = load_run(run_id, "engineer")
    if not jobs.cancel(run_id):
        return jsonify({"error": "This scan is not running."}), 409
    audit("run.cancel", "run", run_id, project["id"], f"cancelled the scan of Rev {run['seq']}")
    return jsonify(_run_payload(storage.get_run(run_id), project, full=False))


@app.route("/api/runs/<run_id>/clone", methods=["POST"])
@login_required
def clone_run(run_id):
    """'Edit & re-run': a new draft revision with the same settings and files."""
    run, project = load_run(run_id, "engineer")
    inputs = dict(run.get("inputs") or {})
    inputs.pop("_rev", None)
    title = f"Edited from Rev {run['seq']}" + (f" - {run['title']}" if run.get("title") else "")
    new = storage.create_run(project["id"], request.user["id"], inputs, title=title[:300], parent_run_id=run_id)
    storage.copy_run_files(run_id, new["id"])
    audit("run.clone", "run", new["id"], project["id"], f"Rev {new['seq']} from Rev {run['seq']}")
    return jsonify(_run_payload(storage.get_run(new["id"]), project, full=False)), 201


@app.route("/api/jobs")
@login_required
def my_jobs():
    """Queued/running scans the user can see, plus ones that finished in the last few minutes."""
    user = request.user
    visible = {p["id"]: p for p in storage.list_projects_for(user)}
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
    run, project = load_run(run_id, "engineer")
    _require_results(run)
    body = request.get_json(silent=True) or {}
    holder = {}

    def change(r):
        review = r.get("review") or {}
        entry = review.setdefault("findings", {}).setdefault(disc_id, {})
        if body.get("status") in FINDING_STATUSES:
            entry["status"] = body["status"]
        if "note" in body:
            entry["note"] = str(body["note"] or "")[:4000]
        if "severity" in body and body.get("severity") in ("critical", "major", "minor", None, ""):
            entry["severity"] = body["severity"] or None
        for extra in FINDING_FIELDS:
            if extra in body:
                entry[extra] = body[extra]
        entry["by"] = request.user["display_name"]
        entry["at"] = storage.now()
        holder["entry"], holder["review"], holder["results"] = entry, review, r.get("results")
        return {"review": review}

    mutate_run(run_id, change, request.user["id"])
    audit("finding.review", "finding", disc_id, project["id"],
          f"Rev {run['seq']} {disc_id}: " + ", ".join(f"{k}={body[k]}" for k in ("status", "severity") if k in body))
    return jsonify({"finding": holder["entry"], "counts": storage.run_counts(holder["results"], holder["review"])})


FINDING_FIELDS: tuple = ()     # e.g. assignee (collaboration phase)


def _next_rfi_no(items):
    nums = [int(m.group(1)) for r in items for m in [re.search(r"(\d+)$", str(r.get("rfi_no", "")))] if m]
    return f"RFI-{(max(nums) + 1) if nums else 1:03d}"


@app.route("/api/runs/<run_id>/rfis", methods=["POST"])
@login_required
def rfi_add(run_id):
    """Add an RFI by hand (e.g. for an issue the scan missed), optionally linked to findings."""
    run, project = load_run(run_id, "engineer")
    _require_results(run)
    body = request.get_json(silent=True) or {}
    holder = {}

    def change(r):
        results = r["results"]
        items = results.setdefault("rfi_items", [])
        ids = [str(x) for x in (body.get("discrepancy_ids") or [])]
        discs = {d.get("id"): d for d in results.get("discrepancies") or []}
        linked = [discs[i] for i in ids if i in discs]
        rfi = {
            "rfi_no": _next_rfi_no(items),
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
            "status": "draft", "manual": True, "docx_file": None, "rev": 1,
            "edited_by": request.user["display_name"], "edited_at": storage.now(),
        }
        _rebuild_description(rfi)
        items.append(rfi)
        holder["rfi"] = rfi
        return {"results": results}

    mutate_run(run_id, change, request.user["id"])
    audit("rfi.add", "rfi", holder["rfi"]["rfi_no"], project["id"], f"Rev {run['seq']} {holder['rfi']['subject'][:80]}")
    return jsonify(holder["rfi"]), 201


@app.route("/api/runs/<run_id>/rfis/<rfi_no>", methods=["PUT", "DELETE"])
@login_required
def rfi_item(run_id, rfi_no):
    run, project = load_run(run_id, "engineer")
    _require_results(run)
    holder = {}
    if request.method == "DELETE":
        def remove(r):
            idx, _rfi = _find_rfi(r["results"], rfi_no)
            holder["rfi"] = r["results"]["rfi_items"].pop(idx)
            return {"results": r["results"]}
        mutate_run(run_id, remove, request.user["id"])
        for hook in RFI_DELETE_HOOKS:
            hook(run, project, holder["rfi"])
        audit("rfi.delete", "rfi", rfi_no, project["id"], f"Rev {run['seq']} {rfi_no}")
        return jsonify({"status": "deleted"})

    body = request.get_json(silent=True) or {}

    def change(r):
        results = r["results"]
        idx, rfi = _find_rfi(results, rfi_no)
        base = body.get("base_rev")
        if base is not None and int(base) != int(rfi.get("rev") or 0):
            raise Conflict({"rfi": rfi})
        if "original" not in rfi and not rfi.get("manual"):
            rfi["original"] = {k: rfi.get(k) for k in RFI_EDITABLE_FIELDS}
        changed = []
        for key in RFI_EDITABLE_FIELDS:
            if key not in body:
                continue
            value = body[key]
            if key == "attachments" and isinstance(value, str):
                value = [a.strip() for a in value.splitlines() if a.strip()]
            if key == "status" and value not in RFI_STATUSES:
                continue
            if rfi.get(key) != value:
                changed.append(key)
            rfi[key] = value
        _rebuild_description(rfi)
        rfi["edited_by"] = request.user["display_name"]
        rfi["edited_at"] = storage.now()
        rfi["rev"] = int(rfi.get("rev") or 0) + 1
        rfi["docx_stale"] = True
        results["rfi_items"][idx] = rfi
        holder["rfi"], holder["changed"] = rfi, changed
        return {"results": results}

    mutate_run(run_id, change, request.user["id"])
    if holder["changed"]:
        audit("rfi.edit", "rfi", rfi_no, project["id"], f"Rev {run['seq']} {rfi_no}: " + ", ".join(holder["changed"]))
    return jsonify(holder["rfi"])


RFI_DELETE_HOOKS: list = []


@app.route("/api/runs/<run_id>/rfis/<rfi_no>/revert", methods=["POST"])
@login_required
def rfi_revert(run_id, rfi_no):
    run, project = load_run(run_id, "engineer")
    _require_results(run)
    holder = {}

    def change(r):
        results = r["results"]
        idx, rfi = _find_rfi(results, rfi_no)
        original = rfi.pop("original", None)
        if not original:
            abort(409, description="There is no AI draft to revert to.")
        rfi.update(original)
        _rebuild_description(rfi)
        for k in ("edited_by", "edited_at"):
            rfi.pop(k, None)
        rfi["rev"] = int(rfi.get("rev") or 0) + 1
        rfi["docx_stale"] = True
        results["rfi_items"][idx] = rfi
        holder["rfi"] = rfi
        return {"results": results}

    mutate_run(run_id, change, request.user["id"])
    audit("rfi.revert", "rfi", rfi_no, project["id"], f"Rev {run['seq']} {rfi_no} back to the AI draft")
    return jsonify(holder["rfi"])


def write_rfi_docx(run, project, rfi, template_path, user_name=None):
    inputs = run.get("inputs") or {}
    results = run.get("results") or {}
    state_like = {
        "project_name": project["name"],
        "user_id": user_name or request.user["display_name"],
        "rfi_form_meta": inputs.get("rfi_form_meta") or {},
    }
    label_a = (results.get("document_a") or {}).get("label") or inputs.get("document_a_label") or "Document A"
    label_b = (results.get("document_b") or {}).get("label") or inputs.get("document_b_label") or "Document B"
    rfi = dict(rfi)
    if isinstance(rfi.get("attachments"), str):
        rfi["attachments"] = [a.strip() for a in rfi["attachments"].splitlines() if a.strip()]
    safe_no = secure_filename(str(rfi.get("log_no") or rfi["rfi_no"])) or "RFI"
    safe_subject = secure_filename(str(rfi.get("subject") or ""))[:50]
    name = f"{safe_no}_{safe_subject}_edited.docx" if safe_subject else f"{safe_no}_edited.docx"
    out = os.path.join(storage.run_dir(run["id"], "exports"), name)
    values = build_token_values(rfi, state_like, label_a, label_b)
    if rfi.get("log_no"):
        values["rfi_no"] = rfi["log_no"]
    fill_docx_template(template_path, out, values)
    file_store.store().sync(storage.run_key(run["id"], "exports", name))
    return name


def template_for(project):
    """The project's own RFI form if it has one, else the shared one."""
    for hook in TEMPLATE_RESOLVERS:
        path = hook(project)
        if path:
            return path
    return resolve_template_path()


TEMPLATE_RESOLVERS: list = []


@app.route("/api/runs/<run_id>/rfis/<rfi_no>/docx", methods=["POST"])
@login_required
def rfi_docx(run_id, rfi_no):
    run, project = load_run(run_id, "engineer")
    _require_results(run)
    template_path = template_for(project)
    if not template_path:
        return jsonify({"error": "No RFI form is loaded. Ask an administrator to add the project's .docx form."}), 400
    holder = {}

    def change(r):
        results = r["results"]
        idx, rfi = _find_rfi(results, rfi_no)
        try:
            name = write_rfi_docx(r, project, rfi, template_path)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            abort(500, description=f"Could not write the RFI document: {exc}")
        if rfi.get("docx_file") and rfi["docx_file"] != name and not rfi.get("original_docx_file"):
            rfi["original_docx_file"] = rfi["docx_file"]
        rfi["docx_file"] = name
        rfi["docx_stale"] = False
        results["rfi_items"][idx] = rfi
        holder["rfi"] = rfi
        return {"results": results}

    mutate_run(run_id, change, request.user["id"])
    return jsonify(holder["rfi"])


@app.route("/api/runs/<run_id>/docx-all", methods=["POST"])
@login_required
def rfi_docx_all(run_id):
    run, project = load_run(run_id, "engineer")
    _require_results(run)
    template_path = template_for(project)
    if not template_path:
        return jsonify({"error": "No RFI form is loaded. Ask an administrator to add the project's .docx form."}), 400
    holder = {"errors": []}

    def change(r):
        results = r["results"]
        holder["errors"] = []
        for rfi in results.get("rfi_items") or []:
            try:
                name = write_rfi_docx(r, project, rfi, template_path)
                if rfi.get("docx_file") and rfi["docx_file"] != name and not rfi.get("original_docx_file"):
                    rfi["original_docx_file"] = rfi["docx_file"]
                rfi["docx_file"] = name
                rfi["docx_stale"] = False
            except Exception as exc:  # noqa: BLE001
                holder["errors"].append(f"{rfi.get('rfi_no')}: {exc}")
        holder["items"] = results.get("rfi_items") or []
        return {"results": results}

    mutate_run(run_id, change, request.user["id"])
    return jsonify({"rfi_items": holder["items"], "errors": holder["errors"]})


def reviewed_discrepancies(run):
    results = run.get("results") or {}
    review = (run.get("review") or {}).get("findings", {})
    out = []
    for d in results.get("discrepancies") or []:
        r = review.get(d.get("id"), {})
        if r.get("status") == "dismissed":
            continue
        d = dict(d)
        if r.get("severity"):
            d["severity"] = r["severity"]
        d["review_status"] = r.get("status") or "open"
        d["review_note"] = r.get("note") or ""
        out.append(d)
    return out


@app.route("/api/runs/<run_id>/register", methods=["POST"])
@login_required
def run_register(run_id):
    """Re-write the discrepancy register + RFI log from the edited results (dismissed findings left out)."""
    import shutil
    from nodes.export import export_discrepancy_register
    from nodes.run_paths import scan_dirs

    run, project = load_run(run_id, "engineer")
    results = _require_results(run)
    export_dir = storage.run_dir(run_id, "exports")
    rfi_items = [
        {**rfi, "docx_path": os.path.join(export_dir, rfi["docx_file"]) if rfi.get("docx_file") else None}
        for rfi in results.get("rfi_items") or []
    ]
    with scan_dirs(output_dir=os.path.join(storage.DATA_DIR, "scratch", f"register_{uuid.uuid4().hex[:8]}")):
        delta = export_discrepancy_register({
            "discrepancies": reviewed_discrepancies(run),
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
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    file_store.store().sync(storage.run_key(run_id, "exports", name))

    def change(r):
        r["results"]["register_file"] = name
        return {"results": r["results"]}

    mutate_run(run_id, change, request.user["id"])
    return jsonify({"register_file": name})


@app.route("/api/runs/<run_id>/exports/<path:filename>")
@login_required
def run_export_download(run_id, filename):
    _run, project = load_run(run_id)
    audit("export.download", "run", run_id, project["id"], filename[:200])
    return send_from_directory(storage.run_dir(run_id, "exports"), filename, as_attachment=True)


@app.route("/api/runs/<run_id>/bundle")
@login_required
def run_bundle(run_id):
    """Register + every current RFI document in one zip."""
    run, project = load_run(run_id)
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
    audit("export.bundle", "run", run_id, project["id"], f"Rev {run['seq']} zip")
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
    """Liveness probe for load balancers and monitoring (no sign-in, no details)."""
    try:
        storage.count_users()
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return jsonify({"status": "running" if db_ok else "degraded", "service": "ConflictScan / RFI Automation",
                    "version": VERSION, "database": db_ok}), (200 if db_ok else 503)


@app.route("/api/rfi-template", methods=["GET"])
@login_required
def rfi_template_status():
    """Report which RFI form is in use and which tokens it contains."""
    project = None
    if request.args.get("project"):
        project, _role = load_project(request.args["project"])
    path = template_for(project) if project else resolve_template_path()
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
    except Exception as exc:  # noqa: BLE001
        tokens = []
        print(f"[api] Could not read tokens from template: {exc}")
    return jsonify({"loaded": True, "filename": os.path.basename(path), "tokens": tokens})


# Admin console and the later-phase modules register their routes here.
import admin_api  # noqa: E402

admin_api.register(app)


def register_extensions():
    """Optional modules (RFI log, collaboration, fleet admin...) - each adds its routes and hooks."""
    import importlib
    for name in ("project_settings", "revisions", "rfi_log", "collab", "fleet_api"):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name:
                continue
            raise
        mod.register(app)


register_extensions()


if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("STARTING CONFLICTSCAN / RFI AUTOMATION (development server)")
    print("=" * 80)
    print("Web Interface: http://localhost:5000      (production: python serve.py)")
    template = resolve_template_path()
    print(f"RFI form:      {os.path.basename(template) if template else 'none loaded (templates_rfi/)'}")
    import db as _db
    print(f"Database:      {_db.describe()}")
    print(f"Files:         {file_store.store().describe()}")
    print(f"Scan workers:  {jobs.WORKERS} (embedded)")
    print("=" * 80 + "\n")

    # Debug mode (auto-reloader) is opt-in: the reloader restarts the server - and with it every
    # running scan - whenever a source file changes. Production runs serve.py (waitress).
    debug = os.environ.get("CONFLICTSCAN_DEBUG", "0").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=int(os.environ.get("CONFLICTSCAN_PORT", "5000")), debug=debug,
            use_reloader=debug)
