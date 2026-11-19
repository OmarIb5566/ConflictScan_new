"""
Scan jobs - ConflictScan / RFI Automation
==========================================
Scans and upload checks are rows in the `jobs` table. Any number of worker
processes (worker.py, normally one Windows service per GPU server) claim
them atomically, so the queue survives restarts and is shared by everyone.
For a single workstation the web server runs the same workers in-process
(CONFLICTSCAN_EMBEDDED_WORKER=1, the default).

    submit(run_id)        queue a scan (priority: admins can move it up)
    cancel(run_id)        queued: removed at once; running: stops after the
                          graph stage in progress
    inspect_file_later()  queue the background check of an uploaded file

Reliability:
- every running job sends a heartbeat; a job whose worker died (no heartbeat
  for CONFLICTSCAN_STALE_SECONDS) is put back in the queue, or failed once it
  has used its attempts;
- failures caused by an unreachable model server are retried with back-off
  (CONFLICTSCAN_MAX_ATTEMPTS, default 3); problems with the documents are not;
- progress and the live log are written to the run every second, so any web
  server process can show them.

Windows compatible.
"""

import contextvars
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import deque
from typing import Any, Dict, List, Optional

import storage
from adapters import files as file_store

STAGES = [
    ("receive_input", "Validate inputs"),
    ("analyze_documents", "Read & profile both sets"),
    ("conflict_scan", "Compare sets & find conflicts"),
    ("draft_rfi", "Draft RFIs"),
    ("export", "Write the register"),
]
STAGE_KEYS = [k for k, _ in STAGES]
LOG_LINES_KEPT = 400

WORKERS = max(1, int(os.environ.get("CONFLICTSCAN_WORKERS", "1")))
MAX_ATTEMPTS = max(1, int(os.environ.get("CONFLICTSCAN_MAX_ATTEMPTS", "3")))
HEARTBEAT_SECONDS = 10
STALE_SECONDS = int(os.environ.get("CONFLICTSCAN_STALE_SECONDS", "90"))
RETRY_BASE_SECONDS = int(os.environ.get("CONFLICTSCAN_RETRY_SECONDS", "30"))
# Per-scan drawing-reader diagnostics (runs/<id>/debug); deleted with the run.
DEBUG_LOGS = os.environ.get("CONFLICTSCAN_DEBUG_LOGS", "1").lower() not in ("0", "false", "no")
WORKER_NAME = f"{socket.gethostname()}:{os.getpid()}"

_live: Dict[str, Dict[str, Any]] = {}
_live_lock = threading.Lock()
_wake = threading.Event()


# ---------------------------------------------------------------------------
# Live log: tee print() output from a scan's thread into that run's log
# ---------------------------------------------------------------------------

_current_run = contextvars.ContextVar("conflictscan_run", default=None)


class _TeeStdout:
    def __init__(self, real):
        self._real = real
        self._partial: Dict[str, str] = {}

    def write(self, text):
        run_id = _current_run.get()
        if run_id:
            buf = self._partial.get(run_id, "") + text
            *lines, rest = buf.split("\n")
            self._partial[run_id] = rest
            for line in lines:
                _log(run_id, line)
        return self._real.write(text)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


def install_log_capture() -> None:
    if not isinstance(sys.stdout, _TeeStdout):
        sys.stdout = _TeeStdout(sys.stdout)


def _log(run_id: str, line: str) -> None:
    line = line.rstrip()
    if not line.strip() or set(line.strip()) <= set("=-"):
        return
    with _live_lock:
        live = _live.get(run_id)
        if live is not None:
            live["log"].append(line)
            live["message"] = line.strip()
            live["dirty"] = True


def _new_progress() -> Dict[str, Any]:
    return {
        "stages": [{"key": k, "label": label, "status": "pending"} for k, label in STAGES],
        "current": None,
        "message": "",
        "log": [],
    }


def live(run_id: str) -> Optional[Dict[str, Any]]:
    """Progress of a scan running in this process (other processes read it from the database)."""
    with _live_lock:
        entry = _live.get(run_id)
        if not entry:
            return None
        return {
            "stages": [dict(s) for s in entry["stages"]],
            "current": entry["current"],
            "message": entry["message"],
            "log": list(entry["log"])[-150:],
            "started_at": entry.get("started_at"),
            "attempt": entry.get("attempt"),
        }


