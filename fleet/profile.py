"""
Fleet profiles - which local model does which job (config/fleet.toml)
=====================================================================
    active()           the active profile: mode, features, roles
    role(name)         one role's settings, or None when the profile has no such role
    feature(name)      verify_findings / semantic_matching / rfi_memory / second_reader / vote_critical
    use(name)          switch profile for this thread/context (the benchmark compares profiles)

Old single-model settings (CONFLICTSCAN_TEXT_MODEL, CONFLICTSCAN_VISION_MODEL,
CONFLICTSCAN_OCR_MODEL) still win over the "baseline" profile, so existing
installations keep behaving as before when they choose it.

Windows compatible.
"""

import contextvars
import json
import os
import tomllib
from contextlib import contextmanager
from typing import Any, Dict, Optional

import config

FEATURES = ("verify_findings", "semantic_matching", "rfi_memory", "second_reader", "vote_critical")
VISION_ROLES = ("page_triage", "ocr", "drawing_overview", "drawing_reader", "drawing_second")
_override: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("fleet_profile", default=None)
_cache: Dict[str, Any] = {}


def fleet_file() -> str:
    return os.environ.get("CONFLICTSCAN_FLEET_FILE") or os.path.join(config.ROOT, "config", "fleet.toml")


def _load() -> Dict[str, Any]:
    path = fleet_file()
    mtime = os.path.getmtime(path) if os.path.isfile(path) else 0
    if _cache.get("mtime") != mtime:
        data = {}
        if mtime:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        _cache.update(mtime=mtime, data=data)
    return _cache["data"]


def profile_names():
    return sorted((_load().get("profiles") or {}).keys())


def _console() -> Dict[str, Any]:
    """Choices an administrator made in the admin console (stored in the database)."""
    try:
        import storage
        return storage.get_setting("fleet", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def active_name() -> str:
    # A benchmark's use() > the admin console > the settings file / environment > fleet.toml default
    return (_override.get() or (_console().get("profile") if _override.get() is None else None)
            or os.environ.get("CONFLICTSCAN_FLEET_PROFILE") or _load().get("default_profile") or "workstation")


def active() -> Dict[str, Any]:
    data = _load()
    name = active_name()
    prof = dict((data.get("profiles") or {}).get(name) or {})
    roles = {k: dict(v) for k, v in (prof.pop("roles", None) or {}).items()}
    # Roles set in conflictscan.toml [fleet.roles] (or CONFLICTSCAN_FLEET_ROLES) override the file.
    try:
        for k, v in json.loads(os.environ.get("CONFLICTSCAN_FLEET_ROLES") or "{}").items():
            roles[k] = {**roles.get(k, {}), **(v if isinstance(v, dict) else {"model": v})}
    except ValueError:
        pass
    if _override.get() is None:
        for k, v in (_console().get("roles") or {}).items():
            if v:
                roles[k] = {**roles.get(k, {}), "model": v}
    if name == "baseline":
        legacy = {"extractor": "CONFLICTSCAN_TEXT_MODEL", "adjudicator": "CONFLICTSCAN_TEXT_MODEL",
                  "drafter": "CONFLICTSCAN_TEXT_MODEL", "drawing_reader": "CONFLICTSCAN_VISION_MODEL",
                  "drawing_overview": "CONFLICTSCAN_VISION_MODEL", "ocr": "CONFLICTSCAN_OCR_MODEL",
                  "page_triage": "CONFLICTSCAN_PAGE_KIND_MODEL"}
        for role, env in legacy.items():
            if os.environ.get(env) and role in roles:
                roles[role] = {**roles[role], "model": os.environ[env]}
    mode = os.environ.get("CONFLICTSCAN_FLEET_MODE") or prof.get("mode") or "staged"
    out = {"name": name, "mode": mode, "parallel": int(prof.get("parallel", 1)), "roles": roles}
    for f in FEATURES:
        out[f] = bool(prof.get(f, False))
    return out


def role(name: str) -> Optional[Dict[str, Any]]:
    prof = active()
    cfg = prof["roles"].get(name)
    if not cfg:
        return None
    cfg = dict(cfg)
    cfg.setdefault("parallel", prof["parallel"])
    cfg.setdefault("fallback", [])
    cfg["vision"] = name in VISION_ROLES
    return cfg


def feature(name: str) -> bool:
    return bool(active().get(name))


@contextmanager
def use(name: Optional[str]):
    token = _override.set(name)
    try:
        yield
    finally:
        _override.reset(token)
