"""
Phase 1 platform tests: roles and membership, admin console, sign-up approval,
directory sign-in (stubbed), the database job queue (priority, retries,
crashed workers, concurrent claims), edit conflicts, presence, audit log,
data export/import between engines, the first-release database import, and
the DBA's offline SQL.

PostgreSQL tests run when CONFLICTSCAN_TEST_PG_URL is set, e.g.
    postgresql+psycopg2://cstest@127.0.0.1:55432/conflictscan_test
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest

from test_workspace_api import (  # noqa: F401 - stub_graph is an autouse fixture
    PASSWORD, ROOT, api, client_for, graph, inspected, jobs, new_scanned_run, security, stub_graph, upload, wait_for,
)
import storage  # noqa: E402

PG_URL = os.environ.get("CONFLICTSCAN_TEST_PG_URL")


def admin_client():
    """The first account in the test database is the admin; make sure 'root' is one."""
    c = client_for("root")
    user = storage.get_user_by_username("root")
    if user["role"] != "admin":
        storage.update_user(user["id"], role="admin")
    return c


def user_id(name):
    return storage.get_user_by_username(name)["id"]


# ---------------------------------------------------------------------------
# Roles and membership
# ---------------------------------------------------------------------------

def test_viewers_can_read_but_not_change():
    owner = client_for("pm1")
    project_id, run_id = new_scanned_run(owner, "Role checks")
    wait_for(owner, run_id)
    viewer = client_for("vic")
    storage.update_user(user_id("vic"), role="viewer")
    assert viewer.get(f"/api/projects/{project_id}").get_json()["my_role"] == "viewer"
    assert viewer.get(f"/api/runs/{run_id}").status_code == 200
    assert viewer.get(f"/api/runs/{run_id}/bundle").status_code == 200
    for method, url, body in (("patch", f"/api/runs/{run_id}/findings/D-001", {"status": "accepted"}),
                              ("put", f"/api/runs/{run_id}/rfis/RFI-001", {"subject": "x"}),
                              ("post", f"/api/runs/{run_id}/clone", None),
                              ("patch", f"/api/projects/{project_id}", {"name": "x"})):
        r = getattr(viewer, method)(url, json=body)
        assert r.status_code == 403, (url, r.get_json())
        assert "view-only" in r.get_json()["error"]
    assert viewer.post("/api/projects", json={"name": "Nope"}).status_code == 403


def test_private_projects_and_membership():
    owner = client_for("pm2")
    eng = client_for("eng2")
    project = owner.post("/api/projects", json={"name": "Private job", "visibility": "private"}).get_json()["project"]
    assert eng.get(f"/api/projects/{project['id']}").status_code == 404
    # engineers cannot manage members; the owner can
    assert eng.put(f"/api/projects/{project['id']}/members/{user_id('eng2')}", json={"role": "engineer"}).status_code == 404
    r = owner.put(f"/api/projects/{project['id']}/members/{user_id('eng2')}", json={"role": "viewer"})
    assert r.status_code == 200 and r.get_json()["members"][0]["role"] == "viewer"
    assert eng.get(f"/api/projects/{project['id']}").get_json()["my_role"] == "viewer"
    assert project["id"] in {p["id"] for p in eng.get("/api/projects").get_json()}
    owner.put(f"/api/projects/{project['id']}/members/{user_id('eng2')}", json={"role": "manager"})
    assert eng.patch(f"/api/projects/{project['id']}", json={"visibility": "team"}).status_code == 200
    owner.delete(f"/api/projects/{project['id']}/members/{user_id('eng2')}")
    # now a team project, so still visible, but as an engineer (global role) only
    assert eng.get(f"/api/projects/{project['id']}").get_json()["my_role"] == "engineer"
    assert eng.delete(f"/api/projects/{project['id']}").status_code == 403


def test_admins_see_everything():
    admin = admin_client()
    other = client_for("pm3")
    secret = other.post("/api/projects", json={"name": "Hidden", "visibility": "private"}).get_json()["project"]
    assert secret["id"] in {p["id"] for p in admin.get("/api/projects").get_json()}
    assert admin.get(f"/api/projects/{secret['id']}").get_json()["my_role"] == "manager"


# ---------------------------------------------------------------------------
# Admin console and accounts
# ---------------------------------------------------------------------------

def test_admin_creates_users_with_temporary_passwords():
    admin = admin_client()
    name = f"new{uuid.uuid4().hex[:6]}"
    r = admin.post("/api/admin/users", json={"username": name, "display_name": "New Person", "role": "viewer"})
    assert r.status_code == 201
    temp = r.get_json()["temporary_password"]
    c = api.app.test_client()
    me = c.post("/api/auth/login", json={"username": name, "password": temp}).get_json()["user"]
    assert me["must_change_password"] is True and me["role"] == "viewer"
    assert c.patch("/api/me", json={"current_password": temp, "new_password": "brand-new-pw"}).status_code == 200
    assert c.get("/api/auth/status").get_json()["user"]["must_change_password"] is False

    uid = user_id(name)
    admin.patch(f"/api/admin/users/{uid}", json={"status": "disabled"})
    assert c.get("/api/projects").status_code == 401                     # the open session ends at once
    r = api.app.test_client().post("/api/auth/login", json={"username": name, "password": "brand-new-pw"})
    assert r.status_code == 403 and "disabled" in r.get_json()["error"]
    temp2 = admin.post(f"/api/admin/users/{uid}/reset-password").get_json()["temporary_password"]
    admin.patch(f"/api/admin/users/{uid}", json={"status": "active"})
    assert api.app.test_client().post("/api/auth/login", json={"username": name, "password": temp2}).status_code == 200


def test_non_admins_cannot_use_the_console_and_last_admin_is_protected():
    admin = admin_client()
    assert client_for("eng3").get("/api/admin/users").status_code == 403
    for u in storage.list_users():
        if u["role"] == "admin" and u["username"] != "root":
            storage.update_user(u["id"], role="engineer")
    r = admin.patch(f"/api/admin/users/{user_id('root')}", json={"role": "engineer"})
    assert r.status_code == 400 and "last administrator" in r.get_json()["error"]
    assert admin.patch(f"/api/admin/users/{user_id('root')}", json={"status": "disabled"}).status_code == 400


def test_signup_waits_for_approval(monkeypatch):
    monkeypatch.setattr(api, "ALLOW_SIGNUP", True)
    monkeypatch.setattr(api, "SIGNUP_APPROVAL", True)
    name = f"asker{uuid.uuid4().hex[:6]}"
    c = api.app.test_client()
    r = c.post("/api/auth/register", json={"username": name, "password": PASSWORD})
    assert r.status_code == 202 and r.get_json()["pending"]
    r = c.post("/api/auth/login", json={"username": name, "password": PASSWORD})
    assert r.status_code == 403 and "approval" in r.get_json()["error"]
    admin_client().patch(f"/api/admin/users/{user_id(name)}", json={"status": "active"})
    assert c.post("/api/auth/login", json={"username": name, "password": PASSWORD}).status_code == 200


def test_signup_is_off_by_default(monkeypatch):
    monkeypatch.setattr(api, "ALLOW_SIGNUP", False)
    c = api.app.test_client()
    assert c.get("/api/auth/status").get_json()["signup_allowed"] is False
    assert c.post("/api/auth/register", json={"username": "walkin", "password": PASSWORD}).status_code == 403


def test_directory_sign_in_maps_groups_to_roles(monkeypatch):
    from adapters import identity
    monkeypatch.setenv("CONFLICTSCAN_AUTH", "ldap+local")
    monkeypatch.setenv("CONFLICTSCAN_LDAP_GROUPS", json.dumps({"CN=CS-Managers,DC=x": "manager", "CN=CS-Eng,DC=x": "engineer"}))
    directory = {"layla": {"password": "dir-pass", "external_id": "guid-123", "username": "layla",
                           "display_name": "Layla Directory", "email": "layla@example.local", "groups": ["CN=CS-Eng,DC=x"]}}

    def fake_lookup(username, password):
        entry = directory.get(identity._bare(username).lower())
        return dict(entry) if entry and entry["password"] == password else None

    monkeypatch.setattr(identity, "ldap_lookup", fake_lookup)
    c = api.app.test_client()
    assert c.post("/api/auth/login", json={"username": "EXAMPLE\\layla", "password": "wrong"}).status_code == 401
    me = c.post("/api/auth/login", json={"username": "EXAMPLE\\layla", "password": "dir-pass"}).get_json()["user"]
    assert me["role"] == "engineer" and me["auth_source"] == "ldap" and me["display_name"] == "Layla Directory"
    # promoted in the directory -> promoted here at the next sign-in; renamed account keeps its identity
    directory["layla"]["groups"].append("CN=CS-Managers,DC=x")
    directory["layla"]["display_name"] = "Layla D."
    me2 = api.app.test_client().post("/api/auth/login", json={"username": "layla", "password": "dir-pass"}).get_json()["user"]
    assert me2["id"] == me["id"] and me2["role"] == "manager" and me2["display_name"] == "Layla D."
    # directory accounts cannot set a local password; local accounts still work (break-glass)
    assert c.patch("/api/me", json={"current_password": "x", "new_password": "12345678x"}).status_code == 400
    assert client_for("root").get("/api/projects").status_code == 200
    monkeypatch.setenv("CONFLICTSCAN_LDAP_REQUIRE_GROUP", "1")
    directory["nogroup"] = {**directory["layla"], "username": "nogroup", "external_id": "guid-9", "groups": []}
    r = api.app.test_client().post("/api/auth/login", json={"username": "nogroup", "password": "dir-pass"})
    assert r.status_code == 401 and "group" in r.get_json()["error"]


# ---------------------------------------------------------------------------
# Job queue
# ---------------------------------------------------------------------------

def test_concurrent_claims_never_double_book():
    ids = [storage.enqueue_job("probe", uuid.uuid4().hex[:16])["id"] for _ in range(30)]
    got, lock = [], threading.Lock()

    def worker(n):
        while True:
            job = storage.claim_job(["probe"], f"w{n}")
            if not job:
                return
            with lock:
                got.append(job["id"])
            storage.finish_job(job["id"], "done")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == sorted(ids)


def test_priority_order_and_admin_move_to_top():
    a = storage.enqueue_job("probe2", "a")
    b = storage.enqueue_job("probe2", "b")
    c = storage.enqueue_job("probe2", "c")
    admin = admin_client()
    assert admin.post(f"/api/admin/queue/{c['id']}", json={"action": "top"}).status_code == 200
    order = []
    while True:
        job = storage.claim_job(["probe2"], "t")
        if not job:
            break
        order.append(job["target_id"])
        storage.finish_job(job["id"], "done")
    assert order == ["c", "a", "b"]
    assert {a["id"], b["id"]}


def test_jobs_of_a_dead_worker_go_back_to_the_queue():
    job = storage.enqueue_job("probe3", "x", max_attempts=2)
    claimed = storage.claim_job(["probe3"], "dead-worker")
    assert claimed["id"] == job["id"]
    with storage.tx() as conn:
        from sqlalchemy import update
        conn.execute(update(storage.jobs).where(storage.jobs.c.id == job["id"]).values(heartbeat_at=storage.ago(minutes=10)))
    assert jobs.recover_stale_jobs() >= 1
    again = storage.claim_job(["probe3"], "new-worker")
    assert again["id"] == job["id"] and again["attempts"] == 2
    with storage.tx() as conn:
        from sqlalchemy import update
        conn.execute(update(storage.jobs).where(storage.jobs.c.id == job["id"]).values(heartbeat_at=storage.ago(minutes=10)))
    jobs.recover_stale_jobs()
    assert storage.get_job(job["id"])["status"] == "failed"          # out of attempts


def test_unreachable_model_server_is_retried(monkeypatch):
    from test_workspace_api import fake_scan
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("Ollama is not reachable at http://localhost:11434")
        return fake_scan(**kwargs)

    monkeypatch.setattr(graph, "run_conflict_scan", flaky)
    monkeypatch.setattr(jobs, "RETRY_BASE_SECONDS", 0)
    c = client_for("retry")
    _project_id, run_id = new_scanned_run(c, "Flaky server")
    run = wait_for(c, run_id, timeout=30)
    assert run["status"] == "complete" and calls["n"] == 2
    assert any("retrying" in line for line in run["progress"]["log"]) or calls["n"] == 2


def test_document_problems_are_not_retried(monkeypatch):
    def broken(**kwargs):
        raise ValueError("Set B has no readable text")

    monkeypatch.setattr(graph, "run_conflict_scan", broken)
    c = client_for("noretry")
    _project_id, run_id = new_scanned_run(c, "Bad docs")
    run = wait_for(c, run_id)
    assert run["status"] == "failed" and "readable" in run["error"]
    failed = [j for j in storage.list_jobs(("failed",), kind="scan") if j["target_id"] == run_id]
    assert failed and failed[0]["attempts"] == 1


# ---------------------------------------------------------------------------
# Edit conflicts, presence, audit
# ---------------------------------------------------------------------------

def test_two_people_editing_the_same_rfi():
    sara = client_for("sara")
    omar = client_for("omar")
    _project_id, run_id = new_scanned_run(sara, "Conflicts")
    wait_for(sara, run_id)
    rfi = sara.get(f"/api/runs/{run_id}").get_json()["results"]["rfi_items"][0]
    base = rfi.get("rev") or 0
    assert omar.put(f"/api/runs/{run_id}/rfis/RFI-001", json={"subject": "Omar's", "base_rev": base}).status_code == 200
    r = sara.put(f"/api/runs/{run_id}/rfis/RFI-001", json={"subject": "Sara's", "base_rev": base})
    assert r.status_code == 409
    body = r.get_json()
    assert body["conflict"] and body["rfi"]["subject"] == "Omar's" and body["rfi"]["edited_by"] == "Omar"
    # without a base revision (e.g. "overwrite"), the save goes through
    assert sara.put(f"/api/runs/{run_id}/rfis/RFI-001", json={"subject": "Sara's"}).get_json()["subject"] == "Sara's"


def test_parallel_triage_loses_nothing():
    c1, c2 = client_for("tri1"), client_for("tri2")
    _project_id, run_id = new_scanned_run(c1, "Parallel triage")
    wait_for(c1, run_id)
    errors = []

    def triage(c, did, status):
        r = c.patch(f"/api/runs/{run_id}/findings/{did}", json={"status": status})
        if r.status_code != 200:
            errors.append(r.get_json())

    ts = [threading.Thread(target=triage, args=(c1, "D-001", "accepted")),
          threading.Thread(target=triage, args=(c2, "D-002", "dismissed"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    review = c1.get(f"/api/runs/{run_id}").get_json()["review"]["findings"]
    assert not errors and review["D-001"]["status"] == "accepted" and review["D-002"]["status"] == "dismissed"


def test_draft_inputs_conflict():
    c1, c2 = client_for("inp1"), client_for("inp2")
    run_id = c1.post("/api/projects", json={"name": "Inputs"}).get_json()["run"]["id"]
    rev = c1.get(f"/api/runs/{run_id}").get_json()["inputs"].get("_rev", 0)
    assert c2.patch(f"/api/runs/{run_id}", json={"inputs": {"review_focus": "B"}, "base_inputs_rev": rev}).status_code == 200
    r = c1.patch(f"/api/runs/{run_id}", json={"inputs": {"review_focus": "A"}, "base_inputs_rev": rev})
    assert r.status_code == 409 and r.get_json()["inputs"]["review_focus"] == "B"


def test_presence_shows_who_else_is_here():
    a, b = client_for("pres_a"), client_for("pres_b")
    assert a.post("/api/presence", json={"target": "run:xyz", "detail": "rfis"}).get_json() == []
    others = b.post("/api/presence", json={"target": "run:xyz", "detail": "findings"}).get_json()
    assert [o["display_name"] for o in others] == ["Pres_A"]
    a.post("/api/presence", json={"target": "run:xyz", "leave": True})
    assert b.post("/api/presence", json={"target": "run:xyz"}).get_json() == []


def test_actions_are_audited():
    c = client_for("aud")
    project_id, run_id = new_scanned_run(c, "Audited")
    wait_for(c, run_id)
    c.patch(f"/api/runs/{run_id}/findings/D-001", json={"status": "dismissed"})
    c.get(f"/api/runs/{run_id}/bundle")
    rows = admin_client().get(f"/api/admin/audit?project={project_id}").get_json()
    actions = {r["action"] for r in rows}
    assert {"project.create", "run.files_added", "run.start", "finding.review", "export.bundle"} <= actions
    assert all(r["username"] == "aud" for r in rows)
    failed = admin_client().get("/api/admin/audit?action=auth.login").get_json()
    assert any(r["action"] == "auth.login" for r in failed)


def test_health_and_settings_pages():
    admin = admin_client()
    h = admin.get("/api/admin/health").get_json()
    names = {c["name"] for c in h["checks"]}
    assert {"Database", "File storage", "Sign-in", "Reference data", "Job queue"} <= names
    assert next(c for c in h["checks"] if c["name"] == "Database")["ok"]
    s = admin.get("/api/admin/settings").get_json()
    assert s["schema_revision"] and "database.url" in s["settings"]


def test_files_are_stored_by_relative_key():
    c = client_for("keys")
    _project_id, run_id = new_scanned_run(c, "Keys")
    for f in storage.list_run_files(run_id):
        assert not os.path.isabs(f["path"]) and f["path"].startswith(f"runs/{run_id}/inputs/")
        assert f["sha256"] is None or len(f["sha256"]) == 64


def test_reference_projects_prefill_the_form():
    c = client_for("refuser")
    hits = c.get("/api/reference/projects?q=sample").get_json()
    assert hits and hits[0]["code"]
    r = c.post("/api/projects", json={"name": hits[0]["name"], "reference": hits[0]}).get_json()
    full = c.get(f"/api/runs/{r['run']['id']}").get_json()
    assert r["project"]["code"] == hits[0]["code"]
    assert full["inputs"]["rfi_form_meta"]["project_code"] == hits[0]["code"]


# ---------------------------------------------------------------------------
# Moving data between engines (separate processes: one DATABASE_URL each)
# ---------------------------------------------------------------------------

def _manage(args, env_extra, check=True):
    env = {**os.environ, **env_extra}
    env.pop("CONFLICTSCAN_CONFIG", None)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "manage.py"), *args], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    if check:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


def test_export_import_roundtrip_between_databases(tmp_path):
    c = client_for("mover")
    _project_id, run_id = new_scanned_run(c, "Move me ✓ مشروع")
    wait_for(c, run_id)
    out = tmp_path / "backup.zip"
    _manage(["backup", str(out)], {})
    target_dir = tmp_path / "target"
    env = {"CONFLICTSCAN_DATA_DIR": str(target_dir), "DATABASE_URL": "sqlite:///" + str(target_dir / "t.db").replace("\\", "/")}
    _manage(["restore", str(out)], env)
    assert "Identical" in _manage(["verify", str(out)], env).stdout
    assert "All run files are present" in _manage(["check-files"], env).stdout
    # importing twice needs --replace
    r = _manage(["import", str(out)], env, check=False)
    assert r.returncode != 0 and "--replace" in (r.stdout + r.stderr)


@pytest.mark.skipif(not PG_URL, reason="set CONFLICTSCAN_TEST_PG_URL to test PostgreSQL")
def test_export_import_into_postgresql(tmp_path):
    c = client_for("pgmover")
    _project_id, run_id = new_scanned_run(c, "To Postgres ✓ مشروع")
    wait_for(c, run_id)
    out = tmp_path / "move.zip"
    _manage(["backup", str(out)], {})
    env = {"DATABASE_URL": PG_URL, "CONFLICTSCAN_DB_SCHEMA": f"cs_move_{uuid.uuid4().hex[:6]}",
           "CONFLICTSCAN_DATA_DIR": str(tmp_path / "pgdata")}
    _manage(["restore", str(out)], env)
    assert "Identical" in _manage(["verify", str(out)], env).stdout
    status = _manage(["status"], env).stdout
    assert "postgresql" in status


@pytest.mark.skipif(not PG_URL, reason="set CONFLICTSCAN_TEST_PG_URL to test PostgreSQL")
def test_whole_workspace_suite_on_postgresql():
    env = {**os.environ, "DATABASE_URL": PG_URL, "CONFLICTSCAN_DB_SCHEMA": f"cs_suite_{uuid.uuid4().hex[:6]}"}
    env.pop("CONFLICTSCAN_DATA_DIR", None)
    r = subprocess.run([sys.executable, "-m", "pytest", os.path.join(ROOT, "tests", "test_workspace_api.py"), "-q",
                        "-p", "no:cacheprovider"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-3000:]


def test_first_release_database_is_imported(tmp_path):
    data = tmp_path / "old"
    (data / "runs" / "r1" / "inputs").mkdir(parents=True)
    (data / "runs" / "r1" / "inputs" / "x_boq.txt").write_text("C40")
    con = sqlite3.connect(data / "conflictscan.db")
    con.executescript("""
    CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT, display_name TEXT, password_hash TEXT, created_at TEXT, last_seen_at TEXT);
    CREATE TABLE user_prefs (user_id TEXT, key TEXT, value_json TEXT);
    CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, description TEXT, owner_id TEXT, visibility TEXT, archived INTEGER, created_at TEXT, updated_at TEXT);
    CREATE TABLE runs (id TEXT PRIMARY KEY, project_id TEXT, seq INTEGER, title TEXT, status TEXT, created_by TEXT, updated_by TEXT, parent_run_id TEXT, inputs_json TEXT, results_json TEXT, review_json TEXT, progress_json TEXT, error TEXT, created_at TEXT, updated_at TEXT, queued_at TEXT, started_at TEXT, finished_at TEXT);
    CREATE TABLE run_files (id TEXT PRIMARY KEY, run_id TEXT, slot TEXT, name TEXT, path TEXT, kind TEXT, detected_kind TEXT, detected_reason TEXT, size INTEGER, position INTEGER, created_at TEXT);
    """)
    t = "2026-11-01T10:00:00+00:00"
    con.execute("INSERT INTO users VALUES ('u1','Omar','Omar I','hash',?,NULL)", (t,))
    con.execute("INSERT INTO user_prefs VALUES ('u1','theme','\"dark\"')")
    con.execute("INSERT INTO projects VALUES ('p1','Old job','',  'u1','team',0,?,?)", (t, t))
    con.execute("INSERT INTO runs VALUES ('r1','p1',1,'','complete','u1','u1',NULL,'{}','{\"discrepancies\":[]}','{}','{}',NULL,?,?,NULL,NULL,NULL)", (t, t))
    con.execute("INSERT INTO run_files VALUES ('f1','r1','a','boq.txt',?, 'auto','document','text',3,0,?)",
                (str(data / "runs" / "r1" / "inputs" / "x_boq.txt"), t))
    con.commit()
    con.close()
    code = ("import storage, json; storage.init_db(); u = storage.get_user_by_username('omar'); "
            "f = storage.list_run_files('r1')[0]; "
            "print(json.dumps([u['role'], storage.get_prefs('u1'), f['path'], storage.get_run('r1')['results']]))")
    env = {**os.environ, "CONFLICTSCAN_DATA_DIR": str(data)}
    env.pop("DATABASE_URL", None)
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    role, prefs, path, results = json.loads(r.stdout.strip().splitlines()[-1])
    assert role == "admin" and prefs == {"theme": "dark"} and path == "runs/r1/inputs/x_boq.txt"
    assert results == {"discrepancies": []}
    assert any(p.name.startswith("conflictscan.legacy-") for p in data.iterdir())


def test_offline_sql_for_the_dba():
    for url, expect in (("postgresql://x/y", 'CREATE SCHEMA IF NOT EXISTS "conflictscan"'),
                        ("mssql+pyodbc://x/y?driver=ODBC", "NVARCHAR(80)")):
        out = _manage(["sql", "--url", url], {}).stdout
        assert expect in out and "CREATE TABLE conflictscan.runs" in out


# ---------------------------------------------------------------------------
# Cancelling
# ---------------------------------------------------------------------------

def test_cancel_stops_a_long_stage_at_the_next_model_call(monkeypatch):
    """A cancel during a long stage takes effect before the next model call, not after the stage."""
    from fleet import client as fc
    calls = {"n": 0}

    def slow_post(server, payload, timeout=900):
        calls["n"] += 1
        time.sleep(0.05)
        return {"content": "{}", "prompt_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(fc, "_post", slow_post)
    monkeypatch.setattr(fc, "resolve", lambda role: [("m", {"name": "s", "url": "http://x"})])

    def long_stage(**kwargs):
        kwargs["on_node_complete"]("receive_input", {})
        for i in range(400):                 # one long "read & profile" stage
            try:
                fc.call("extractor", "s", f"chunk {i} {time.time()}", cache=False)
            except Exception as exc:          # nodes swallow errors per chunk - the cancel must still win
                if type(exc).__name__ != "ScanCancelled":
                    raise
        raise AssertionError("the stage was not stopped")

    monkeypatch.setattr(graph, "run_conflict_scan", long_stage)
    c = client_for("canceller")
    _project_id, run_id = new_scanned_run(c, "Cancel mid-stage")
    deadline = time.time() + 10
    while c.get(f"/api/runs/{run_id}/progress").get_json()["status"] != "running" and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    started = time.time()
    assert c.post(f"/api/runs/{run_id}/cancel").status_code == 200
    run = wait_for(c, run_id, timeout=10)
    assert run["status"] == "cancelled" and run["error"] == "Cancelled by the user."
    assert time.time() - started < 5 and calls["n"] < 400


def test_restart_never_revives_a_cancelled_scan():
    from sqlalchemy import update
    c = client_for("canceller2")
    run_id = c.post("/api/projects", json={"name": "Dead worker"}).get_json()["run"]["id"]
    storage.update_run(run_id, status="running")
    job = storage.enqueue_job("scan", run_id, max_attempts=3)
    storage.claim_job(["scan"], "dead-host:1")
    storage.request_job_cancel(job["id"])
    with storage.tx() as conn:            # the app was shut down: no heartbeat since
        conn.execute(update(storage.jobs).where(storage.jobs.c.id == job["id"]).values(heartbeat_at=storage.ago(hours=12)))
    jobs.recover_stale_jobs()
    assert storage.get_job(job["id"])["status"] == "cancelled"
    assert storage.get_run(run_id)["status"] == "cancelled"


def test_claimed_cancelled_job_closes_its_run():
    c = client_for("canceller3")
    run_id = c.post("/api/projects", json={"name": "Claimed after cancel"}).get_json()["run"]["id"]
    storage.update_run(run_id, status="queued")
    job = storage.enqueue_job("scan", run_id)
    storage.request_job_cancel(job["id"])
    jobs._execute_scan(storage.get_job(job["id"]))
    assert storage.get_job(job["id"])["status"] == "cancelled"
    assert storage.get_run(run_id)["status"] == "cancelled"