def _persist_progress(run_id: str) -> None:
    snap = live(run_id)
    if snap:
        snap["log"] = snap["log"][-LOG_LINES_KEPT:]
        storage.set_progress(run_id, snap)
        with _live_lock:
            if run_id in _live:
                _live[run_id]["dirty"] = False


def _set_stage(run_id: str, key: str, status: str) -> None:
    with _live_lock:
        entry = _live.get(run_id)
        if not entry:
            return
        for stage in entry["stages"]:
            if stage["key"] == key:
                stage["status"] = status
        if status == "running":
            entry["current"] = key
        entry["dirty"] = True


# ---------------------------------------------------------------------------
# Queue API (used by the web app)
# ---------------------------------------------------------------------------

def submit(run_id: str, user_id: str = None, priority: int = 0) -> Dict[str, Any]:
    storage.update_run(run_id, status="queued", queued_at=storage.now(), started_at=None,
                       finished_at=None, error=None, progress=_new_progress())
    job = storage.enqueue_job("scan", run_id, priority=priority, max_attempts=MAX_ATTEMPTS, created_by=user_id)
    _wake.set()
    return job


def cancel(run_id: str) -> bool:
    run = storage.get_run(run_id)
    if not run or run["status"] not in storage.ACTIVE_STATUSES:
        return False
    job = storage.active_job_for(run_id, "scan")
    if job and storage.cancel_queued_job(job["id"]):
        storage.update_run(run_id, status="cancelled", finished_at=storage.now(), error="Cancelled before it started.")
        return True
    if job:
        storage.request_job_cancel(job["id"])
        _log(run_id, "Cancel requested - stopping after the current stage...")
        return True
    # No job behind it (e.g. lost in a crash before the queue existed): just close it.
    storage.update_run(run_id, status="cancelled", finished_at=storage.now(), error="Cancelled.")
    return True


def queue_position(run_id: str) -> int:
    queued = [j["target_id"] for j in storage.list_jobs(("queued",), kind="scan")]
    return queued.index(run_id) + 1 if run_id in queued else 0


def inspect_file_later(file_id: str) -> None:
    storage.enqueue_job("inspect", file_id, priority=10, max_attempts=2)
    _wake.set()


# ---------------------------------------------------------------------------
# Upload inspection
# ---------------------------------------------------------------------------

def inspect_file_now(file_id: str) -> Optional[Dict[str, Any]]:
    """Run upload_checks.inspect on one stored file and save the outcome; returns the updated record."""
    import hashlib
    import upload_checks

    f = storage.get_run_file(file_id)
    if not f or f.get("inspected_at"):
        return f
    store = file_store.store()
    if not store.exists(f["path"]):
        storage.update_run_file(file_id, problem="is missing from storage - upload it again",
                                inspected_at=storage.now())
        return storage.get_run_file(file_id)
    path = store.local_path(f["path"])
    result = upload_checks.inspect(path, os.path.splitext(f["name"])[1])
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    storage.update_run_file(file_id, inspected_at=storage.now(), sha256=digest.hexdigest(), **result)
    return storage.get_run_file(file_id)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def set_summary(result_state: dict, slot: str, files: list) -> dict:
    profile = result_state.get(f"profile_{slot}", {}) or {}
    return {
        "label": result_state.get(f"document_{slot}_label"),
        "source": profile.get("source_name") or " + ".join(f.get("name", "") for f in files),
        "file_count": len(files),
        "files": profile.get("files") or [
            {"name": f.get("name"), "kind": f.get("kind"), "classified_as": f.get("classified_as", "")} for f in files
        ],
        "char_count": profile.get("char_count", 0),
        "chunk_count": profile.get("chunk_count", 0),
        "requirement_count": len(profile.get("requirements", [])),
        "quantity_count": len(profile.get("quantities", [])),
        "standards": profile.get("standards", []),
    }


def _move_into(path: Optional[str], dest_dir: str) -> Optional[str]:
    """Move a file the graph wrote into the run's own exports folder (and the file store); returns its name."""
    if not path or not os.path.isfile(path):
        return None
    name = os.path.basename(path)
    dest = os.path.join(dest_dir, name)
    try:
        shutil.move(path, dest)
    except OSError:
        shutil.copy2(path, dest)
    store = file_store.store()
    store.sync(store.key_for(dest))
    return name


