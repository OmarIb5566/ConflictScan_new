"""
AI fleet tests: profiles, model resolution and fallback, the response cache,
the per-call log, parallel fan-out, second opinions, votes, meaning-based
matching, the RFI memory and the drawing second reader. Model servers are
faked - no Ollama needed.
"""

import contextvars
import json
import threading
import time

import pytest

from test_workspace_api import api  # noqa: F401 - sets up the test database
import fleet
import storage
from fleet import client, memory, profile, verify


@pytest.fixture
def fake_server(monkeypatch):
    """One model server with a chosen set of installed models; records every request."""
    state = {"installed": ["qwen2.5vl:7b", "qwen3:14b", "qwen3.6:27b", "gpt-oss:latest", "bge-m3:latest", "gemma4:e4b"],
             "requests": [], "reply": lambda payload: json.dumps({"ok": True}), "fail": set()}
    monkeypatch.setenv("CONFLICTSCAN_MODEL_SERVERS", json.dumps([{"name": "gpu0", "url": "http://127.0.0.1:1"}]))
    monkeypatch.setattr(client, "installed_models", lambda server, max_age=60: state["installed"])
    client._broken.clear()

    def post(server, payload, timeout=900):
        state["requests"].append(payload)
        if payload["model"] in state["fail"]:
            raise ConnectionError("boom")
        return {"content": state["reply"](payload), "prompt_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(client, "_post", post)
    return state


def calls_for(run_id):
    from db import model_calls
    from sqlalchemy import select
    with storage.tx() as conn:
        return [dict(r._mapping) for r in conn.execute(select(model_calls).where(model_calls.c.run_id == run_id))]


def test_profiles_and_roles(monkeypatch):
    assert {"baseline", "workstation", "a100"} <= set(profile.profile_names())
    with profile.use("workstation"):
        assert profile.role("adjudicator")["model"] == "qwen3.6:27b"
        assert profile.role("ocr")["vision"] is True
        assert profile.feature("verify_findings") and not profile.feature("second_reader")
    with profile.use("a100"):
        assert profile.active()["mode"] == "resident" and profile.feature("vote_critical")
    with profile.use("baseline"):
        assert profile.role("verifier") is None
        monkeypatch.setenv("CONFLICTSCAN_TEXT_MODEL", "llama3.2:latest")   # old single-model setting still works
        assert profile.role("extractor")["model"] == "llama3.2:latest"
    monkeypatch.setenv("CONFLICTSCAN_FLEET_ROLES", json.dumps({"drafter": {"model": "qwen3:14b"}}))
    with profile.use("workstation"):
        assert profile.role("drafter")["model"] == "qwen3:14b"


def test_fallback_when_model_missing_and_cloud_refused(fake_server, monkeypatch):
    monkeypatch.setenv("CONFLICTSCAN_FLEET_ROLES", json.dumps({"drafter": {"model": "kimi-k3:cloud",
                                                                          "fallback": ["qwen3:14b"]}}))
    with profile.use("workstation"):
        assert client.resolved_model("drafter") == "qwen3:14b"           # cloud model never used
        fake_server["installed"].remove("qwen3.6:27b")
        assert client.resolved_model("adjudicator") == "qwen3:14b"       # first fallback that is installed
        fake_server["fail"].add("qwen3:14b")
        out = client.call("adjudicator", "sys", f"user {time.time()}", max_retries=0)
        assert json.loads(out)["ok"] and fake_server["requests"][-1]["model"] == "gemma4:e4b"


def test_nothing_answers_is_a_retryable_failure(fake_server):
    import jobs
    fake_server["fail"].update(fake_server["installed"])
    with profile.use("workstation"), pytest.raises(client.ModelUnavailable) as err:
        client.call("verifier", "sys", f"u{time.time()}", max_retries=0)
    assert jobs._is_transient(err.value)


def test_cache_and_call_log(fake_server):
    run_id = storage.new_id()
    token = client.current_run.set(run_id)
    try:
        with profile.use("workstation"):
            prompt = f"identical prompt {time.time()}"
            first = client.call("extractor", "sys", prompt)
            second = client.call("extractor", "sys", prompt)
            client.call("extractor", "sys", prompt, temperature=0.9)      # creative calls are never cached
    finally:
        client.current_run.reset(token)
    assert first == second
    sent = [p for p in fake_server["requests"] if p["messages"][1]["content"] == prompt]
    assert len(sent) == 2
    assert sent[0]["think"] is False and sent[0]["keep_alive"] == "3m"   # qwen3 thinking off; staged keep-alive
    rows = calls_for(run_id)
    assert [r["cached"] for r in rows].count(True) == 1 and all(r["role"] == "extractor" for r in rows)
    assert all(r["server"] == "gpu0" for r in rows)


def test_images_are_part_of_the_cache_key(fake_server, tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(b"\x89PNG one")
    b.write_bytes(b"\x89PNG two")
    with profile.use("workstation"):
        client.call("drawing_reader", "s", "read", images=[str(a)])
        client.call("drawing_reader", "s", "read", images=[str(b)])
        client.call("drawing_reader", "s", "read", images=[str(a)])
    reads = [p for p in fake_server["requests"] if p["messages"][1]["content"] == "read"]
    assert len(reads) == 2 and reads[0]["messages"][1]["images"]


def test_resident_mode_keeps_models_loaded(fake_server):
    with profile.use("a100"):
        client.call("drafter", "s", f"x{time.time()}", temperature=0.9)
    assert fake_server["requests"][-1]["keep_alive"] == -1


def test_pmap_keeps_order_context_and_limits(fake_server):
    var = contextvars.ContextVar("v", default=None)
    var.set("scan-42")
    active, peak, lock = [0], [0], threading.Lock()

    def work(i):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return i * i, var.get()

    with profile.use("a100"):
        out = fleet.pmap(work, range(12), role="extractor")
    assert [o[0] for o in out] == [i * i for i in range(12)]
    assert all(o[1] == "scan-42" for o in out)
    assert 1 < peak[0] <= 6


def _finding(i, sev="major"):
    return {"id": f"D-{i:03d}", "topic": f"t{i}", "description": "A says C40, B says C35", "severity": sev,
            "evidence": [{"side": "a", "file": "a.pdf", "says": "C40"}, {"side": "b", "file": "b.pdf", "says": "C35"}]}


def test_second_opinion_marks_disputed_findings(monkeypatch):
    from fleet.client import RoleLLM

    def answer(self, system, user, **kw):
        n = user.count("--- FINDING")
        return {"verdicts": [{"index": i, "verdict": "not_a_conflict" if i == 1 else "confirmed", "reason": "r"}
                             for i in range(n)]}

    monkeypatch.setattr(RoleLLM, "run", answer)
    monkeypatch.setattr(client, "resolved_model", lambda role: "gpt-oss:latest")
    found = [_finding(i) for i in range(3)]
    with profile.use("workstation"):
        stats = verify.verify_findings(found)
    assert stats["checked"] == 3 and stats["disputed"] == 1
    assert found[1]["disputed"] and found[1]["verification"]["verdict"] == "not_a_conflict"
    assert not found[0].get("disputed") and found[0]["verification"]["model"] == "gpt-oss:latest"
    with profile.use("baseline"):
        assert verify.verify_findings([_finding(9)])["checked"] == 0      # off in the baseline profile


def test_critical_findings_get_a_vote(monkeypatch):
    from fleet.client import RoleLLM

    def answer(self, system, user, **kw):
        verdict = "not_a_conflict" if self.role in ("verifier", "extractor") else "confirmed"
        return {"verdicts": [{"index": 0, "verdict": verdict, "reason": "r"}]}

    monkeypatch.setattr(RoleLLM, "run", answer)
    monkeypatch.setattr(client, "resolved_model", lambda role: "m")
    found = [_finding(1, "critical")]
    with profile.use("a100"):
        verify.verify_findings(found)
    assert found[0]["vote"] == {"for": 1, "against": 2, "unsure": 0} and found[0]["disputed"]


def test_meaning_based_matching(monkeypatch):
    from nodes import conflict_scan as cs
    vectors = {"sprinkler": [1.0, 0.0, 0.0], "pre-action": [0.98, 0.05, 0.0], "lighting": [0.0, 1.0, 0.0]}

    def fake_embed(texts, **kw):
        return [next((v for k, v in vectors.items() if k in t), [0.0, 0.0, 1.0]) for t in texts]

    monkeypatch.setattr(fleet, "embed", fake_embed)
    a = [{"topic": "Fire protection", "statement": "sprinkler system dry pipe", "location": "Basement"}]
    b = [{"topic": "Fire suppression", "statement": "pre-action system", "location": "Basement"},
         {"topic": "Lighting", "statement": "lighting LED", "location": "Basement"}]
    with profile.use("workstation"):
        pairs, rest_a, rest_b = cs.semantic_pairs(a, b)
    assert len(pairs) == 1 and pairs[0][1]["topic"] == "Fire suppression"
    assert rest_a == [] and [r["topic"] for r in rest_b] == ["Lighting"]
    with profile.use("baseline"):
        assert cs.semantic_pairs(a, b)[0] == []


def test_rfi_memory_search(monkeypatch):
    def fake_embed(texts, **kw):
        return [[1.0, 0.0] if "slab" in t.lower() else [0.0, 1.0] for t in texts]

    monkeypatch.setattr(client, "embed", fake_embed)
    monkeypatch.setattr(client, "resolved_model", lambda role: "bge-m3:latest")
    memory.add([{"project_name": "Past Tower", "rfi_no": "PT-RFI-0007", "subject": "Slab thickness at L2",
                 "text": "Kindly confirm the slab thickness at level 2."},
                {"project_name": "Past Tower", "rfi_no": "PT-RFI-0008", "subject": "Door hardware",
                 "text": "Kindly advise the ironmongery schedule."}], source="training")
    memory._index["at"] = 0
    with profile.use("workstation"):
        hits = memory.search("slab level differs between sets", k=1)
        assert hits and hits[0]["rfi_no"] == "PT-RFI-0007"
        assert "Kindly confirm the slab thickness" in memory.style_examples("slab")
    with profile.use("baseline"):
        assert memory.search("slab") == []
    assert memory.count()["training"] >= 2


def test_drawing_second_reader(monkeypatch):
    from nodes import drawing_analysis as da
    monkeypatch.setattr(da, "_run_json", lambda client_, **kw: {"requirements": [{"value": "C40"}, {"value": "450 mm"}]})
    reqs = [{"statement": "Column concrete", "value": "C40"},
            {"statement": "Beam depth", "value": "600 mm"},
            {"statement": "Slab", "value": "200 mm", "value_unverified": True, "low_confidence": True,
             "statement_x": ""}]
    reqs[2]["statement"] = "Slab (value not found in the sheet's text - verify)"
    with profile.use("a100"):
        flagged = da._second_reading(reqs, "prompt", {"path": __file__}, strings=[])
    assert flagged == 1
    assert reqs[0]["second_reading"] == "agrees" and not reqs[0].get("low_confidence")
    assert reqs[1]["second_reading"] == "differs" and reqs[1]["low_confidence"]
    with profile.use("workstation"):
        assert da._second_reading([{"statement": "x", "value": "1"}], "p", {"path": __file__}, []) == 0


def test_full_graph_with_the_fleet(monkeypatch, tmp_path):
    """The real graph on the workstation profile: per-role routing, verification, a fleet report."""
    from test_rfi_learning import _fake_llm
    from fleet.client import RoleLLM
    roles_used = []

    def run(self, system, user, temperature=0.2, **kw):
        roles_used.append(self.role)
        if self.role == "verifier":
            return {"verdicts": [{"index": 0, "verdict": "not_a_conflict", "reason": "different transformers"}]}
        return _fake_llm(system, user, temperature)

    monkeypatch.setattr(RoleLLM, "run", run)
    monkeypatch.setattr(fleet, "embed", lambda texts, **kw: [None] * len(texts))
    monkeypatch.setattr(client, "embed", lambda texts, **kw: [None] * len(texts))
    monkeypatch.setattr(client, "resolved_model", lambda role: f"model-for-{role}")
    boq = tmp_path / "boq.txt"
    boq.write_text("Transformer TR4 rating | 1250 kVA | BOQ item 1.2 | TR4\n", encoding="utf-8")
    sld = tmp_path / "sld.txt"
    sld.write_text("Transformer TR4 rating | 1000 kVA | SLD-100 | TR4\n", encoding="utf-8")
    from graph import run_conflict_scan
    import os
    with profile.use("workstation"):
        state = run_conflict_scan(
            user_id="t", document_a_text="", document_b_text="", document_a_label="BOQ", document_b_label="SLD",
            document_a_files=[{"name": "boq.txt", "path": str(boq), "kind": "document"}],
            document_b_files=[{"name": "sld.txt", "path": str(sld), "kind": "document"}],
            project_name="Fleet", rfi_template_path=os.path.join(api.__file__, "..", "templates_rfi", "none.docx"),
            output_dir=str(tmp_path / "out"))
    assert {"extractor", "adjudicator", "verifier", "drafter"} <= set(roles_used)
    disc = state["discrepancies"][0]
    assert disc["disputed"] and disc["verification"]["reason"] == "different transformers"
    assert any("independent check" in w for w in state.get("analysis_warnings", []))


def test_admin_switches_profile_and_overrides_a_role(fake_server):
    from test_platform import admin_client
    from test_workspace_api import client_for
    admin = admin_client()
    assert client_for("fleetuser").get("/api/admin/fleet").status_code == 403
    try:
        f = admin.get("/api/admin/fleet").get_json()
        assert f["profile"] == "baseline" and "workstation" in f["profiles"]
        r = admin.put("/api/admin/fleet", json={"profile": "workstation", "roles": {"drafter": "qwen3:14b"}})
        assert r.status_code == 200
        storage._settings_cache.clear()
        f = r.get_json()
        assert f["profile"] == "workstation"
        drafter = next(x for x in f["roles"] if x["role"] == "drafter")
        assert drafter["used"] == "qwen3:14b" and drafter["overridden"]
        assert admin.put("/api/admin/fleet", json={"roles": {"drafter": "kimi-k3:cloud"}}).status_code == 400
        t = admin.post("/api/admin/fleet/test", json={"role": "extractor"}).get_json()
        assert t["ok"] and t["model"] == "qwen3:14b"
        rows = admin.get("/api/admin/audit?action=admin.fleet").get_json()
        assert rows and "workstation" in rows[0]["summary"]
    finally:
        admin.put("/api/admin/fleet", json={"profile": None, "roles": {"drafter": None}})
        storage._settings_cache.clear()
    assert profile.active_name() == "baseline"
