"""
Per-scan output folders
=======================
Where the pipeline writes its files for the scan currently running. Each scan
gets its own folders, so two scans running at once (or two users) never share
or overwrite each other's registers, RFI documents or debug logs.

graph.run_conflict_scan() sets the folders for the duration of one scan:

    with scan_dirs(output_dir=..., debug_dir=...):
        ...

Nodes ask for them with output_dir() / debug_dir(). Outside a scan (scripts,
tests calling a node directly) output_dir() falls back to a fresh temporary
folder, and debug_dir() is None unless CONFLICTSCAN_DEBUG_DIR is set, in which
case no debug logs are written.

Windows compatible.
"""

import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_output_dir: ContextVar[Optional[str]] = ContextVar("conflictscan_output_dir", default=None)
_debug_dir: ContextVar[Optional[str]] = ContextVar("conflictscan_debug_dir", default=None)


@contextmanager
def scan_dirs(output_dir: Optional[str] = None, debug_dir: Optional[str] = None):
    """Route this scan's files into its own folders (created on demand)."""
    out_token = _output_dir.set(output_dir)
    dbg_token = _debug_dir.set(debug_dir)
    try:
        yield
    finally:
        _output_dir.reset(out_token)
        _debug_dir.reset(dbg_token)


def output_dir() -> str:
    """Folder for this scan's register and RFI documents (exists on return)."""
    path = _output_dir.get() or os.environ.get("CONFLICTSCAN_OUTPUT_DIR")
    if not path:
        path = tempfile.mkdtemp(prefix="conflictscan_out_")
        _output_dir.set(path)
    os.makedirs(path, exist_ok=True)
    return path


def debug_dir() -> Optional[str]:
    """Folder for this scan's diagnostic logs, or None when debug logs are off."""
    path = _debug_dir.get() or os.environ.get("CONFLICTSCAN_DEBUG_DIR")
    if path:
        os.makedirs(path, exist_ok=True)
    return path or None


def template_dir() -> str:
    """The shared RFI form folder (read-only input; per-project forms come later)."""
    return os.environ.get("CONFLICTSCAN_TEMPLATE_DIR") or os.path.join(PROJECT_ROOT, "templates_rfi")
