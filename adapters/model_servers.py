"""
Model servers adapter - where the local models run
==================================================
A list of inference servers on the company's own GPUs. Nothing leaves the
network: only local Ollama or OpenAI-compatible (vLLM) endpoints are
accepted, and "cloud" models are refused.

From the settings file:

    [[model_servers]]
    name  = "gpu0"
    url   = "http://127.0.0.1:11434"
    kind  = "ollama"            # or "openai" for vLLM / llama.cpp server
    gpus  = [0]
    roles = ["ocr", "drawing", "page_triage"]    # optional: pin roles here

    [[model_servers]]
    name  = "gpu1"
    url   = "http://127.0.0.1:11435"
    ...

On the workstation, with nothing configured, there is one server: OLLAMA_URL
(default http://127.0.0.1:11434 - 'localhost' costs ~2 s per request on Windows). On the A100 host, run one Ollama instance
per GPU (CUDA_VISIBLE_DEVICES=N, OLLAMA_HOST=127.0.0.1:1143N) and list each.

Windows compatible.
"""

import json
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

import requests

import config  # noqa: F401

_health_cache: Dict[str, Any] = {}


def servers() -> List[Dict[str, Any]]:
    raw = os.environ.get("CONFLICTSCAN_MODEL_SERVERS")
    if raw:
        try:
            out = json.loads(raw)
        except ValueError:
            out = []
    else:
        from nodes.llm_client import ollama_base
        out = [{"name": "local", "url": ollama_base(), "kind": "ollama",
                "gpus": [], "roles": []}]
    for s in out:
        s.setdefault("kind", "ollama")
        s.setdefault("gpus", [])
        s.setdefault("roles", [])
        s["url"] = s["url"].rstrip("/").replace("://localhost", "://127.0.0.1")
    return out


def server_for_role(role: str) -> Optional[Dict[str, Any]]:
    """The server a role is pinned to, else None (the caller picks by model availability)."""
    for s in servers():
        if role in (s.get("roles") or []):
            return s
    return None


def list_models(server: Dict[str, Any], timeout: float = 3.0) -> List[str]:
    if server["kind"] == "openai":
        r = requests.get(server["url"] + "/v1/models", timeout=timeout)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]
    r = requests.get(server["url"] + "/api/tags", timeout=timeout)
    r.raise_for_status()
    return [m["name"] for m in r.json().get("models", []) if not m["name"].endswith(":cloud")]


def loaded_models(server: Dict[str, Any], timeout: float = 3.0) -> List[Dict[str, Any]]:
    if server["kind"] != "ollama":
        return []
    r = requests.get(server["url"] + "/api/ps", timeout=timeout)
    r.raise_for_status()
    return [{"name": m["name"], "vram_gb": round((m.get("size_vram") or 0) / 1e9, 1)} for m in r.json().get("models", [])]


def health(max_age: float = 15.0) -> List[Dict[str, Any]]:
    """Each server: reachable?, installed models, models loaded in GPU memory (cached a few seconds)."""
    out = []
    for s in servers():
        cached = _health_cache.get(s["url"])
        if cached and time.time() - cached["at"] < max_age:
            out.append(cached["value"])
            continue
        entry = {"name": s["name"], "url": s["url"], "kind": s["kind"], "gpus": s["gpus"], "roles": s["roles"]}
        try:
            entry["models"] = list_models(s)
            entry["loaded"] = loaded_models(s)
            entry["ok"] = True
        except Exception as exc:  # noqa: BLE001
            entry.update(ok=False, error=str(exc)[:200], models=[], loaded=[])
        _health_cache[s["url"]] = {"at": time.time(), "value": entry}
        out.append(entry)
    return out


def gpus() -> List[Dict[str, Any]]:
    """GPUs on this machine (nvidia-smi); empty when there is none or the tool is missing."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 6:
            rows.append({"index": int(parts[0]), "name": parts[1], "memory_used_mb": int(float(parts[2])),
                         "memory_total_mb": int(float(parts[3])), "utilization": int(float(parts[4])),
                         "temperature": int(float(parts[5]))})
    return rows
