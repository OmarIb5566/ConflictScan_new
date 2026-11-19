"""
Persistence - ConflictScan / RFI Automation
============================================
A small SQLite store so the web app can serve several engineers at once and
keep every scan they have run:

    users       sign-in accounts (password hashed with werkzeug)
    user_prefs  per-user UI state: open workspace tabs, pinned projects, theme
    projects    a named piece of work; owned by one user, visible to the team
                unless marked private
    runs        one scan of a project (a "revision"). Holds the inputs the
                engineer entered, the results the graph produced, and the
                engineer's edits to those results (RFI text, finding triage).
    run_files   files uploaded to a run, kept on disk under data/runs/<run>/
                inputs/ so a run can be re-opened, cloned and re-run without
                uploading again.

Everything lives under CONFLICTSCAN_DATA_DIR (default ./data). One connection
per call keeps it safe to use from Flask request threads and the scan workers.
"""

import json
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Next to the code by default (not the working directory, which is System32 for a Windows service).
DATA_DIR = os.environ.get("CONFLICTSCAN_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "conflictscan.db")
RUNS_DIR = os.path.join(DATA_DIR, "runs")

RUN_STATUSES = ("draft", "queued", "running", "complete", "failed", "cancelled")
ACTIVE_STATUSES = ("queued", "running")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS user_prefs (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value_json TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner_id    TEXT NOT NULL REFERENCES users(id),
    visibility  TEXT NOT NULL DEFAULT 'team',
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'draft',
    created_by    TEXT REFERENCES users(id),
    updated_by    TEXT REFERENCES users(id),
    parent_run_id TEXT,
    inputs_json   TEXT NOT NULL DEFAULT '{}',
    results_json  TEXT,
    review_json   TEXT NOT NULL DEFAULT '{}',
    progress_json TEXT NOT NULL DEFAULT '{}',
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    queued_at     TEXT,
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, seq);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS run_files (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    slot           TEXT NOT NULL,
    name           TEXT NOT NULL,
    path           TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'auto',
    detected_kind  TEXT,
    detected_reason TEXT,
    size           INTEGER NOT NULL DEFAULT 0,
    position       INTEGER NOT NULL DEFAULT 0,
    pages          INTEGER,
    problem        TEXT,
    inspected_at   TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_files_run ON run_files(run_id, slot, position);
"""

JSON_COLUMNS = {
    "inputs_json": "inputs",
    "results_json": "results",
    "review_json": "review",
    "progress_json": "progress",
    "value_json": "value",
}


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(RUNS_DIR, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # Columns added after the first release (databases created before them).
        have = {row[1] for row in conn.execute("PRAGMA table_info(run_files)")}
        for column, ddl in (("pages", "INTEGER"), ("problem", "TEXT"), ("inspected_at", "TEXT")):
            if column not in have:
                conn.execute(f"ALTER TABLE run_files ADD COLUMN {column} {ddl}")


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    out = {}
    for key in row.keys():
        value = row[key]
        if key in JSON_COLUMNS:
            out[JSON_COLUMNS[key]] = json.loads(value) if value else None
        else:
            out[key] = value
    return out


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def run_dir(run_id: str, *parts: str) -> str:
    path = os.path.join(RUNS_DIR, run_id, *parts)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(username: str, display_name: str, password_hash: str) -> Dict[str, Any]:
    user = {
        "id": new_id(), "username": username, "display_name": display_name or username,
        "password_hash": password_hash, "created_at": now(),
    }
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, password_hash, created_at) VALUES (?,?,?,?,?)",
            (user["id"], user["username"], user["display_name"], user["password_hash"], user["created_at"]),
        )
    return user


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())


def list_users() -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT id, username, display_name FROM users ORDER BY display_name").fetchall()
    return [dict(r) for r in rows]


def count_users() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def touch_user(user_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (now(), user_id))


def update_user(user_id: str, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("display_name", "password_hash")}
    if not allowed:
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with connect() as conn:
        conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*allowed.values(), user_id))


def get_prefs(user_id: str) -> Dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT key, value_json FROM user_prefs WHERE user_id = ?", (user_id,)).fetchall()
    return {r["key"]: json.loads(r["value_json"]) for r in rows}


def set_prefs(user_id: str, prefs: Dict[str, Any]) -> None:
    with connect() as conn:
        for key, value in prefs.items():
            conn.execute(
                "INSERT INTO user_prefs (user_id, key, value_json) VALUES (?,?,?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value_json = excluded.value_json",
                (user_id, key, _dump(value)),
            )


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def create_project(name: str, owner_id: str, description: str = "", visibility: str = "team") -> Dict[str, Any]:
    stamp = now()
    project = {
        "id": new_id(), "name": name, "description": description or "", "owner_id": owner_id,
        "visibility": visibility if visibility in ("team", "private") else "team",
        "archived": 0, "created_at": stamp, "updated_at": stamp,
    }
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id, name, description, owner_id, visibility, archived, created_at, updated_at) "
            "VALUES (:id, :name, :description, :owner_id, :visibility, :archived, :created_at, :updated_at)",
            project,
        )
    return project


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT p.*, u.display_name AS owner_name FROM projects p "
            "LEFT JOIN users u ON u.id = p.owner_id WHERE p.id = ?", (project_id,),
        ).fetchone())


def update_project(project_id: str, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("name", "description", "visibility", "archived")}
    if not allowed:
        return
    allowed["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with connect() as conn:
        conn.execute(f"UPDATE projects SET {sets} WHERE id = ?", (*allowed.values(), project_id))


def touch_project(project_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now(), project_id))


def delete_project(project_id: str) -> None:
    run_ids = [r["id"] for r in list_runs(project_id)]
    with connect() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    for run_id in run_ids:
        shutil.rmtree(os.path.join(RUNS_DIR, run_id), ignore_errors=True)


def list_projects_for(user_id: str) -> List[Dict[str, Any]]:
    """Every project the user can see: their own, plus the team's shared ones."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.*, u.display_name AS owner_name FROM projects p "
            "LEFT JOIN users u ON u.id = p.owner_id "
            "WHERE p.owner_id = ? OR p.visibility = 'team' ORDER BY p.updated_at DESC",
            (user_id,),
        ).fetchall()
    return [_row(r) for r in rows]


def can_view(project: Optional[Dict[str, Any]], user_id: str) -> bool:
    return bool(project) and (project["owner_id"] == user_id or project["visibility"] == "team")


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

RUN_SUMMARY_COLUMNS = (
    "r.id, r.project_id, r.seq, r.title, r.status, r.created_by, r.updated_by, r.parent_run_id, "
    "r.error, r.created_at, r.updated_at, r.queued_at, r.started_at, r.finished_at, r.progress_json, "
    "cu.display_name AS created_by_name, uu.display_name AS updated_by_name"
)


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
    stamp = now()
    with connect() as conn:
        seq = (conn.execute("SELECT COALESCE(MAX(seq), 0) FROM runs WHERE project_id = ?",
                            (project_id,)).fetchone()[0] or 0) + 1
        run_id = new_id()
        conn.execute(
            "INSERT INTO runs (id, project_id, seq, title, status, created_by, updated_by, parent_run_id, "
            "inputs_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, project_id, seq, title or "", "draft", user_id, user_id, parent_run_id,
             _dump(inputs or {}), stamp, stamp),
        )
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (stamp, project_id))
    return get_run(run_id)


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT r.*, cu.display_name AS created_by_name, uu.display_name AS updated_by_name "
            "FROM runs r LEFT JOIN users cu ON cu.id = r.created_by "
            "LEFT JOIN users uu ON uu.id = r.updated_by WHERE r.id = ?", (run_id,),
        ).fetchone()
    return _row(row)