def build_results(result_state: Dict[str, Any], files_a: list, files_b: list, export_dir: str) -> Dict[str, Any]:
    from nodes.rfi_knowledge import issue_classes

    rfi_items = []
    for rfi in result_state.get("rfi_items", []) or []:
        rfi = dict(rfi)
        rfi["docx_file"] = _move_into(rfi.pop("docx_path", None), export_dir)
        rfi.setdefault("status", "draft")
        rfi_items.append(rfi)

    return {
        "project_name": result_state.get("project_name"),
        "document_a": set_summary(result_state, "a", files_a),
        "document_b": set_summary(result_state, "b", files_b),
        "scan_summary": result_state.get("scan_summary", ""),
        "confidence_level": result_state.get("confidence_level", "medium"),
        "warnings": result_state.get("analysis_warnings", []),
        "discrepancies": result_state.get("discrepancies", []),
        "issue_classes": {c["key"]: c["title"] for c in issue_classes(detectable_only=False)},
        "rfi_grouping": result_state.get("rfi_grouping"),
        "rfi_items": rfi_items,
        "register_file": _move_into(result_state.get("register_path"), export_dir),
        "steps_executed": result_state.get("step_history", []),
    }


def resolve_source_files(run_id: str, work_dir: str, inputs: Dict[str, Any]):
    """The run's stored uploads -> state.SourceFile lists, copied into a scratch folder for the graph."""
    from nodes.file_ingest import classify_file, pair_drawing_files

    store = file_store.store()
    sets: Dict[str, List[Dict[str, Any]]] = {"a": [], "b": []}
    problems = []
    for f in storage.list_run_files(run_id):
        if f["slot"] not in sets or not store.exists(f["path"]):
            continue
        if not f.get("inspected_at"):
            f = inspect_file_now(f["id"]) or f
        if f.get("problem"):
            problems.append(f"{f['name']} {f['problem']}")
            continue
        work_path = os.path.join(work_dir, os.path.basename(f["path"]))
        shutil.copy2(store.local_path(f["path"]), work_path)
        kind, reason = f["kind"], "chosen by the user"
        if kind == "auto":
            # Full classification (may ask the vision model about ambiguous scans);
            # the upload-time guess shown in the UI skips that call to stay fast.
            kind, reason = classify_file(work_path)
        sets[f["slot"]].append({"name": f["name"], "path": work_path, "kind": kind, "classified_as": reason,
                                "sha256": f.get("sha256"), "file_id": f["id"]})

    if problems:
        raise ValueError("Remove or replace these files and start again: " + "; ".join(problems))
    for slot in ("a", "b"):
        pasted = (inputs.get(f"document_{slot}_text") or "").strip()
        if pasted:
            sets[slot].append({"name": "pasted text", "kind": "text", "text": pasted, "classified_as": "pasted"})
        sets[slot] = pair_drawing_files(sets[slot])
    return sets["a"], sets["b"]


# ---------------------------------------------------------------------------
# Executing jobs
# ---------------------------------------------------------------------------

class _Retry(Exception):
    pass


def _is_transient(exc: BaseException) -> bool:
    """A failure worth retrying: the model server or the network, not the documents."""
    try:
        import requests
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
    except ImportError:
        pass
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    text = str(exc).lower()
    return any(s in text for s in ("connection refused", "connection aborted", "max retries exceeded",
                                   "timed out", "not reachable", "failed to establish", "server unavailable",
                                   "503 server error", "502 server error"))


def _cancel_checker(job_id: str):
    last = {"at": 0.0, "value": False}

    def should_stop() -> bool:
        if last["value"]:
            return True
        if time.time() - last["at"] >= 1.0:
            last["at"] = time.time()
            job = storage.get_job(job_id)
            last["value"] = bool(job and job.get("cancel_requested"))
        return last["value"]
    return should_stop


