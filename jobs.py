"""
Scan jobs - ConflictScan / RFI Automation
==========================================
Runs scans in background worker threads so the web UI stays responsive, many
engineers can queue work at once, and a scan survives the browser tab being
closed.

    submit(run_id)  -> queued; a worker picks it up when one is free
    cancel(run_id)  -> queued runs stop at once; a running scan stops after
                       the graph stage in progress (stages cannot be cut short)
    live(run_id)    -> stage list, latest log line and recent log for the UI

Worker count comes from CONFLICTSCAN_WORKERS (default 1: the local LLM is the
bottleneck, so parallel scans mostly slow each other down).

Everything a scan produces - register, RFI .docx files - is moved into
data/runs/<run>/exports/ when it finishes, so runs never overwrite each other.
"""

import contextvars
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import storage

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
_executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="scan")
_cancel_flags: Dict[str, threading.Event] = {}
_live: Dict[str, Dict[str, Any]] = {}
_live_lock = threading.Lock()


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


def _new_progress() -> Dict[str, Any]:
    return {
        "stages": [{"key": k, "label": label, "status": "pending"} for k, label in STAGES],
        "current": None,
        "message": "",
        "log": [],
    }


def live(run_id: str) -> Optional[Dict[str, Any]]:
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
        }


def _persist_progress(run_id: str) -> None:
    snap = live(run_id)
    if snap:
        snap["log"] = snap["log"][-LOG_LINES_KEPT:]
        storage.update_run(run_id, progress=snap)


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


# ---------------------------------------------------------------------------
# Queue API
# ---------------------------------------------------------------------------

def submit(run_id: str) -> None:
    _cancel_flags[run_id] = threading.Event()
    progress = _new_progress()
    progress["log"] = []
    storage.update_run(run_id, status="queued", queued_at=storage.now(), started_at=None,
                       finished_at=None, error=None, progress=progress)
    _executor.submit(_execute, run_id)


def cancel(run_id: str) -> bool:
    flag = _cancel_flags.get(run_id)
    run = storage.get_run(run_id)
    if not run or run["status"] not in storage.ACTIVE_STATUSES:
        return False
    if flag:
        flag.set()
    if run["status"] == "queued" or not flag:
        storage.update_run(run_id, status="cancelled", finished_at=storage.now(),
                           error="Cancelled before it started.")
    else:
        _log(run_id, "Cancel requested - stopping after the current stage...")
    return True


def queue_position(run_id: str) -> int:
    queued = [r["id"] for r in storage.runs_with_status(("queued",))]
    return queued.index(run_id) + 1 if run_id in queued else 0


def recover_after_restart() -> None:
    """Runs interrupted by a server restart: re-queue waiting ones, fail running ones."""
    for run in storage.runs_with_status(storage.ACTIVE_STATUSES):
        if run["status"] == "running":
            storage.update_run(run["id"], status="failed", finished_at=storage.now(),
                               error="The server restarted while this scan was running. Start it again.")
        else:
            submit(run["id"])


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
    """Move a file the graph wrote to exports/ into the run's own folder; returns the new basename."""
    if not path or not os.path.isfile(path):
        return None
    name = os.path.basename(path)
    dest = os.path.join(dest_dir, name)
    try:
        shutil.move(path, dest)
    except OSError:
        shutil.copy2(path, dest)
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

    sets: Dict[str, List[Dict[str, Any]]] = {"a": [], "b": []}
    for f in storage.list_run_files(run_id):
        if f["slot"] not in sets or not os.path.isfile(f["path"]):
            continue
        work_path = os.path.join(work_dir, os.path.basename(f["path"]))
        shutil.copy2(f["path"], work_path)
        kind, reason = f["kind"], "chosen by the user"
        if kind == "auto":
            # Full classification (may ask the vision model about ambiguous scans);
            # the upload-time guess shown in the UI skips that call to stay fast.
            kind, reason = classify_file(work_path)
        sets[f["slot"]].append({"name": f["name"], "path": work_path, "kind": kind, "classified_as": reason})

    for slot in ("a", "b"):
        pasted = (inputs.get(f"document_{slot}_text") or "").strip()
        if pasted:
            sets[slot].append({"name": "pasted text", "kind": "text", "text": pasted, "classified_as": "pasted"})
        sets[slot] = pair_drawing_files(sets[slot])
    return sets["a"], sets["b"]


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _execute(run_id: str) -> None:
    flag = _cancel_flags.setdefault(run_id, threading.Event())
    run = storage.get_run(run_id)
    if not run or run["status"] != "queued" or flag.is_set():
        _cancel_flags.pop(run_id, None)
        return

    project = storage.get_project(run["project_id"]) or {}
    creator = storage.get_user(run["created_by"]) if run.get("created_by") else None
    inputs = run.get("inputs") or {}

    with _live_lock:
        _live[run_id] = {**_new_progress(), "log": deque(maxlen=LOG_LINES_KEPT), "started_at": storage.now()}
    storage.update_run(run_id, status="running", started_at=storage.now())
    install_log_capture()      # again here, in case something replaced sys.stdout since startup
    token = _current_run.set(run_id)
    work_dir = tempfile.mkdtemp(prefix=f"scan_{run_id}_", dir=_scratch_root())
    started = time.time()

    try:
        from graph import run_conflict_scan, ScanCancelled

        files_a, files_b = resolve_source_files(run_id, work_dir, inputs)
        if not files_a or not files_b:
            missing = " and ".join(s for s, f in (("Set A", files_a), ("Set B", files_b)) if not f)
            raise ValueError(f"{missing} has no files or text. Add documents and start the scan again.")

        _set_stage(run_id, "receive_input", "running")
        _log(run_id, f"Starting scan: {len(files_a)} file(s) in Set A, {len(files_b)} in Set B")

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
            should_stop=flag.is_set,
        )

        if result_state.get("error_message"):
            raise ValueError(result_state["error_message"])

        results = build_results(result_state, files_a, files_b, storage.run_dir(run_id, "exports"))
        results["duration_seconds"] = round(time.time() - started)
        _log(run_id, f"Done: {len(results['discrepancies'])} finding(s), {len(results['rfi_items'])} RFI(s)")
        _persist_progress(run_id)
        storage.update_run(run_id, status="complete", results=results, finished_at=storage.now(), error=None)

    except Exception as exc:  # noqa: BLE001 - every failure must land on the run record
        cancelled = type(exc).__name__ == "ScanCancelled"
        if not cancelled:
            traceback.print_exc()
            _log(run_id, f"ERROR: {exc}")
        with _live_lock:
            entry = _live.get(run_id)
            if entry:
                for stage in entry["stages"]:
                    if stage["status"] == "running":
                        stage["status"] = "cancelled" if cancelled else "failed"
        _persist_progress(run_id)
        storage.update_run(
            run_id,
            status="cancelled" if cancelled else "failed",
            error="Cancelled by the user." if cancelled else str(exc),
            finished_at=storage.now(),
        )
    finally:
        _current_run.reset(token)
        shutil.rmtree(work_dir, ignore_errors=True)
        _cancel_flags.pop(run_id, None)
        with _live_lock:
            _live.pop(run_id, None)


def _scratch_root() -> str:
    root = os.path.join(os.getcwd(), "uploads")
    os.makedirs(root, exist_ok=True)
    return root


def new_upload_name(filename: str) -> str:
    return f"{uuid.uuid4().hex[:8]}_{filename}"