def update_run(run_id: str, user_id: str = None, **fields) -> None:
    columns = {}
    for key, value in fields.items():
        if key in ("inputs", "results", "review", "progress"):
            columns[f"{key}_json"] = _dump(value)
        elif key in ("title", "status", "error", "queued_at", "started_at", "finished_at"):
            columns[key] = value
    if not columns:
        return
    columns["updated_at"] = now()
    if user_id:
        columns["updated_by"] = user_id
    sets = ", ".join(f"{k} = ?" for k in columns)
    with connect() as conn:
        conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*columns.values(), run_id))
        conn.execute(
            "UPDATE projects SET updated_at = ? WHERE id = (SELECT project_id FROM runs WHERE id = ?)",
            (columns["updated_at"], run_id),
        )


def delete_run(run_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    shutil.rmtree(os.path.join(RUNS_DIR, run_id), ignore_errors=True)


def list_runs(project_id: str = None, project_ids: List[str] = None, with_counts: bool = False) -> List[Dict[str, Any]]:
    """Run summaries (no inputs/results payload) for one project or several."""
    cols = RUN_SUMMARY_COLUMNS + (", r.results_json, r.review_json" if with_counts else "")
    sql = (f"SELECT {cols} FROM runs r LEFT JOIN users cu ON cu.id = r.created_by "
           "LEFT JOIN users uu ON uu.id = r.updated_by ")
    params: tuple = ()
    if project_id:
        sql += "WHERE r.project_id = ? "
        params = (project_id,)
    elif project_ids is not None:
        if not project_ids:
            return []
        sql += f"WHERE r.project_id IN ({','.join('?' * len(project_ids))}) "
        params = tuple(project_ids)
    sql += "ORDER BY r.project_id, r.seq DESC"
    with connect() as conn:
        rows = [_row(r) for r in conn.execute(sql, params).fetchall()]
    if with_counts:
        for r in rows:
            r["counts"] = run_counts(r.pop("results", None), r.pop("review", None))
    return rows


def runs_with_status(statuses) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {RUN_SUMMARY_COLUMNS} FROM runs r LEFT JOIN users cu ON cu.id = r.created_by "
            "LEFT JOIN users uu ON uu.id = r.updated_by "
            f"WHERE r.status IN ({','.join('?' * len(statuses))}) ORDER BY r.queued_at",
            tuple(statuses),
        ).fetchall()
    return [_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Run files
# ---------------------------------------------------------------------------

def add_run_file(run_id: str, slot: str, name: str, path: str, kind: str, size: int,
                 detected_kind: str = None, detected_reason: str = None, pages: int = None,
                 problem: str = None, inspected_at: str = None) -> Dict[str, Any]:
    with connect() as conn:
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM run_files WHERE run_id = ? AND slot = ?",
            (run_id, slot),
        ).fetchone()[0]
        file_id = new_id()
        conn.execute(
            "INSERT INTO run_files (id, run_id, slot, name, path, kind, detected_kind, detected_reason, size, "
            "position, pages, problem, inspected_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (file_id, run_id, slot, name, path, kind, detected_kind, detected_reason, size, position,
             pages, problem, inspected_at, now()),
        )
        return _row(conn.execute("SELECT * FROM run_files WHERE id = ?", (file_id,)).fetchone())


