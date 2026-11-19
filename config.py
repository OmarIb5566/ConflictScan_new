"""
Deployment settings - one file for the whole installation
==========================================================
Every setting can come from three places, highest priority first:

    1. an environment variable (CONFLICTSCAN_*, DATABASE_URL, OLLAMA_URL)
    2. the settings file: conflictscan.toml next to the code, or the file
       named by CONFLICTSCAN_CONFIG
    3. the built-in default in the module that uses it

load() copies the settings file into environment variables that are not
already set, so every module keeps reading plain os.environ. It runs once,
on import, from api.py / worker.py / manage.py before anything else.

The file layout (see conflictscan.example.toml):

    [server]      host, port, https, behind_proxy, threads
    [database]    url, schema
    [storage]     data_dir, files_backend, files_root, s3_*
    [auth]        mode, allow_signup, signup_approval, idle_minutes, ...
    [auth.ldap]   url, domain, base_dn, user_filter, groups -> roles
    [reference]   backend, file, url, projects_query, employees_query
    [scans]       workers, max_file_mb, max_pdf_pages, ...
    [retention]   scratch_hours, finished_jobs_days, audit_days, trash_days
    [[model_servers]]  name, url, kind, gpus, roles  (one block per server)
    [fleet]       mode, roles.* (AI fleet: which model does what)

Windows compatible.
"""

import json
import os
import tomllib
from typing import Any, Dict

ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION = "6.0.0"

# settings-file key -> environment variable
_ENV_MAP = {
    ("server", "host"): "CONFLICTSCAN_HOST",
    ("server", "port"): "CONFLICTSCAN_PORT",
    ("server", "threads"): "CONFLICTSCAN_THREADS",
    ("server", "https"): "CONFLICTSCAN_HTTPS",
    ("server", "behind_proxy"): "CONFLICTSCAN_BEHIND_PROXY",
    ("server", "allowed_origins"): "CONFLICTSCAN_ALLOWED_ORIGINS",
    ("server", "secret_key"): "CONFLICTSCAN_SECRET_KEY",
    ("server", "embedded_worker"): "CONFLICTSCAN_EMBEDDED_WORKER",
    ("database", "url"): "DATABASE_URL",
    ("database", "schema"): "CONFLICTSCAN_DB_SCHEMA",
    ("storage", "data_dir"): "CONFLICTSCAN_DATA_DIR",
    ("storage", "files_backend"): "CONFLICTSCAN_FILES_BACKEND",
    ("storage", "files_root"): "CONFLICTSCAN_FILES_ROOT",
    ("storage", "s3_endpoint"): "CONFLICTSCAN_S3_ENDPOINT",
    ("storage", "s3_bucket"): "CONFLICTSCAN_S3_BUCKET",
    ("storage", "s3_prefix"): "CONFLICTSCAN_S3_PREFIX",
    ("storage", "s3_access_key"): "CONFLICTSCAN_S3_ACCESS_KEY",
    ("storage", "s3_secret_key"): "CONFLICTSCAN_S3_SECRET_KEY",
    ("auth", "mode"): "CONFLICTSCAN_AUTH",
    ("auth", "allow_signup"): "CONFLICTSCAN_ALLOW_SIGNUP",
    ("auth", "signup_approval"): "CONFLICTSCAN_SIGNUP_APPROVAL",
    ("auth", "idle_minutes"): "CONFLICTSCAN_IDLE_MINUTES",
    ("auth", "session_days"): "CONFLICTSCAN_SESSION_DAYS",
    ("auth", "login_max_fails"): "CONFLICTSCAN_LOGIN_MAX_FAILS",
    ("auth", "min_password"): "CONFLICTSCAN_MIN_PASSWORD",
    ("auth.ldap", "url"): "CONFLICTSCAN_LDAP_URL",
    ("auth.ldap", "domain"): "CONFLICTSCAN_LDAP_DOMAIN",
    ("auth.ldap", "base_dn"): "CONFLICTSCAN_LDAP_BASE_DN",
    ("auth.ldap", "user_filter"): "CONFLICTSCAN_LDAP_USER_FILTER",
    ("auth.ldap", "bind_user"): "CONFLICTSCAN_LDAP_BIND_USER",
    ("auth.ldap", "bind_password"): "CONFLICTSCAN_LDAP_BIND_PASSWORD",
    ("auth.ldap", "default_role"): "CONFLICTSCAN_LDAP_DEFAULT_ROLE",
    ("auth.ldap", "require_group"): "CONFLICTSCAN_LDAP_REQUIRE_GROUP",
    ("auth.ldap", "groups"): "CONFLICTSCAN_LDAP_GROUPS",
    ("reference", "backend"): "CONFLICTSCAN_REFERENCE",
    ("reference", "file"): "CONFLICTSCAN_REFERENCE_FILE",
    ("reference", "url"): "CONFLICTSCAN_REFERENCE_URL",
    ("reference", "projects_query"): "CONFLICTSCAN_REFERENCE_PROJECTS_SQL",
    ("reference", "employees_query"): "CONFLICTSCAN_REFERENCE_EMPLOYEES_SQL",
    ("scans", "workers"): "CONFLICTSCAN_WORKERS",
    ("scans", "max_attempts"): "CONFLICTSCAN_MAX_ATTEMPTS",
    ("scans", "max_upload_mb"): "CONFLICTSCAN_MAX_UPLOAD_MB",
    ("scans", "max_file_mb"): "CONFLICTSCAN_MAX_FILE_MB",
    ("scans", "max_pdf_pages"): "CONFLICTSCAN_MAX_PDF_PAGES",
    ("scans", "max_image_mp"): "CONFLICTSCAN_MAX_IMAGE_MP",
    ("scans", "max_files_per_set"): "CONFLICTSCAN_MAX_FILES_PER_SET",
    ("scans", "debug_logs"): "CONFLICTSCAN_DEBUG_LOGS",
    ("scans", "daily_page_limit"): "CONFLICTSCAN_DAILY_PAGE_LIMIT",
    ("retention", "scratch_hours"): "CONFLICTSCAN_RETAIN_SCRATCH_HOURS",
    ("retention", "finished_jobs_days"): "CONFLICTSCAN_RETAIN_JOBS_DAYS",
    ("retention", "audit_days"): "CONFLICTSCAN_RETAIN_AUDIT_DAYS",
    ("retention", "trash_days"): "CONFLICTSCAN_RETAIN_TRASH_DAYS",
    ("retention", "debug_days"): "CONFLICTSCAN_RETAIN_DEBUG_DAYS",
    ("ollama", "url"): "OLLAMA_URL",
    ("fleet", "profile"): "CONFLICTSCAN_FLEET_PROFILE",
    ("fleet", "mode"): "CONFLICTSCAN_FLEET_MODE",
    ("fleet", "file"): "CONFLICTSCAN_FLEET_FILE",
}

