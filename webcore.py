"""
Request helpers shared by every API module
==========================================
Sign-in checks, role checks, loading a project/run the user may see, the
audit-log helper and conflict-safe updates of a run's JSON documents.

Roles
-----
Global role (users.role): admin | manager | engineer | viewer.
Effective role in a project (storage.project_role): manager | engineer | viewer.

    viewer    read everything in the project, download outputs
    engineer  + upload files, run scans, triage findings, edit RFIs
    manager   + rename/delete the project, change who sees it, manage members,
              delete anyone's revision, approve and issue RFIs
    admin     manager everywhere, plus the admin console

Windows compatible.
"""

import time
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple

from flask import abort, jsonify, request, session

import security
import storage


def current_user() -> Optional[Dict[str, Any]]:
    uid = session.get("uid")
    if not uid:
        return None
    user = storage.get_user(uid)
    if not user or user.get("status") != "active":
        session.clear()
        return None
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


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if request.user.get("role") != "admin":
            return jsonify({"error": "Only administrators can do this."}), 403
        return fn(*args, **kwargs)
    return wrapper


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": user["id"], "username": user["username"], "display_name": user["display_name"],
            "role": user.get("role", "engineer"), "email": user.get("email"),
            "auth_source": user.get("auth_source", "local"),
            "must_change_password": bool(user.get("must_change_password"))}


NEED_MESSAGES = {
    "engineer": "You have view-only access to this project.",
    "manager": "Only the project's managers can do this.",
}


def deny(need: str):
    abort(403, description=NEED_MESSAGES.get(need, "You do not have permission to do this."))


def load_project(project_id: str, need: str = "viewer") -> Tuple[Dict[str, Any], str]:
    """The project and the user's role in it; 404 when they cannot see it, 403 when the role is too low."""
    project = storage.get_project(project_id)
    role = storage.project_role(project, request.user)
    if not role:
        abort(404)
    if not storage.can(role, need):
        deny(need)
    project["my_role"] = role
    return project, role


def load_run(run_id: str, need: str = "viewer"):
    run = storage.get_run(run_id)
    if not run:
        abort(404)
    project, role = load_project(run["project_id"], need)
    return run, project


def audit(action: str, target_type: str = None, target_id: str = None, project_id: str = None,
          summary: str = "", details: Dict[str, Any] = None, user: Dict[str, Any] = None) -> None:
    try:
        storage.audit(action, user or getattr(request, "user", None), target_type, target_id, project_id,
                      summary, details, security.client_address())
    except Exception as exc:  # noqa: BLE001 - never fail the user's action because the log is busy
        print(f"[audit] could not record {action}: {exc}")


class Conflict(Exception):
    def __init__(self, payload: Dict[str, Any]):
        super().__init__("conflict")
        self.payload = payload


def mutate_run(run_id: str, change: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]], user_id: str = None,
               attempts: int = 8) -> Dict[str, Any]:
    """
    Read-modify-write of a run without losing anyone's edit, across threads and processes.

    `change(run)` edits the run's JSON in place and returns the fields to save
    (or None to save nothing). If another request saved the run in between,
    the change is re-applied to the fresh copy.
    """
    for i in range(attempts):
        run = storage.get_run(run_id)
        if not run:
            abort(404)
        fields = change(run)
        if not fields:
            return run
        if storage.update_run(run_id, user_id=user_id, expect_version=run["version"], **fields):
            run.update(fields)
            run["version"] += 1
            return run
        time.sleep(0.02 * (i + 1))
    abort(409, description="This revision is being edited by several people at once - try again.")