def list_run_files(run_id: str) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM run_files WHERE run_id = ? ORDER BY slot, position", (run_id,)
        ).fetchall()
    return [_row(r) for r in rows]


def get_run_file(file_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM run_files WHERE id = ?", (file_id,)).fetchone())


def uninspected_file_ids() -> List[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT id FROM run_files WHERE inspected_at IS NULL")]


def update_run_file(file_id: str, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in (
        "kind", "slot", "position", "detected_kind", "detected_reason", "pages", "problem", "inspected_at")}
    if not allowed:
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with connect() as conn:
        conn.execute(f"UPDATE run_files SET {sets} WHERE id = ?", (*allowed.values(), file_id))


def delete_run_file(file_id: str) -> None:
    f = get_run_file(file_id)
    with connect() as conn:
        conn.execute("DELETE FROM run_files WHERE id = ?", (file_id,))
    if f and os.path.isfile(f["path"]):
        try:
            os.remove(f["path"])
        except OSError:
            pass


def copy_run_files(src_run_id: str, dst_run_id: str) -> None:
    """Give a cloned run its own copy of the source run's uploads."""
    dst_dir = run_dir(dst_run_id, "inputs")
    for f in list_run_files(src_run_id):
        if not os.path.isfile(f["path"]):
            continue
        dst_path = os.path.join(dst_dir, os.path.basename(f["path"]))
        try:
            os.link(f["path"], dst_path)       # same volume: no extra disk space
        except OSError:
            shutil.copy2(f["path"], dst_path)
        add_run_file(dst_run_id, f["slot"], f["name"], dst_path, f["kind"], f["size"],
                     f.get("detected_kind"), f.get("detected_reason"), f.get("pages"),
                     f.get("problem"), f.get("inspected_at"))
