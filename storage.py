"""
Persistence - ConflictScan / RFI Automation
============================================
All database access goes through this module (SQLAlchemy Core, any engine -
see db.py). Files go through adapters/files.py; the database stores only
their keys.

    users            accounts: role (admin | manager | engineer | viewer),
                     status (active | pending | disabled), local or directory
    user_prefs       per-user UI state: open tabs, pinned projects, theme
    projects         a named piece of work, owned by one user; "team" projects
                     are visible to everyone, "private" ones to members only
    project_members  per-project roles that override the global role
    runs             one scan of a project (a "revision"): inputs, results,
                     the engineers' edits, progress
    run_files        files uploaded to a run (key in the file store)
    jobs             the scan / upload-check queue shared by all workers
    audit_log        who did what, when
    login_attempts   failed sign-ins (throttling works across processes)
    presence         who is looking at what right now

Windows compatible.
"""

import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

import db
from adapters import files as file_store
from db import (app_settings, audit_log, engine, jobs, login_attempts, presence, project_members, projects, run_files,
                runs, user_prefs, users)

DATA_DIR = db.DATA_DIR
RUNS_DIR = os.path.join(file_store.store().root, "runs")

RUN_STATUSES = ("draft", "queued", "running", "complete", "failed", "cancelled")
ACTIVE_STATUSES = ("queued", "running")
ROLES = ("admin", "manager", "engineer", "viewer")
PROJECT_ROLES = ("manager", "engineer", "viewer")
USER_STATUSES = ("active", "pending", "disabled")
_RANK = {"viewer": 1, "engineer": 2, "manager": 3}


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


@contextmanager
def tx():
    with engine.begin() as conn:
        yield conn


def _d(row) -> Optional[Dict[str, Any]]:
    return dict(row._mapping) if row is not None else None


def _all(conn, stmt) -> List[Dict[str, Any]]:
    return [dict(r._mapping) for r in conn.execute(stmt)]


def init_db() -> None:
    """Create or upgrade the schema to the latest migration (and import a pre-migration SQLite file)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    legacy = _detect_legacy_sqlite()
    upgrade_schema()
    if legacy:
        _import_legacy_sqlite(legacy)


def alembic_config():
    from alembic.config import Config
    root = os.path.dirname(os.path.abspath(__file__))
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(root, "migrations"))
    cfg.set_main_option("sqlalchemy.url", db.DATABASE_URL.replace("%", "%%"))
    return cfg


def upgrade_schema(revision: str = "head") -> None:
    from alembic import command
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision)


def schema_revision() -> Optional[str]:
    from alembic.runtime.migration import MigrationContext
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"version_table_schema": db.SCHEMA})
        return ctx.get_current_revision()


def file_path(f: Dict[str, Any]) -> str:
    """Ordinary path of a run file (downloaded into the cache for remote stores)."""
    return file_store.store().local_path(f["path"])


def run_dir(run_id: str, *parts: str) -> str:
    """A run's folder for outputs (exports/, debug/); call file_store.store().sync(key) after writing."""
    return file_store.store().local_dir("/".join(("runs", run_id) + parts))


def run_key(run_id: str, *parts: str) -> str:
    return "/".join(("runs", run_id) + parts)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(username: str, display_name: str, password_hash: Optional[str], role: str = "engineer",
                status: str = "active", auth_source: str = "local", external_id: str = None,
                email: str = None, must_change_password: bool = False) -> Dict[str, Any]:
    user = {
        "id": new_id(), "username": username, "username_key": username.lower(),
        "display_name": display_name or username, "email": email, "password_hash": password_hash,
        "auth_source": auth_source, "external_id": external_id,
        "role": role if role in ROLES else "engineer", "status": status if status in USER_STATUSES else "active",
        "must_change_password": bool(must_change_password), "created_at": now(),
    }
    with tx() as conn:
        conn.execute(insert(users).values(**user))
    return get_user(user["id"])


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    with tx() as conn:
        return _d(conn.execute(select(users).where(users.c.id == user_id)).first())


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    with tx() as conn:
        return _d(conn.execute(select(users).where(users.c.username_key == (username or "").lower())).first())


def get_user_by_external(source: str, external_id: str) -> Optional[Dict[str, Any]]:
    with tx() as conn:
        return _d(conn.execute(select(users).where(and_(users.c.auth_source == source,
                                                        users.c.external_id == external_id))).first())