def _execute_scan(job: Dict[str, Any]) -> None:
    run_id = job["target_id"]
    run = storage.get_run(run_id)
    if not run or run["status"] not in ("queued", "running") or job.get("cancel_requested"):
        storage.finish_job(job["id"], "cancelled")
        return

    project = storage.get_project(run["project_id"]) or {}
    creator = storage.get_user(run["created_by"]) if run.get("created_by") else None
    inputs = run.get("inputs") or {}
    should_stop = _cancel_checker(job["id"])

    with _live_lock:
        _live[run_id] = {**_new_progress(), "log": deque(maxlen=LOG_LINES_KEPT), "started_at": storage.now(),
                         "attempt": job["attempts"], "dirty": True}
    storage.update_run(run_id, status="running", started_at=storage.now(), bump=False)
    install_log_capture()      # again here, in case something replaced sys.stdout since startup
    token = _current_run.set(run_id)
    from fleet import current_run as fleet_run
    fleet_token = fleet_run.set(run_id)          # every model call is logged against this scan
    work_dir = tempfile.mkdtemp(prefix=f"job_{job['id']}_", dir=_scratch_root())
    started = time.time()
    outcome, error = "done", None

    try:
        from graph import run_conflict_scan

        if job["attempts"] > 1:
            _log(run_id, f"Attempt {job['attempts']} of {job['max_attempts']}")
        files_a, files_b = resolve_source_files(run_id, work_dir, inputs)
        if not files_a or not files_b:
            missing = " and ".join(s for s, f in (("Set A", files_a), ("Set B", files_b)) if not f)
            raise ValueError(f"{missing} has no files or text. Add documents and start the scan again.")

        _set_stage(run_id, "receive_input", "running")
        _log(run_id, f"Starting scan: {len(files_a)} file(s) in Set A, {len(files_b)} in Set B (worker {WORKER_NAME})")

        def on_node_complete(node: str, delta: Dict[str, Any]) -> None:
            _set_stage(run_id, node, "done")
            if node == "conflict_scan" and not (delta.get("discrepancies") or []):
                _set_stage(run_id, "draft_rfi", "skipped")
                nxt = "export"
            else:
                idx = STAGE_KEYS.index(node) if node in STAGE_KEYS else -1
                nxt = STAGE_KEYS[idx + 1] if 0 <= idx < len(STAGE_KEYS) - 1 else None
            if nxt:
                _set_stage(run_id, nxt, "running")
            _persist_progress(run_id)

        meta = inputs.get("rfi_form_meta") or {}
        result_state = run_conflict_scan(
            user_id=(creator or {}).get("username") or "anonymous",
            document_a_text="",
            document_b_text="",
            document_a_label=(inputs.get("document_a_label") or "").strip() or "Document A",
            document_b_label=(inputs.get("document_b_label") or "").strip() or "Document B",
            document_a_files=files_a,
            document_b_files=files_b,
            project_name=project.get("name") or "Unnamed Project",
            review_focus=inputs.get("review_focus") or "",
            document_precedence=inputs.get("document_precedence") or "",
            rfi_grouping=inputs.get("rfi_grouping") or "consolidated",
            rfi_form_meta=meta,
            export_format="excel",
            on_node_complete=on_node_complete,
            should_stop=should_stop,
            output_dir=os.path.join(work_dir, "out"),
            debug_dir=storage.run_dir(run_id, "debug") if DEBUG_LOGS else None,
            **_scan_extras(run, project),
        )

        if result_state.get("error_message"):
            raise ValueError(result_state["error_message"])

        results = build_results(result_state, files_a, files_b, storage.run_dir(run_id, "exports"))
        results["duration_seconds"] = round(time.time() - started)
        results["fleet"] = fleet_report(run_id)
        results = _finalise_results(run, project, results)
        _log(run_id, f"Done: {len(results['discrepancies'])} finding(s), {len(results['rfi_items'])} RFI(s)")
        _persist_progress(run_id)
        storage.update_run(run_id, status="complete", results=results, finished_at=storage.now(), error=None)
        _after_scan(run_id, "complete")

    except Exception as exc:  # noqa: BLE001 - every failure must land on the run record
        cancelled = type(exc).__name__ == "ScanCancelled"
        retry = (not cancelled and _is_transient(exc) and job["attempts"] < job["max_attempts"])
        if not cancelled:
            traceback.print_exc()
            _log(run_id, f"ERROR: {exc}")
        with _live_lock:
            entry = _live.get(run_id)
            if entry:
                for stage in entry["stages"]:
                    if stage["status"] == "running":
                        stage["status"] = "pending" if retry else ("cancelled" if cancelled else "failed")
        if retry:
            wait = RETRY_BASE_SECONDS * job["attempts"]
            _log(run_id, f"The model server did not answer - retrying in {wait}s "
                         f"(attempt {job['attempts'] + 1} of {job['max_attempts']}).")
            _persist_progress(run_id)
            storage.update_run(run_id, status="queued", bump=False)
            outcome, error = "retry", str(exc)
            storage.finish_job(job["id"], "queued", error=str(exc), retry_in=wait)
        else:
            _persist_progress(run_id)
            storage.update_run(run_id, status="cancelled" if cancelled else "failed",
                               error="Cancelled by the user." if cancelled else str(exc), finished_at=storage.now())
            outcome, error = ("cancelled" if cancelled else "failed"), (None if cancelled else str(exc))
            _after_scan(run_id, outcome)
    finally:
        _current_run.reset(token)
        fleet_run.reset(fleet_token)
        shutil.rmtree(work_dir, ignore_errors=True)
        with _live_lock:
            _live.pop(run_id, None)
    if outcome != "retry":
        storage.finish_job(job["id"], outcome, error=error)


