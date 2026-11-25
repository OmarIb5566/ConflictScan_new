"""
Stage checkpoints
=================
The two long stages (reading both sets, comparing them) save their result in
the scan's own output folder. When the same scan is started again - after a
server restart, a crash, or a retry of the job - a stage whose inputs have not
changed is loaded from its checkpoint instead of being redone, so the scan
resumes where it stopped.

A checkpoint is only used for the same scan folder AND the same inputs (file
names, sizes, modification times, labels, focus) AND the same checkpoint
version; anything else re-runs the stage.

Windows compatible.
"""

import hashlib
import json
import os
from typing import Any, Callable, Dict

CHECKPOINT_VERSION = "2026-11-25.2"     # bump when a stage's output shape or logic changes


def _digest(path: str) -> str:
    """Content hash - a retried job works on fresh copies of the files, so paths and times change."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return "missing:" + os.path.basename(path)
    return h.hexdigest()


def _signature(state: Dict[str, Any], name: str) -> str:
    parts = [CHECKPOINT_VERSION, name, state.get("document_a_label"), state.get("document_b_label"),
             state.get("review_focus"), state.get("document_a_text"), state.get("document_b_text")]
    for slot in ("a", "b"):
        for f in state.get(f"document_{slot}_files") or []:
            parts += [slot, f.get("name"), _digest(f.get("path") or "")]
    return hashlib.sha256(json.dumps(parts, default=str).encode("utf-8")).hexdigest()


def checkpointed(name: str, fn: Callable[[Dict[str, Any]], Dict[str, Any]]):
    """Wrap a graph node so its result is saved, and reused when the same scan resumes."""
    def node(state: Dict[str, Any]) -> Dict[str, Any]:
        from nodes.run_paths import checkpoint_dir
        folder = checkpoint_dir()             # the run's own folder - stable across retries
        if not folder:
            return fn(state)
        path = os.path.join(folder, f".checkpoint_{name}.json")
        sig = _signature(state, name)
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            if saved.get("signature") == sig:
                print(f"[checkpoint] '{name}' already done for these inputs - resuming after it")
                return saved["delta"]
        except (OSError, ValueError, KeyError):
            pass
        delta = fn(state)
        try:
            os.makedirs(folder, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"signature": sig, "delta": delta}, fh, default=str)
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError) as exc:
            print(f"[checkpoint] could not save '{name}': {exc}")
        return delta
    node.__name__ = getattr(fn, "__name__", name)
    return node
