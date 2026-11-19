"""
Retention and clean-up
======================
Run hourly by every worker (and on demand: `python manage.py purge`).

    CONFLICTSCAN_RETAIN_SCRATCH_HOURS  leftover scan scratch folders     (default 24)
    CONFLICTSCAN_RETAIN_JOBS_DAYS      finished rows in the job queue    (default 30)
    CONFLICTSCAN_RETAIN_AUDIT_DAYS     audit log entries                 (default 730)
    CONFLICTSCAN_RETAIN_DEBUG_DAYS     drawing-reader debug logs         (default 30)
    CONFLICTSCAN_RETAIN_TRASH_DAYS     deleted projects/revisions/RFIs   (default 30)

0 keeps that kind of record forever.

Windows compatible.
"""

import os
import shutil
import time
from typing import Dict

import storage
from adapters import files as file_store


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# Later phases register extra clean-up steps (e.g. emptying the trash).
EXTRA_STEPS: list = []


def purge_scratch(hours: int) -> int:
    import jobs
    root = jobs._scratch_root()
    running = {j["id"] for j in storage.list_jobs(("running",))}
    cutoff = time.time() - hours * 3600
    n = 0
    for name in os.listdir(root):
        parts = name.split("_")
        if len(parts) > 1 and parts[1] in running:
            continue
        path = os.path.join(root, name)
        if os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            n += 1
    return n


def purge_debug_logs(days: int) -> int:
    store = file_store.store()
    runs_root = store.local_path("runs")
    if not os.path.isdir(runs_root):
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for run_id in os.listdir(runs_root):
        debug = os.path.join(runs_root, run_id, "debug")
        if os.path.isdir(debug) and os.path.getmtime(debug) < cutoff:
            shutil.rmtree(debug, ignore_errors=True)
            n += 1
    return n


def run_all() -> Dict[str, int]:
    out: Dict[str, int] = {}
    scratch = _env_int("CONFLICTSCAN_RETAIN_SCRATCH_HOURS", 24)
    if scratch:
        out["scratch_folders"] = purge_scratch(scratch)
    debug = _env_int("CONFLICTSCAN_RETAIN_DEBUG_DAYS", 30)
    if debug:
        out["debug_logs"] = purge_debug_logs(debug)
    out.update(storage.purge(audit_days=_env_int("CONFLICTSCAN_RETAIN_AUDIT_DAYS", 730) or None,
                             jobs_days=_env_int("CONFLICTSCAN_RETAIN_JOBS_DAYS", 30) or None))
    for step in EXTRA_STEPS:
        out.update(step() or {})
    return out