PUBLIC_USER_COLUMNS = (users.c.id, users.c.username, users.c.display_name, users.c.role, users.c.status,
                       users.c.email, users.c.auth_source, users.c.created_at, users.c.last_seen_at)


def list_users(include_inactive: bool = False) -> List[Dict[str, Any]]:
    stmt = select(*PUBLIC_USER_COLUMNS).order_by(users.c.display_name)
    if not include_inactive:
        stmt = stmt.where(users.c.status == "active")
    with tx() as conn:
        return _all(conn, stmt)


def count_users() -> int:
    with tx() as conn:
        return conn.execute(select(func.count()).select_from(users)).scalar_one()


def count_admins() -> int:
    with tx() as conn:
        return conn.execute(select(func.count()).select_from(users).where(
            and_(users.c.role == "admin", users.c.status == "active"))).scalar_one()


def touch_user(user_id: str) -> None:
    with tx() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(last_seen_at=now()))


def update_user(user_id: str, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in (
        "display_name", "password_hash", "role", "status", "email", "must_change_password", "external_id",
        "auth_source")}
    if not allowed:
        return
    with tx() as conn:
        conn.execute(update(users).where(users.c.id == user_id).values(**allowed))


def get_prefs(user_id: str) -> Dict[str, Any]:
    with tx() as conn:
        rows = conn.execute(select(user_prefs.c.key, user_prefs.c.value).where(user_prefs.c.user_id == user_id))
        return {r.key: r.value for r in rows}


def set_prefs(user_id: str, prefs: Dict[str, Any]) -> None:
    with tx() as conn:
        for key, value in prefs.items():
            done = conn.execute(update(user_prefs).where(and_(user_prefs.c.user_id == user_id,
                                                              user_prefs.c.key == key)).values(value=value))
            if done.rowcount == 0:
                conn.execute(insert(user_prefs).values(user_id=user_id, key=key, value=value))


# ---------------------------------------------------------------------------
# Projects, membership and permissions
# ---------------------------------------------------------------------------

_owner = users.alias("owner")
_PROJECT_COLS = (projects, _owner.c.display_name.label("owner_name"))


def _project_select():
    return select(*_PROJECT_COLS).select_from(projects.outerjoin(_owner, _owner.c.id == projects.c.owner_id))


def create_project(name: str, owner_id: str, description: str = "", visibility: str = "team",
                   code: str = None, settings: Dict[str, Any] = None) -> Dict[str, Any]:
    stamp = now()
    project = {
        "id": new_id(), "name": name, "code": code or None, "description": description or "", "owner_id": owner_id,
        "visibility": visibility if visibility in ("team", "private") else "team",
        "archived": False, "settings": settings or {}, "created_at": stamp, "updated_at": stamp,
    }
    with tx() as conn:
        conn.execute(insert(projects).values(**project))
    return get_project(project["id"])