def fleet_report(run_id: str) -> Dict[str, Any]:
    """Which model did which job in this scan, how many calls, how long, how many from the cache."""
    try:
        from sqlalchemy import Integer, func, select
        from db import model_calls
        from fleet import profile
        with storage.tx() as conn:
            rows = conn.execute(select(model_calls.c.role, model_calls.c.model, func.count(), func.sum(model_calls.c.ms),
                                       func.sum(model_calls.c.cached.cast(Integer)),
                                       func.sum(model_calls.c.prompt_tokens), func.sum(model_calls.c.output_tokens))
                                .where(model_calls.c.run_id == run_id).group_by(model_calls.c.role, model_calls.c.model)).all()
        roles = [{"role": r[0], "model": r[1], "calls": r[2], "seconds": round((r[3] or 0) / 1000, 1),
                  "cached": int(r[4] or 0), "prompt_tokens": int(r[5] or 0), "output_tokens": int(r[6] or 0)}
                 for r in rows]
        return {"profile": profile.active_name(), "mode": profile.active()["mode"], "roles": roles,
                "calls": sum(x["calls"] for x in roles), "cached": sum(x["cached"] for x in roles),
                "model_seconds": round(sum(x["seconds"] for x in roles), 1)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


HOOK_MODULES = ("project_settings", "revisions", "rfi_log", "collab")


def load_hooks() -> None:
    """Import the modules that add scan hooks (a worker process does not import the web app)."""
    import importlib
    for name in HOOK_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise


# Hooks later phases plug into (fleet options, carry-forward, notifications).
_SCAN_EXTRAS: list = []
_FINALISERS: list = []
_AFTER_SCAN: list = []


def _scan_extras(run, project) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for hook in _SCAN_EXTRAS:
        out.update(hook(run, project) or {})
    return out


def _finalise_results(run, project, results):
    for hook in _FINALISERS:
        results = hook(run, project, results) or results
    return results


def _after_scan(run_id: str, outcome: str) -> None:
    for hook in _AFTER_SCAN:
        try:
            hook(run_id, outcome)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def _execute_inspect(job: Dict[str, Any]) -> None:
    try:
        inspect_file_now(job["target_id"])
        storage.finish_job(job["id"], "done")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        if job["attempts"] < job["max_attempts"]:
            storage.finish_job(job["id"], "queued", error=str(exc), retry_in=5)
        else:
            storage.update_run_file(job["target_id"], problem=f"could not be checked ({exc})", inspected_at=storage.now())
            storage.finish_job(job["id"], "failed", error=str(exc))


HANDLERS = {"scan": _execute_scan, "inspect": _execute_inspect}


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

class WorkerPool:
    """Threads that claim jobs from the shared queue. One per process (embedded or worker.py)."""

    def __init__(self, scan_threads: int = WORKERS, inspect_threads: int = 2, name: str = WORKER_NAME):
        self.name = name
        self.plan = [("scan", scan_threads), ("inspect", inspect_threads)]
        self._stop = threading.Event()
        self._running: Dict[str, str] = {}          # job id -> thread name
        self._lock = threading.Lock()
        self._threads: List[threading.Thread] = []

    def start(self) -> "WorkerPool":
        load_hooks()
        install_log_capture()
        recover_after_restart()
        for kind, count in self.plan:
            for i in range(count):
                t = threading.Thread(target=self._loop, args=(kind,), name=f"{kind}-{i}", daemon=True)
                t.start()
                self._threads.append(t)
        t = threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True)
        t.start()
        self._threads.append(t)
        return self

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        _wake.set()
        for t in self._threads:
            t.join(timeout)

    def running_jobs(self) -> List[str]:
        with self._lock:
            return list(self._running)

    def _loop(self, kind: str) -> None:
        while not self._stop.is_set():
            try:
                job = storage.claim_job([kind], self.name)
            except Exception:  # noqa: BLE001 - database blip: wait and retry
                traceback.print_exc()
                job = None
            if not job:
                _wake.wait(1.0)
                _wake.clear()
                continue
            with self._lock:
                self._running[job["id"]] = threading.current_thread().name
            try:
                HANDLERS[job["kind"]](job)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                storage.finish_job(job["id"], "failed", error=traceback.format_exc()[-2000:])
            finally:
                with self._lock:
                    self._running.pop(job["id"], None)

    def _housekeeping(self) -> None:
        last_beat = last_recover = 0.0
        last_maint = time.time()
        while not self._stop.is_set():
            try:
                with _live_lock:
                    dirty = [rid for rid, e in _live.items() if e.get("dirty")]
                for run_id in dirty:
                    _persist_progress(run_id)
                if time.time() - last_beat >= HEARTBEAT_SECONDS:
                    storage.heartbeat_jobs(self.running_jobs())
                    last_beat = time.time()
                if time.time() - last_recover >= 30:
                    recover_stale_jobs()
                    last_recover = time.time()
                if time.time() - last_maint >= 3600:
                    import maintenance
                    maintenance.run_all()
                    last_maint = time.time()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            self._stop.wait(1.0)