_loaded: Dict[str, Any] = {}


def settings_path() -> str:
    return os.environ.get("CONFLICTSCAN_CONFIG") or os.path.join(ROOT, "conflictscan.toml")


def _as_env(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, dict)):
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return ",".join(value)
        return json.dumps(value)
    return str(value)


def load(path: str = None) -> Dict[str, Any]:
    """Read the settings file (if any) and fill in unset environment variables."""
    global _loaded
    path = path or settings_path()
    if not os.path.isfile(path):
        _loaded = {}
        return _loaded
    with open(path, "rb") as f:
        data = tomllib.load(f)
    _loaded = data
    for (section, key), env in _ENV_MAP.items():
        node: Any = data
        for part in section.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict) and key in node and env not in os.environ:
            os.environ[env] = _as_env(node[key])
    if data.get("model_servers") and "CONFLICTSCAN_MODEL_SERVERS" not in os.environ:
        os.environ["CONFLICTSCAN_MODEL_SERVERS"] = json.dumps(data["model_servers"])
    if data.get("fleet", {}).get("roles") and "CONFLICTSCAN_FLEET_ROLES" not in os.environ:
        os.environ["CONFLICTSCAN_FLEET_ROLES"] = json.dumps(data["fleet"]["roles"])
    return _loaded


def effective() -> Dict[str, str]:
    """Every known setting with its current value (secrets masked) - for the admin console."""
    out = {}
    for (section, key), env in sorted(_ENV_MAP.items(), key=lambda kv: kv[1]):
        value = os.environ.get(env)
        if value and any(word in env for word in ("SECRET", "PASSWORD", "ACCESS_KEY")):
            value = "********"
        if env == "DATABASE_URL" and value and "@" in value:
            scheme, rest = value.split("://", 1)
            value = f"{scheme}://********@{rest.split('@', 1)[1]}"
        out[f"{section}.{key}"] = value if value is not None else ""
    return out


load()
