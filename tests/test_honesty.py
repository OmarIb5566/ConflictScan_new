"""
Honesty tests: the scan reports what is really there.

- an empty / unreadable set stops the scan before any comparison (no hunting for "gaps")
- sets that agree are reported as consistent; small differences as minor
- with little in common, statements in one set only are not turned into gaps
- findings an independent check doubted are not counted and not drafted into RFIs
- every comparison prompt tells the model there is no target number of findings
"""

import os

import pytest

from test_workspace_api import api, client_for, wait_for  # noqa: F401 - test database
from test_rfi_learning import ROOT, _fake_llm
from nodes import assessment
from nodes import conflict_scan as cs

NO_FORM = os.path.join(ROOT, "templates_rfi", "none.docx")


@pytest.fixture
def models(monkeypatch):
    """Every model role answers like _fake_llm; records which prompts were sent."""
    from fleet.client import RoleLLM
    import fleet
    seen = []

    def run(self, system, user, temperature=0.2, **kw):
        seen.append((self.role, system, user))
        if "no counterpart" in system:           # coverage review: would report a gap for anything it is shown
            return {"discrepancies": [{"item_index": 0, "topic": "Missing item", "issue_class": "scope_gap_boq",
                                       "type": "scope_gap", "severity": "major", "description": "Not in the other set."}]}
        return _fake_llm(system, user, temperature)

    monkeypatch.setattr(RoleLLM, "run", run)
    monkeypatch.setattr(fleet, "embed", lambda texts, **kw: [None] * len(texts))
    return seen


def scan(tmp_path, a_text, b_text, **kw):
    from graph import run_conflict_scan
    a = tmp_path / "set_a.txt"
    a.write_text(a_text, encoding="utf-8")
    b = tmp_path / "set_b.txt"
    b.write_text(b_text, encoding="utf-8")
    return run_conflict_scan(
        user_id="t", document_a_text="", document_b_text="", document_a_label="Tender BOQ", document_b_label="IFC",
        document_a_files=[{"name": "set_a.txt", "path": str(a), "kind": "document"}],
        document_b_files=[{"name": "set_b.txt", "path": str(b), "kind": "document"}],
        project_name="Honest", rfi_template_path=NO_FORM, output_dir=str(tmp_path / "out"), **kw)


ROWS = "\n".join(f"Item {i} rating | {100 + i} kVA | BOQ item {i} | T{i}" for i in range(8))


def test_an_empty_set_stops_the_scan(models, tmp_path):
    state = scan(tmp_path, ROWS, "   \n\n  ")
    a = state["assessment"]
    assert a["verdict"] == "insufficient_input" and "IFC" in " ".join(a["problems"])
    assert state["discrepancies"] == [] and not state.get("rfi_items")
    assert "stopped before comparing" in state["scan_summary"]
    assert not [r for r, _s, _u in models if r == "adjudicator"]          # nothing was compared


def test_documents_that_agree_are_reported_as_consistent(models, tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "compare_quantities", lambda *a, **k: [])
    # the pair reviewer finds nothing when the values are equal
    from fleet.client import RoleLLM
    orig = RoleLLM.run

    def agree(self, system, user, temperature=0.2, **kw):
        if "pairs of statements" in system or "set for internal" in system:
            return {"discrepancies": []}
        return orig(self, system, user, temperature, **kw)

    monkeypatch.setattr(RoleLLM, "run", agree)
    state = scan(tmp_path, ROWS, ROWS)
    a = state["assessment"]
    assert a["verdict"] == "consistent" and a["headline"].startswith("No conflicts found")
    assert a["compared"]["pairs"] >= 5 and state["discrepancies"] == []


def test_real_differences_are_reported_as_conflicts(models, tmp_path):
    state = scan(tmp_path, ROWS, ROWS.replace("100 kVA", "160 kVA"))
    a = state["assessment"]
    assert a["verdict"] in ("conflicts", "minor_only") and a["counts"]["real"] >= 1
    assert a["headline"] in state["scan_summary"]


def test_unrelated_sets_do_not_produce_invented_gaps(models, tmp_path):
    other = "\n".join(f"Door {i} hardware | lever set {i} | ARCH-{i} | D{i}" for i in range(8))
    state = scan(tmp_path, ROWS, other)
    a = state["assessment"]
    assert a["verdict"] == "no_overlap", a
    assert not [d for d in state["discrepancies"] if d.get("issue_class") == "scope_gap_boq"]
    assert any("share little subject matter" in l for l in a["limitations"])
    coverage_calls = [u for r, s, u in models if "no counterpart" in s]
    assert all("OPEN ITEM" in u for u in coverage_calls)                   # only self-declared open items reviewed