_pool: Optional[WorkerPool] = None


def start_embedded_worker() -> Optional[WorkerPool]:
    global _pool
    if _pool is None and os.environ.get("CONFLICTSCAN_EMBEDDED_WORKER", "1").lower() not in ("0", "false", "no"):
        _pool = WorkerPool().start()
    return _pool


def recover_stale_jobs() -> int:
    """Jobs whose worker stopped sending heartbeats: back to the queue, or failed when out of attempts."""
    n = 0
    for job in storage.stale_running_jobs(STALE_SECONDS):
        if job["attempts"] >= job["max_attempts"]:
            storage.finish_job(job["id"], "failed", error="The worker running this job stopped.")
            if job["kind"] == "scan":
                storage.update_run(job["target_id"], status="failed", finished_at=storage.now(),
                                   error="The scan worker stopped while this scan was running. Start it again.")
        elif storage.release_job(job["id"]):
            if job["kind"] == "scan":
                storage.update_run(job["target_id"], status="queued", bump=False)
        n += 1
    return n


def recover_after_restart() -> None:
    """At worker start: requeue dead jobs, close runs with no job behind them, clean scratch, recheck uploads."""
    recover_stale_jobs()
    for run in storage.runs_with_status(storage.ACTIVE_STATUSES):
        if storage.active_job_for(run["id"], "scan"):
            continue
        if run["status"] == "running":
            storage.update_run(run["id"], status="failed", finished_at=storage.now(),
                               error="The server restarted while this scan was running. Start it again.")
        else:
            storage.enqueue_job("scan", run["id"], max_attempts=MAX_ATTEMPTS)
    root = _scratch_root()
    live_jobs = {j["id"] for j in storage.list_jobs(("running",))}
    for name in os.listdir(root):
        parts = name.split("_")
        if len(parts) > 1 and parts[0] == "job" and parts[1] in live_jobs:
            continue
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)
    queued_inspections = {j["target_id"] for j in storage.list_jobs(("queued", "running"), kind="inspect")}
    for file_id in storage.uninspected_file_ids():
        if file_id not in queued_inspections:
            inspect_file_later(file_id)
    _wake.set()


def _scratch_root() -> str:
    """Temporary per-scan working folders (deleted when the scan ends)."""
    root = os.environ.get("CONFLICTSCAN_SCRATCH_DIR") or os.path.join(storage.DATA_DIR, "scratch")
    os.makedirs(root, exist_ok=True)
    return root


def new_upload_name(filename: str) -> str:
    return f"{uuid.uuid4().hex[:8]}_{filename}"
