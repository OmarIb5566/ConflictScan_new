"""
Moving data between databases - export, import, verify, backup, restore
=======================================================================
The export format is engine-neutral, so data moves between any two engines
(workstation SQLite -> the host's PostgreSQL / SQL Server, or back):

    conflictscan-export.zip
        manifest.json         format, app version, schema revision, time,
                              per table: row count + SHA-256 of its rows
        tables/<table>.jsonl  one JSON object per row, in primary-key order
        files/...             (backups only) the whole file store

Import loads every table in dependency order inside one transaction, then
re-reads the target and compares row counts and checksums with the
manifest; any difference rolls the import back.

Windows compatible.
"""

import hashlib
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, insert, select

import config
import db
import storage
from adapters import files as file_store

FORMAT = "conflictscan-export"
FORMAT_VERSION = 1


def _canonical(row: Dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _table_rows(conn, table) -> List[Dict[str, Any]]:
    pk = list(table.primary_key.columns)
    return [dict(r._mapping) for r in conn.execute(select(table).order_by(*pk))]


def table_digest(conn, table) -> Dict[str, Any]:
    h = hashlib.sha256()
    n = 0
    for row in _table_rows(conn, table):
        h.update(_canonical(row).encode("utf-8"))
        h.update(b"\n")
        n += 1
    return {"rows": n, "sha256": h.hexdigest()}


def snapshot(conn=None) -> Dict[str, Dict[str, Any]]:
    if conn is None:
        with db.engine.connect() as c:
            return snapshot(c)
    return {t.name: table_digest(conn, t) for t in db.ALL_TABLES}


def export_zip(out_path: str, include_files: bool = False, progress=print) -> Dict[str, Any]:
    manifest = {"format": FORMAT, "format_version": FORMAT_VERSION, "app_version": config.VERSION,
                "schema_revision": storage.schema_revision(), "source": db.describe(),
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tables": {},
                "includes_files": include_files}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        with db.engine.connect() as conn:
            for table in db.ALL_TABLES:
                h = hashlib.sha256()
                buf = io.StringIO()
                n = 0
                for row in _table_rows(conn, table):
                    line = _canonical(row)
                    buf.write(line + "\n")
                    h.update(line.encode("utf-8"))
                    h.update(b"\n")
                    n += 1
                zf.writestr(f"tables/{table.name}.jsonl", buf.getvalue())
                manifest["tables"][table.name] = {"rows": n, "sha256": h.hexdigest()}
                progress(f"  {table.name}: {n} rows")
        if include_files:
            store = file_store.store()
            count = 0
            for key in store.iter_keys("runs"):
                zf.write(store.local_path(key), f"files/{key}")
                count += 1
            for extra in ("project_files",):
                for key in store.iter_keys(extra) if os.path.isdir(store.local_path(extra)) else []:
                    zf.write(store.local_path(key), f"files/{key}")
                    count += 1
            manifest["file_count"] = count
            progress(f"  files: {count}")
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return manifest


def read_manifest(path: str) -> Dict[str, Any]:
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    if manifest.get("format") != FORMAT:
        raise ValueError("This is not a ConflictScan export.")
    return manifest


class VerificationError(Exception):
    pass


def import_zip(path: str, replace: bool = False, restore_files: bool = True, progress=print) -> Dict[str, Any]:
    """Load an export into the current DATABASE_URL (schema upgraded first). All-or-nothing."""
    manifest = read_manifest(path)
    storage.upgrade_schema(manifest["schema_revision"] or "head")
    known = {t.name: t for t in db.ALL_TABLES}
    with zipfile.ZipFile(path) as zf, db.engine.begin() as conn:
        existing = sum(conn.execute(select(func.count()).select_from(t)).scalar_one() for t in db.ALL_TABLES)
        if existing and not replace:
            raise ValueError(f"The target database already has {existing} rows. Use --replace to overwrite it.")
        for table in reversed(db.ALL_TABLES):
            conn.execute(delete(table))
        for table in db.ALL_TABLES:
            name = table.name
            if name not in manifest["tables"]:
                continue
            rows = [json.loads(line) for line in zf.read(f"tables/{name}.jsonl").decode("utf-8").splitlines() if line]
            for i in range(0, len(rows), 500):
                conn.execute(insert(known[name]), rows[i:i + 500])
            progress(f"  {name}: {len(rows)} rows loaded")
        got = snapshot(conn)
        problems = [f"{name}: expected {exp['rows']} rows / {exp['sha256'][:12]}, got {got[name]['rows']} / "
                    f"{got[name]['sha256'][:12]}" for name, exp in manifest["tables"].items()
                    if name in got and got[name] != exp]
        if problems:
            raise VerificationError("Verification failed - nothing was imported:\n  " + "\n  ".join(problems))
    storage.upgrade_schema("head")
    if restore_files and manifest.get("includes_files"):
        store = file_store.store()
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.startswith("files/") and not name.endswith("/"):
                    key = name[len("files/"):]
                    dest = store.local_path(key)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with zf.open(name) as src, open(dest, "wb") as out:
                        out.write(src.read())
                    store.sync(key)
        progress(f"  files restored: {manifest.get('file_count', 0)}")
    progress("Verified: every table's row count and checksum match the export.")
    return manifest


def verify_against(path: str) -> List[str]:
    """Compare the current database with an export; [] when identical."""
    manifest = read_manifest(path)
    got = snapshot()
    # Audit entries written after the export (e.g. by the restore itself) are not differences.
    with db.engine.connect() as conn:
        h = hashlib.sha256()
        n = 0
        for row in _table_rows(conn, db.audit_log):
            if row["at"] <= manifest["created_at"]:
                h.update(_canonical(row).encode("utf-8"))
                h.update(b"\n")
                n += 1
        got["audit_log"] = {"rows": n, "sha256": h.hexdigest()}
    return [f"{name}: export {exp['rows']} rows, database {got.get(name, {}).get('rows')}"
            for name, exp in manifest["tables"].items() if got.get(name) != exp]


def missing_files() -> List[str]:
    """Run files whose stored key no longer exists in the file store."""
    store = file_store.store()
    with db.engine.connect() as conn:
        keys = [r.path for r in conn.execute(select(db.run_files.c.path))]
    return [k for k in keys if not store.exists(k)]
