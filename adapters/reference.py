"""
Reference data adapter - read-only company data
===============================================
Lets engineers pick a project from the company register (code, name, client,
consultant, location) instead of typing it, and fills the RFI form header
from it. ConflictScan never writes to these sources.

CONFLICTSCAN_REFERENCE chooses the source:

    local   a JSON file (default: config/reference_data.json) - the stand-in
            until IT says where the register lives. Same shape as below.
    sql     a read-only database connection (any SQLAlchemy URL):
              CONFLICTSCAN_REFERENCE_URL            mssql+pyodbc://reader:...@erp/...
              CONFLICTSCAN_REFERENCE_PROJECTS_SQL   SELECT code, name, client, consultant,
                                                    location FROM dbo.Projects
                                                    WHERE name LIKE :q OR code LIKE :q
              CONFLICTSCAN_REFERENCE_EMPLOYEES_SQL  SELECT username, display_name, email,
                                                    department FROM ... WHERE ... LIKE :q
            The queries must return those column names (use AS to rename).
    none    no reference data

Record shapes:
    project  {code, name, client, consultant, location}
    employee {username, display_name, email, department}

Windows compatible.
"""

import json
import os
from typing import Any, Dict, List

import config

DEFAULT_FILE = os.path.join(config.ROOT, "config", "reference_data.json")
PROJECT_FIELDS = ("code", "name", "client", "consultant", "location")
EMPLOYEE_FIELDS = ("username", "display_name", "email", "department")


def backend() -> str:
    return os.environ.get("CONFLICTSCAN_REFERENCE", "local").lower()


def _file() -> str:
    return os.environ.get("CONFLICTSCAN_REFERENCE_FILE") or DEFAULT_FILE


def _local(kind: str) -> List[Dict[str, Any]]:
    path = _file()
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f).get(kind, [])


def _match(rec: Dict[str, Any], q: str, fields) -> bool:
    q = q.lower()
    return not q or any(q in str(rec.get(k) or "").lower() for k in fields)


_engine = None


def _sql(query_env: str, q: str, fields) -> List[Dict[str, Any]]:
    global _engine
    from sqlalchemy import create_engine, text
    query = os.environ.get(query_env)
    if not query:
        return []
    if _engine is None:
        _engine = create_engine(os.environ["CONFLICTSCAN_REFERENCE_URL"], pool_pre_ping=True)
    with _engine.connect() as conn:
        rows = conn.execute(text(query), {"q": f"%{q}%"}).mappings().fetchmany(50)
    return [{k: r.get(k) for k in fields} for r in rows]


def search_projects(q: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    b = backend()
    if b == "sql":
        rows = _sql("CONFLICTSCAN_REFERENCE_PROJECTS_SQL", q, PROJECT_FIELDS)
    elif b == "local":
        rows = [r for r in _local("projects") if _match(r, q, ("code", "name", "client"))]
    else:
        rows = []
    return [{k: r.get(k) or "" for k in PROJECT_FIELDS} for r in rows[:limit]]


def search_employees(q: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    b = backend()
    if b == "sql":
        rows = _sql("CONFLICTSCAN_REFERENCE_EMPLOYEES_SQL", q, EMPLOYEE_FIELDS)
    elif b == "local":
        rows = [r for r in _local("employees") if _match(r, q, ("username", "display_name", "email"))]
    else:
        rows = []
    return [{k: r.get(k) or "" for k in EMPLOYEE_FIELDS} for r in rows[:limit]]


def health() -> Dict[str, Any]:
    b = backend()
    if b == "none":
        return {"ok": True, "state": "off", "detail": "No reference data configured"}
    if b == "local":
        n = len(_local("projects"))
        return {"ok": True, "state": "local stand-in",
                "detail": f"{n} project(s) in {os.path.relpath(_file(), config.ROOT)} - waiting for IT's project register"}
    try:
        n = len(search_projects(""))
        return {"ok": True, "state": "live", "detail": f"Read-only SQL source reachable ({n} project rows sampled)"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "state": "unreachable", "detail": f"Reference database: {exc}"}