def test_assessment_counts_only_real_findings():
    disc = [{"severity": "major"}, {"severity": "minor"}, {"severity": "critical", "disputed": True},
            {"severity": "major", "low_confidence": True}]
    a = assessment.assess_scan(disc, {"requirements": [{}] * 20}, {"requirements": [{}] * 20},
                               {"pairs": 15}, {}, [])
    assert a["verdict"] == "conflicts" and a["counts"] == {
        "real": 3, "significant": 1, "critical": 0, "major": 1, "minor": 1, "uncertain": 1, "doubted": 1, "total_raised": 4}
    assert "judged not to be real conflicts" in a["headline"] and "not counted" in a["headline"]
    minor = assessment.assess_scan([{"severity": "minor"}], {"requirements": [{}] * 40}, {"requirements": [{}] * 40},
                                   {"pairs": 30}, {}, [])
    assert minor["verdict"] == "minor_only" and "Nothing found should hold up work" in minor["headline"]
    only_doubted = assessment.assess_scan([{"severity": "critical", "disputed": True}], {"requirements": [{}] * 9},
                                          {"requirements": [{}] * 9}, {"pairs": 8}, {}, [])
    assert only_doubted["verdict"] == "consistent"


def test_thin_sets_are_compared_but_flagged():
    a = assessment.assess_inputs({"label": "A", "requirements": [{}] * 2, "files": [{"name": "a.pdf", "kind": "document", "chars": 90, "requirements": 2}]},
                                 {"label": "B", "requirements": [{}] * 30, "files": [{"name": "b.pdf", "kind": "document", "chars": 9000, "requirements": 30}]})
    assert a["ok"] and any("only 2 comparable" in l for l in a["limitations"])
    scanned = assessment.assess_inputs({"label": "A", "requirements": [{}] * 9, "files": []},
                                       {"label": "B", "requirements": [], "files": [{"name": "scan.pdf", "kind": "document", "chars": 0}]})
    assert not scanned["ok"] and "scan.pdf" in scanned["problems"][0]


def test_every_comparison_prompt_carries_the_no_quota_rule(models, tmp_path):
    scan(tmp_path, ROWS, ROWS.replace("100 kVA", "160 kVA"))
    comparison = [s for r, s, _u in models if r == "adjudicator"]
    assert comparison and all("NO expected or target number of findings" in s for s in comparison)


def test_disputed_findings_are_not_drafted(models):
    from nodes.rfi_drafting import rfi_drafting_agent
    out = rfi_drafting_agent({"discrepancies": [
        {"id": "D-001", "topic": "x", "severity": "major", "disputed": True, "issue_class": "doc_value_conflict"}],
        "rfi_template_path": NO_FORM})
    assert out["rfi_items"] == []


def test_a_scan_of_an_empty_set_says_so_in_the_app(models, monkeypatch):
    """Through the worker: the run completes with the verdict, and the comparison stages show as skipped."""
    import graph
    import io
    from test_workspace_api import upload
    from test_workspace_api import fake_scan  # noqa: F401
    monkeypatch.setattr(graph, "run_conflict_scan", graph.run_conflict_scan.__wrapped__
                        if hasattr(graph.run_conflict_scan, "__wrapped__") else graph.run_conflict_scan)
    c = client_for("honest")
    run_id = c.post("/api/projects", json={"name": "Empty B"}).get_json()["run"]["id"]
    upload(c, run_id, "a", "boq.txt", ROWS.encode())
    upload(c, run_id, "b", "blank.txt", b"   \n  ")
    c.post(f"/api/runs/{run_id}/start")
    run = wait_for(c, run_id, timeout=30)
    assert run["status"] == "complete", run.get("error")
    full = c.get(f"/api/runs/{run_id}").get_json()
    assert full["results"]["assessment"]["verdict"] == "insufficient_input"
    assert full["verdict"]["label"] == "Not enough to compare"
    stages = {s["key"]: s["status"] for s in run["progress"]["stages"]}
    assert stages["conflict_scan"] == "skipped" and stages["draft_rfi"] == "skipped" and stages["export"] == "done"