def get_project(project_id: str, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
    stmt = _project_select().where(projects.c.id == project_id)
    if not include_deleted:
        stmt = stmt.where(projects.c.deleted_at.is_(None))
    with tx() as conn:
        return _d(conn.execute(stmt).first())


def update_project(project_id: str, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in (
        "name", "description", "visibility", "archived", "code", "settings", "owner_id", "deleted_at", "deleted_by")}
    if not allowed:
        return
    allowed["updated_at"] = now()
    if "archived" in allowed:
        allowed["archived"] = bool(allowed["archived"])
    with tx() as conn:
        conn.execute(update(projects).where(projects.c.id == project_id).values(**allowed))


def touch_project(project_id: str) -> None:
    with tx() as conn:
        conn.execute(update(projects).where(projects.c.id == project_id).values(updated_at=now()))


def _delete_runs(conn, run_ids: List[str]) -> None:
    if not run_ids:
        return
    conn.execute(delete(run_files).where(run_files.c.run_id.in_(run_ids)))
    conn.execute(delete(jobs).where(jobs.c.target_id.in_(run_ids)))
    _delete_run_children(conn, run_ids)
    conn.execute(delete(runs).where(runs.c.id.in_(run_ids)))


def _delete_run_children(conn, run_ids: List[str]) -> None:
    """Rows in later-phase tables that hang off a run (hook filled in by those phases)."""
    for hook in _RUN_DELETE_HOOKS:
        hook(conn, run_ids)


_RUN_DELETE_HOOKS: list = []
_PROJECT_DELETE_HOOKS: list = []


def delete_project(project_id: str) -> None:
    """Permanent delete (the trash calls this after the retention period)."""
    with tx() as conn:
        run_ids = [r.id for r in conn.execute(select(runs.c.id).where(runs.c.project_id == project_id))]
        _delete_runs(conn, run_ids)
        for hook in _PROJECT_DELETE_HOOKS:
            hook(conn, project_id)
        conn.execute(delete(project_members).where(project_members.c.project_id == project_id))
        conn.execute(delete(projects).where(projects.c.id == project_id))
    for run_id in run_ids:
        file_store.store().delete_prefix(run_key(run_id))


def add_member(project_id: str, user_id: str, role: str, added_by: str = None) -> None:
    role = role if role in PROJECT_ROLES else "engineer"
    with tx() as conn:
        done = conn.execute(update(project_members).where(and_(
            project_members.c.project_id == project_id, project_members.c.user_id == user_id)).values(role=role))
        if done.rowcount == 0:
            conn.execute(insert(project_members).values(project_id=project_id, user_id=user_id, role=role,
                                                        added_by=added_by, added_at=now()))


def remove_member(project_id: str, user_id: str) -> None:
    with tx() as conn:
        conn.execute(delete(project_members).where(and_(project_members.c.project_id == project_id,
                                                        project_members.c.user_id == user_id)))


def list_members(project_id: str) -> List[Dict[str, Any]]:
    stmt = (select(project_members.c.user_id, project_members.c.role, project_members.c.added_at,
                   users.c.username, users.c.display_name, users.c.status)
            .select_from(project_members.join(users, users.c.id == project_members.c.user_id))
            .where(project_members.c.project_id == project_id).order_by(users.c.display_name))
    with tx() as conn:
        return _all(conn, stmt)


def member_role(project_id: str, user_id: str) -> Optional[str]:
    with tx() as conn:
        return conn.execute(select(project_members.c.role).where(and_(
            project_members.c.project_id == project_id, project_members.c.user_id == user_id))).scalar()


def project_role(project: Optional[Dict[str, Any]], user: Dict[str, Any], membership: Optional[str] = "?") -> Optional[str]:
    """
    The user's effective role in a project: manager | engineer | viewer | None (no access).

    admin -> manager everywhere; the owner -> manager; a membership role wins next;
    otherwise "team" projects give the global role (viewer stays viewer), "private" ones nothing.
    """
    if not project or not user:
        return None
    if user.get("role") == "admin" or project["owner_id"] == user["id"]:
        return "manager"
    if membership == "?":
        membership = member_role(project["id"], user["id"])
    if membership:
        if user.get("role") == "viewer":
            return "viewer"
        return membership
    if project["visibility"] == "team":
        return {"manager": "manager", "engineer": "engineer"}.get(user.get("role"), "viewer")
    return None


def can(role: Optional[str], need: str) -> bool:
    return bool(role) and _RANK.get(role, 0) >= _RANK[need]


def can_view(project: Optional[Dict[str, Any]], user) -> bool:
    if isinstance(user, str):
        user = get_user(user)
    return project_role(project, user) is not None


def list_projects_for(user, include_deleted: bool = False) -> List[Dict[str, Any]]:
    """Every project the user can see, each with its effective role in `my_role`."""
    if isinstance(user, str):
        user = get_user(user)
    stmt = _project_select().order_by(projects.c.updated_at.desc())
    stmt = stmt.where(projects.c.deleted_at.isnot(None) if include_deleted else projects.c.deleted_at.is_(None))
    with tx() as conn:
        mine = {r.project_id: r.role for r in conn.execute(
            select(project_members.c.project_id, project_members.c.role).where(project_members.c.user_id == user["id"]))}
        if user.get("role") != "admin":
            stmt = stmt.where(or_(projects.c.owner_id == user["id"], projects.c.visibility == "team",
                                  projects.c.id.in_(list(mine) or [""])))
        rows = _all(conn, stmt)
    for p in rows:
        p["my_role"] = project_role(p, user, mine.get(p["id"]))
    return [p for p in rows if p["my_role"]]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

_cu = users.alias("cu")
_uu = users.alias("uu")
_RUN_SUMMARY = (runs.c.id, runs.c.project_id, runs.c.seq, runs.c.title, runs.c.status, runs.c.created_by,
                runs.c.updated_by, runs.c.parent_run_id, runs.c.error, runs.c.version, runs.c.created_at,
                runs.c.updated_at, runs.c.queued_at, runs.c.started_at, runs.c.finished_at, runs.c.progress,
                _cu.c.display_name.label("created_by_name"), _uu.c.display_name.label("updated_by_name"))
_RUN_FROM = runs.outerjoin(_cu, _cu.c.id == runs.c.created_by).outerjoin(_uu, _uu.c.id == runs.c.updated_by)


def run_counts(results: Optional[Dict[str, Any]], review: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Headline numbers for lists: findings by severity, open findings, RFIs."""
    results = results or {}
    findings_review = (review or {}).get("findings", {})
    counts = {"critical": 0, "major": 0, "minor": 0, "findings": 0, "open": 0, "dismissed": 0,
              "rfis": len(results.get("rfi_items") or [])}
    for d in results.get("discrepancies") or []:
        counts["findings"] += 1
        sev = (findings_review.get(d.get("id"), {}).get("severity") or d.get("severity") or "minor").lower()
        if sev in counts:
            counts[sev] += 1
        status = findings_review.get(d.get("id"), {}).get("status") or "open"
        if status == "dismissed":
            counts["dismissed"] += 1
        elif status == "open":
            counts["open"] += 1
    return counts


def create_run(project_id: str, user_id: str, inputs: Dict[str, Any], title: str = "",
               parent_run_id: str = None) -> Dict[str, Any]:
    for attempt in range(5):                 # two people adding a revision at once: retry the number
        stamp = now()
        run_id = new_id()
        try:
            with tx() as conn:
                seq = (conn.execute(select(func.max(runs.c.seq)).where(runs.c.project_id == project_id)).scalar() or 0) + 1
                conn.execute(insert(runs).values(
                    id=run_id, project_id=project_id, seq=seq, title=title or "", status="draft", created_by=user_id,
                    updated_by=user_id, parent_run_id=parent_run_id, inputs=inputs or {}, review={}, progress={},
                    version=1, created_at=stamp, updated_at=stamp))
                conn.execute(update(projects).where(projects.c.id == project_id).values(updated_at=stamp))
            return get_run(run_id)
        except IntegrityError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError("unreachable")


def get_run(run_id: str, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
    stmt = (select(runs, _cu.c.display_name.label("created_by_name"), _uu.c.display_name.label("updated_by_name"))
            .select_from(_RUN_FROM).where(runs.c.id == run_id))
    if not include_deleted:
        stmt = stmt.where(runs.c.deleted_at.is_(None))
    with tx() as conn:
        return _d(conn.execute(stmt).first())


RUN_FIELDS = ("title", "status", "error", "queued_at", "started_at", "finished_at", "inputs", "results",
              "review", "progress", "deleted_at", "deleted_by")


def update_run(run_id: str, user_id: str = None, expect_version: int = None, bump: bool = True, **fields) -> bool:
    """Update a run; with expect_version, only if nobody changed it since (returns False on a conflict)."""
    values = {k: v for k, v in fields.items() if k in RUN_FIELDS}
    if not values:
        return True
    stamp = now()
    values["updated_at"] = stamp
    if user_id:
        values["updated_by"] = user_id
    if bump:
        values["version"] = runs.c.version + 1
    cond = runs.c.id == run_id
    if expect_version is not None:
        cond = and_(cond, runs.c.version == expect_version)
    with tx() as conn:
        done = conn.execute(update(runs).where(cond).values(**values))
        if done.rowcount == 0:
            return False
        if bump:
            project_id = conn.execute(select(runs.c.project_id).where(runs.c.id == run_id)).scalar()
            conn.execute(update(projects).where(projects.c.id == project_id).values(updated_at=stamp))
    return True


def set_progress(run_id: str, progress: Dict[str, Any]) -> None:
    """Live progress from a worker - no version bump, so it never conflicts with an engineer's edit."""
    with tx() as conn:
        conn.execute(update(runs).where(runs.c.id == run_id).values(progress=progress))


def delete_run(run_id: str) -> None:
    """Permanent delete (the trash calls this after the retention period)."""
    with tx() as conn:
        _delete_runs(conn, [run_id])
    file_store.store().delete_prefix(run_key(run_id))


def list_runs(project_id: str = None, project_ids: List[str] = None, with_counts: bool = False,
              include_deleted: bool = False) -> List[Dict[str, Any]]:
    """Run summaries (no inputs/results payload) for one project or several."""
    cols = _RUN_SUMMARY + ((runs.c.results, runs.c.review) if with_counts else ())
    stmt = select(*cols).select_from(_RUN_FROM)
    if not include_deleted:
        stmt = stmt.where(runs.c.deleted_at.is_(None))
    if project_id:
        stmt = stmt.where(runs.c.project_id == project_id)
    elif project_ids is not None:
        if not project_ids:
            return []
        stmt = stmt.where(runs.c.project_id.in_(project_ids))
    stmt = stmt.order_by(runs.c.project_id, runs.c.seq.desc())
    with tx() as conn:
        rows = _all(conn, stmt)
    if with_counts:
        for r in rows:
            r["counts"] = run_counts(r.pop("results", None), r.pop("review", None))
    return rows


def runs_with_status(statuses) -> List[Dict[str, Any]]:
    stmt = (select(*_RUN_SUMMARY).select_from(_RUN_FROM)
            .where(and_(runs.c.status.in_(list(statuses)), runs.c.deleted_at.is_(None))).order_by(runs.c.queued_at))
    with tx() as conn:
        return _all(conn, stmt)


# ---------------------------------------------------------------------------
# Run files
# ---------------------------------------------------------------------------

def add_run_file(run_id: str, slot: str, name: str, path: str, kind: str, size: int,
                 detected_kind: str = None, detected_reason: str = None, pages: int = None,
                 problem: str = None, inspected_at: str = None, sha256: str = None) -> Dict[str, Any]:
    """`path` is the file's key in the file store (an absolute path inside the store is converted)."""
    if os.path.isabs(path):
        path = file_store.store().key_for(path)
    file_id = new_id()
    with tx() as conn:
        position = (conn.execute(select(func.max(run_files.c.position)).where(and_(
            run_files.c.run_id == run_id, run_files.c.slot == slot))).scalar())
        position = 0 if position is None else position + 1
        conn.execute(insert(run_files).values(
            id=file_id, run_id=run_id, slot=slot, name=name, path=path, kind=kind, detected_kind=detected_kind,
            detected_reason=detected_reason, size=size, position=position, pages=pages, problem=problem,
            sha256=sha256, inspected_at=inspected_at, created_at=now()))
    return get_run_file(file_id)


def list_run_files(run_id: str) -> List[Dict[str, Any]]:
    with tx() as conn:
        return _all(conn, select(run_files).where(run_files.c.run_id == run_id)
                    .order_by(run_files.c.slot, run_files.c.position))


def get_run_file(file_id: str) -> Optional[Dict[str, Any]]:
    with tx() as conn:
        return _d(conn.execute(select(run_files).where(run_files.c.id == file_id)).first())


def uninspected_file_ids() -> List[str]:
    with tx() as conn:
        return [r.id for r in conn.execute(select(run_files.c.id).where(run_files.c.inspected_at.is_(None)))]


def update_run_file(file_id: str, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in (
        "kind", "slot", "position", "detected_kind", "detected_reason", "pages", "problem", "inspected_at", "sha256")}
    if not allowed:
        return
    with tx() as conn:
        conn.execute(update(run_files).where(run_files.c.id == file_id).values(**allowed))


def delete_run_file(file_id: str) -> None:
    f = get_run_file(file_id)
    with tx() as conn:
        conn.execute(delete(run_files).where(run_files.c.id == file_id))
        if f:
            still_used = conn.execute(select(func.count()).select_from(run_files).where(
                run_files.c.path == f["path"])).scalar_one()
    if f and not still_used:
        file_store.store().delete(f["path"])


def copy_run_files(src_run_id: str, dst_run_id: str, skip_ids: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """Give a cloned run its own copy of the source run's uploads (hard links where possible)."""
    store = file_store.store()
    out = []
    skip = set(skip_ids)
    for f in list_run_files(src_run_id):
        if f["id"] in skip or not store.exists(f["path"]):
            continue
        dst_key = run_key(dst_run_id, "inputs", f["path"].rsplit("/", 1)[-1])
        store.copy(f["path"], dst_key)
        out.append(add_run_file(dst_run_id, f["slot"], f["name"], dst_key, f["kind"], f["size"],
                                f.get("detected_kind"), f.get("detected_reason"), f.get("pages"),
                                f.get("problem"), f.get("inspected_at"), f.get("sha256")))
    return out


# ---------------------------------------------------------------------------
# Job queue (shared by every worker process)
# ---------------------------------------------------------------------------

_order_lock = threading.Lock()
_last_order = [0]


def _next_order() -> int:
    """Strictly increasing within a process (Windows clocks tick every ~15 ms), time-based across processes."""
    with _order_lock:
        value = max(time.time_ns() // 1000, _last_order[0] + 1)
        _last_order[0] = value
        return value


def enqueue_job(kind: str, target_id: str, priority: int = 0, max_attempts: int = 1,
                created_by: str = None, delay_seconds: int = 0) -> Dict[str, Any]:
    job = {"id": new_id(), "kind": kind, "target_id": target_id, "status": "queued", "priority": priority,
           "queue_order": _next_order(),
           "attempts": 0, "max_attempts": max_attempts, "cancel_requested": False,
           "run_after": (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat(timespec="seconds"),
           "created_by": created_by, "created_at": now()}
    with tx() as conn:
        conn.execute(insert(jobs).values(**job))
    return job


def claim_job(kinds: Iterable[str], worker: str) -> Optional[Dict[str, Any]]:
    """Atomically take the next queued job (highest priority, then oldest). Safe across processes."""
    kinds = list(kinds)
    for _ in range(5):
        with tx() as conn:
            candidate = conn.execute(
                select(jobs.c.id).where(and_(jobs.c.status == "queued", jobs.c.kind.in_(kinds),
                                             jobs.c.run_after <= now()))
                .order_by(jobs.c.priority.desc(), jobs.c.queue_order, jobs.c.id).limit(1)).scalar()
            if not candidate:
                return None
            stamp = now()
            won = conn.execute(update(jobs).where(and_(jobs.c.id == candidate, jobs.c.status == "queued")).values(
                status="running", locked_by=worker, locked_at=stamp, heartbeat_at=stamp,
                attempts=jobs.c.attempts + 1)).rowcount
            if won:
                return _d(conn.execute(select(jobs).where(jobs.c.id == candidate)).first())
    return None


def heartbeat_jobs(job_ids: List[str]) -> None:
    if job_ids:
        with tx() as conn:
            conn.execute(update(jobs).where(jobs.c.id.in_(job_ids)).values(heartbeat_at=now()))


def finish_job(job_id: str, status: str, error: str = None, retry_in: int = None) -> None:
    values: Dict[str, Any] = {"error": error}
    if retry_in is not None:
        values.update(status="queued", locked_by=None, locked_at=None,
                      run_after=(datetime.now(timezone.utc) + timedelta(seconds=retry_in)).isoformat(timespec="seconds"))
    else:
        values.update(status=status, finished_at=now())
    with tx() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(**values))


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with tx() as conn:
        return _d(conn.execute(select(jobs).where(jobs.c.id == job_id)).first())


def active_job_for(target_id: str, kind: str = None) -> Optional[Dict[str, Any]]:
    stmt = select(jobs).where(and_(jobs.c.target_id == target_id, jobs.c.status.in_(("queued", "running"))))
    if kind:
        stmt = stmt.where(jobs.c.kind == kind)
    with tx() as conn:
        return _d(conn.execute(stmt.order_by(jobs.c.created_at.desc())).first())


def request_job_cancel(job_id: str) -> None:
    with tx() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(cancel_requested=True))


def cancel_queued_job(job_id: str) -> bool:
    with tx() as conn:
        return conn.execute(update(jobs).where(and_(jobs.c.id == job_id, jobs.c.status == "queued")).values(
            status="cancelled", finished_at=now(), cancel_requested=True)).rowcount == 1


def set_job_priority(job_id: str, priority: int) -> None:
    with tx() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(priority=int(priority)))


def list_jobs(statuses: Iterable[str] = ("queued", "running"), kind: str = None, limit: int = 200) -> List[Dict[str, Any]]:
    stmt = select(jobs).where(jobs.c.status.in_(list(statuses)))
    if kind:
        stmt = stmt.where(jobs.c.kind == kind)
    stmt = stmt.order_by(jobs.c.status.desc(), jobs.c.priority.desc(), jobs.c.queue_order).limit(limit)
    with tx() as conn:
        return _all(conn, stmt)


def stale_running_jobs(older_than_seconds: int) -> List[Dict[str, Any]]:
    with tx() as conn:
        return _all(conn, select(jobs).where(and_(jobs.c.status == "running",
                                                  jobs.c.heartbeat_at < ago(seconds=older_than_seconds))))


def release_job(job_id: str, worker: str = None) -> bool:
    """Put a running job back in the queue (its worker died)."""
    cond = and_(jobs.c.id == job_id, jobs.c.status == "running")
    if worker:
        cond = and_(cond, jobs.c.locked_by == worker)
    with tx() as conn:
        return conn.execute(update(jobs).where(cond).values(status="queued", locked_by=None, locked_at=None,
                                                            run_after=now())).rowcount == 1


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def audit(action: str, user: Optional[Dict[str, Any]] = None, target_type: str = None, target_id: str = None,
          project_id: str = None, summary: str = "", details: Dict[str, Any] = None, ip: str = None) -> None:
    with tx() as conn:
        conn.execute(insert(audit_log).values(
            id=new_id(), at=now(), user_id=(user or {}).get("id"), username=(user or {}).get("username"),
            action=action, target_type=target_type, target_id=target_id, project_id=project_id,
            summary=(summary or "")[:500], details=details or None, ip=ip))


def list_audit(user_id: str = None, project_id: str = None, action: str = None, since: str = None,
               limit: int = 200, before: str = None) -> List[Dict[str, Any]]:
    stmt = select(audit_log)
    if user_id:
        stmt = stmt.where(audit_log.c.user_id == user_id)
    if project_id:
        stmt = stmt.where(audit_log.c.project_id == project_id)
    if action:
        stmt = stmt.where(audit_log.c.action.like(f"{action}%"))
    if since:
        stmt = stmt.where(audit_log.c.at >= since)
    if before:
        stmt = stmt.where(audit_log.c.at < before)
    with tx() as conn:
        return _all(conn, stmt.order_by(audit_log.c.at.desc(), audit_log.c.id).limit(min(limit, 1000)))


# ---------------------------------------------------------------------------
# Sign-in attempts and presence
# ---------------------------------------------------------------------------

def record_login_failure(keys: Iterable[str]) -> None:
    stamp = now()
    with tx() as conn:
        for key in keys:
            conn.execute(insert(login_attempts).values(id=new_id(), key=key[:200], at=stamp))


def login_failures_since(key: str, since: str) -> List[str]:
    with tx() as conn:
        return [r.at for r in conn.execute(select(login_attempts.c.at).where(and_(
            login_attempts.c.key == key[:200], login_attempts.c.at >= since)).order_by(login_attempts.c.at))]


def clear_login_failures(key: str = None) -> None:
    with tx() as conn:
        stmt = delete(login_attempts)
        if key:
            stmt = stmt.where(login_attempts.c.key == key[:200])
        conn.execute(stmt)


def touch_presence(user_id: str, target: str, detail: str = "") -> None:
    with tx() as conn:
        done = conn.execute(update(presence).where(and_(presence.c.user_id == user_id, presence.c.target == target))
                            .values(at=now(), detail=detail[:120]))
        if done.rowcount == 0:
            conn.execute(insert(presence).values(user_id=user_id, target=target, detail=detail[:120], at=now()))


def present_on(target: str, within_seconds: int = 45) -> List[Dict[str, Any]]:
    stmt = (select(presence.c.user_id, presence.c.detail, presence.c.at, users.c.display_name, users.c.username)
            .select_from(presence.join(users, users.c.id == presence.c.user_id))
            .where(and_(presence.c.target == target, presence.c.at >= ago(seconds=within_seconds))))
    with tx() as conn:
        return _all(conn, stmt)


def clear_presence(user_id: str, target: str) -> None:
    with tx() as conn:
        conn.execute(delete(presence).where(and_(presence.c.user_id == user_id, presence.c.target == target)))


# ---------------------------------------------------------------------------
# Admin-console settings
# ---------------------------------------------------------------------------

_settings_cache: Dict[str, Any] = {}


def get_setting(key: str, default: Any = None, max_age: float = 10.0) -> Any:
    hit = _settings_cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    try:
        with tx() as conn:
            value = conn.execute(select(app_settings.c.value).where(app_settings.c.key == key)).scalar()
    except Exception:  # noqa: BLE001 - e.g. before the first migration
        value = None
    _settings_cache[key] = (time.time(), value)
    return default if value is None else value


def set_setting(key: str, value: Any, user_id: str = None) -> None:
    with tx() as conn:
        done = conn.execute(update(app_settings).where(app_settings.c.key == key)
                            .values(value=value, updated_at=now(), updated_by=user_id))
        if done.rowcount == 0:
            conn.execute(insert(app_settings).values(key=key, value=value, updated_at=now(), updated_by=user_id))
    _settings_cache.pop(key, None)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def purge(audit_days: int = None, jobs_days: int = None) -> Dict[str, int]:
    out = {}
    with tx() as conn:
        out["login_attempts"] = conn.execute(delete(login_attempts).where(login_attempts.c.at < ago(days=1))).rowcount
        out["presence"] = conn.execute(delete(presence).where(presence.c.at < ago(hours=1))).rowcount
        if jobs_days:
            out["jobs"] = conn.execute(delete(jobs).where(and_(
                jobs.c.status.in_(("done", "failed", "cancelled")), jobs.c.finished_at < ago(days=jobs_days)))).rowcount
        if audit_days:
            out["audit_log"] = conn.execute(delete(audit_log).where(audit_log.c.at < ago(days=audit_days))).rowcount
    return out


# ---------------------------------------------------------------------------
# Databases created before migrations existed (SQLite, first release)
# ---------------------------------------------------------------------------

def _detect_legacy_sqlite() -> Optional[str]:
    if not db.IS_SQLITE:
        return None
    path = engine.url.database
    if not path or not os.path.isfile(path):
        return None
    con = sqlite3.connect(path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in con.execute("PRAGMA table_info(users)")} if "users" in tables else set()
    finally:
        con.close()
    if "users" in tables and "alembic_version" not in tables and "username_key" not in cols:
        engine.dispose()
        moved = path.replace(".db", f".legacy-{datetime.now():%Y%m%d%H%M%S}.db")
        os.replace(path, moved)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.replace(path + suffix, moved + suffix)
        return moved
    return None


def _import_legacy_sqlite(path: str) -> None:
    """Copy a first-release SQLite database into the current schema (the old file is kept beside it)."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    store = file_store.store()
    try:
        old_users = [dict(r) for r in con.execute("SELECT * FROM users ORDER BY created_at")]
        with tx() as conn:
            for i, u in enumerate(old_users):
                conn.execute(insert(users).values(
                    id=u["id"], username=u["username"], username_key=u["username"].lower(),
                    display_name=u["display_name"], password_hash=u["password_hash"], auth_source="local",
                    role="admin" if i == 0 else "engineer", status="active", must_change_password=False,
                    created_at=u["created_at"], last_seen_at=u.get("last_seen_at")))
            for r in con.execute("SELECT * FROM user_prefs"):
                import json
                conn.execute(insert(user_prefs).values(user_id=r["user_id"], key=r["key"],
                                                       value=json.loads(r["value_json"])))
            for p in con.execute("SELECT * FROM projects"):
                conn.execute(insert(projects).values(
                    id=p["id"], name=p["name"], description=p["description"] or "", owner_id=p["owner_id"],
                    visibility=p["visibility"], archived=bool(p["archived"]), settings={},
                    created_at=p["created_at"], updated_at=p["updated_at"]))
            import json
            for r in con.execute("SELECT * FROM runs"):
                status = r["status"] if r["status"] not in ACTIVE_STATUSES else "failed"
                conn.execute(insert(runs).values(
                    id=r["id"], project_id=r["project_id"], seq=r["seq"], title=r["title"] or "", status=status,
                    created_by=r["created_by"], updated_by=r["updated_by"], parent_run_id=r["parent_run_id"],
                    inputs=json.loads(r["inputs_json"] or "{}"),
                    results=json.loads(r["results_json"]) if r["results_json"] else None,
                    review=json.loads(r["review_json"] or "{}"), progress=json.loads(r["progress_json"] or "{}"),
                    error=r["error"], version=1, created_at=r["created_at"], updated_at=r["updated_at"],
                    queued_at=r["queued_at"], started_at=r["started_at"], finished_at=r["finished_at"]))
            fcols = {c[1] for c in con.execute("PRAGMA table_info(run_files)")}
            for f in con.execute("SELECT * FROM run_files"):
                f = dict(f)
                p = f["path"]
                key = store.key_for(p) if os.path.isabs(p) and os.path.abspath(p).startswith(store.root) else p
                conn.execute(insert(run_files).values(
                    id=f["id"], run_id=f["run_id"], slot=f["slot"], name=f["name"], path=key, kind=f["kind"],
                    detected_kind=f["detected_kind"], detected_reason=f["detected_reason"], size=f["size"],
                    position=f["position"], pages=f.get("pages") if "pages" in fcols else None,
                    problem=f.get("problem") if "problem" in fcols else None,
                    inspected_at=f.get("inspected_at") if "inspected_at" in fcols else now(),
                    created_at=f["created_at"]))
    finally:
        con.close()
    print(f"[storage] Imported the earlier database {os.path.basename(path)} into the current schema.")
