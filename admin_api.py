"""
Admin console API
=================
    /api/admin/users              list, create (with a temporary password), approve,
                                  disable, change role, reset password
    /api/admin/queue              every queued/running job; cancel, move to top, priority
    /api/admin/health             database, file store, sign-in source, reference data,
                                  model servers and GPUs, job queue, workers
    /api/admin/audit              the audit log, filterable
    /api/admin/settings           effective settings (secrets masked), schema revision

Everything here is admin-only and every change is written to the audit log.

Windows compatible.
"""

import os
import secrets
import time

from flask import jsonify, request
from werkzeug.security import generate_password_hash

import config
import db
import jobs
import storage
from adapters import files as file_store
from adapters import identity, model_servers, reference
from webcore import admin_required, audit, public_user


def _temp_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _admin_user_payload(u):
    return {k: u.get(k) for k in ("id", "username", "display_name", "email", "role", "status", "auth_source",
                                  "created_at", "last_seen_at")}


def register(app):

    @app.route("/api/admin/users", methods=["GET", "POST"])
    @admin_required
    def admin_users():
        if request.method == "GET":
            return jsonify([_admin_user_payload(u) for u in storage.list_users(include_inactive=True)])
        body = request.get_json(silent=True) or {}
        import re
        username = (body.get("username") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{3,40}", username):
            return jsonify({"error": "Username: 3-40 letters, digits, dots, dashes or underscores."}), 400
        if storage.get_user_by_username(username):
            return jsonify({"error": "That username is taken."}), 409
        role = body.get("role") if body.get("role") in storage.ROLES else "engineer"
        temp = body.get("password") or _temp_password()
        user = storage.create_user(username, (body.get("display_name") or username).strip()[:80],
                                   generate_password_hash(temp), role=role, email=(body.get("email") or None),
                                   must_change_password=not body.get("password"))
        audit("admin.user_create", "user", user["id"], summary=f"created {username} as {role}")
        return jsonify({"user": _admin_user_payload(user), "temporary_password": None if body.get("password") else temp}), 201

    @app.route("/api/admin/users/<user_id>", methods=["PATCH"])
    @admin_required
    def admin_user_item(user_id):
        target = storage.get_user(user_id)
        if not target:
            return jsonify({"error": "No such user."}), 404
        body = request.get_json(silent=True) or {}
        fields = {}
        if "role" in body:
            if body["role"] not in storage.ROLES:
                return jsonify({"error": "Unknown role."}), 400
            fields["role"] = body["role"]
        if "status" in body:
            if body["status"] not in storage.USER_STATUSES:
                return jsonify({"error": "Unknown status."}), 400
            if user_id == request.user["id"] and body["status"] != "active":
                return jsonify({"error": "You cannot disable your own account."}), 400
            fields["status"] = body["status"]
        for k in ("display_name", "email"):
            if k in body:
                fields[k] = (str(body[k] or "").strip() or None) if k == "email" else str(body[k]).strip()[:80]
        losing_admin = target["role"] == "admin" and target["status"] == "active" and (
            fields.get("role", "admin") != "admin" or fields.get("status", "active") != "active")
        if losing_admin and storage.count_admins() <= 1:
            return jsonify({"error": "This is the last administrator. Make someone else an admin first."}), 400
        storage.update_user(user_id, **fields)
        audit("admin.user_update", "user", user_id,
              summary=f"{target['username']}: " + ", ".join(f"{k}={v}" for k, v in fields.items()))
        return jsonify(_admin_user_payload(storage.get_user(user_id)))

    @app.route("/api/admin/users/<user_id>/reset-password", methods=["POST"])
    @admin_required
    def admin_reset_password(user_id):
        target = storage.get_user(user_id)
        if not target:
            return jsonify({"error": "No such user."}), 404
        if target.get("auth_source") != "local":
            return jsonify({"error": "This account signs in with the company directory; reset it there."}), 400
        temp = _temp_password()
        storage.update_user(user_id, password_hash=generate_password_hash(temp), must_change_password=True)
        storage.clear_login_failures(f"u:{target['username'].lower()}")
        audit("admin.password_reset", "user", user_id, summary=f"reset password of {target['username']}")
        return jsonify({"temporary_password": temp})

    @app.route("/api/admin/queue")
    @admin_required
    def admin_queue():
        rows = storage.list_jobs(("queued", "running"))
        rows += storage.list_jobs(("done", "failed", "cancelled"), limit=30)
        out = []
        for j in rows:
            entry = {k: j[k] for k in ("id", "kind", "target_id", "status", "priority", "attempts", "max_attempts",
                                       "locked_by", "heartbeat_at", "created_at", "finished_at", "error",
                                       "cancel_requested", "run_after")}
            if j["kind"] == "scan":
                run = storage.get_run(j["target_id"], include_deleted=True)
                if run:
                    project = storage.get_project(run["project_id"], include_deleted=True) or {}
                    creator = storage.get_user(j["created_by"]) if j.get("created_by") else None
                    entry.update(run_seq=run["seq"], run_status=run["status"], project_id=run["project_id"],
                                 project_name=project.get("name"), by=(creator or {}).get("display_name"))
            out.append(entry)
        return jsonify(out)

    @app.route("/api/admin/queue/<job_id>", methods=["POST"])
    @admin_required
    def admin_queue_action(job_id):
        job = storage.get_job(job_id)
        if not job:
            return jsonify({"error": "No such job."}), 404
        action = (request.get_json(silent=True) or {}).get("action")
        if action == "cancel":
            if job["kind"] == "scan":
                jobs.cancel(job["target_id"])
            elif not storage.cancel_queued_job(job_id):
                storage.request_job_cancel(job_id)
        elif action == "top":
            top = max([j["priority"] for j in storage.list_jobs(("queued",), kind=job["kind"])] + [0])
            storage.set_job_priority(job_id, top + 1)
        elif action == "priority":
            storage.set_job_priority(job_id, int((request.get_json() or {}).get("priority", 0)))
        else:
            return jsonify({"error": "action must be cancel, top or priority"}), 400
        audit("admin.queue_" + action, "job", job_id, summary=f"{job['kind']} {job['target_id']}")
        return jsonify(storage.get_job(job_id))

    @app.route("/api/admin/health")
    @admin_required
    def admin_health():
        checks = []
        t = time.time()
        try:
            storage.count_users()
            rev = storage.schema_revision()
            checks.append({"name": "Database", "ok": True, "state": "live",
                           "detail": f"{db.describe()} - schema revision {rev} ({(time.time() - t) * 1000:.0f} ms)"})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "Database", "ok": False, "state": "error", "detail": str(exc)})
        fs = file_store.store().health()
        checks.append({"name": "File storage", "state": "live" if fs["ok"] else "error", **fs})
        checks.append({"name": "Sign-in", **identity.health()})
        checks.append({"name": "Reference data", **reference.health()})
        servers = model_servers.health()
        for s in servers:
            checks.append({"name": f"Model server {s['name']}", "ok": s["ok"], "state": "live" if s["ok"] else "unreachable",
                           "detail": f"{s['url']} - " + (f"{len(s['models'])} models installed, "
                                                         f"{len(s['loaded'])} loaded in GPU memory" if s["ok"]
                                                         else s.get("error", ""))})
        queued = storage.list_jobs(("queued",))
        running = storage.list_jobs(("running",))
        stale = storage.stale_running_jobs(jobs.STALE_SECONDS)
        workers = sorted({j["locked_by"] for j in running if j.get("locked_by")})
        checks.append({"name": "Job queue", "ok": not stale, "state": "live" if not stale else "stalled",
                       "detail": f"{len(queued)} queued, {len(running)} running"
                                 + (f", {len(stale)} without a heartbeat" if stale else "")})
        embedded = jobs._pool is not None
        checks.append({"name": "Workers", "ok": True, "state": "embedded" if embedded else "separate",
                       "detail": ("Scans run inside the web server" if embedded else "Scans run in worker services")
                                 + (f"; busy: {', '.join(workers)}" if workers else "")})
        for hook in HEALTH_HOOKS:
            checks.extend(hook() or [])
        return jsonify({"checks": checks, "servers": servers, "gpus": model_servers.gpus(),
                        "queue": {"queued": len(queued), "running": len(running)},
                        "version": config.VERSION})

    @app.route("/api/admin/audit")
    @admin_required
    def admin_audit():
        a = request.args
        rows = storage.list_audit(user_id=a.get("user") or None, project_id=a.get("project") or None,
                                  action=a.get("action") or None, since=a.get("since") or None,
                                  before=a.get("before") or None, limit=int(a.get("limit", 200)))
        return jsonify(rows)

    @app.route("/api/admin/settings")
    @admin_required
    def admin_settings():
        return jsonify({"settings": config.effective(), "settings_file": config.settings_path(),
                        "settings_file_found": os.path.isfile(config.settings_path()),
                        "schema_revision": storage.schema_revision(), "database": db.describe(),
                        "files": file_store.store().describe()})


HEALTH_HOOKS: list = []
