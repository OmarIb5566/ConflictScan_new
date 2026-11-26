"""
Learning from reviewers: Accept / Dismiss / severity decisions are stored with
the model's finding, and later scans adjust findings that match them.

    python -m pytest tests/test_review_feedback.py -q
"""

from sqlalchemy import delete, select

from test_workspace_api import api, client_for, new_scanned_run, stub_graph, wait_for  # noqa: F401 - test database + stubbed scan
import db
import storage
from fleet import client, profile
from nodes import reviewer_feedback as rf


def rows(run_id=None):
    with storage.tx() as conn:
        q = select(db.review_feedback)
        if run_id:
            q = q.where(db.review_feedback.c.run_id == run_id)
        return [dict(r._mapping) for r in conn.execute(q)]


def clear():
    with storage.tx() as conn:
        conn.execute(delete(db.review_feedback))


def test_reviewer_decisions_are_recorded_against_the_models_finding():
    c = client_for("fb_reviewer")
    _, run_id = new_scanned_run(c, "Feedback tower")
    assert wait_for(c, run_id)["status"] == "complete"

    c.patch(f"/api/runs/{run_id}/findings/D-002", json={"status": "dismissed", "note": "Same cover, typo in BOQ"})
    (row,) = rows(run_id)
    assert row["decision"] == "dismissed" and row["model_severity"] == "minor" and row["final_severity"] is None
    assert row["note"] == "Same cover, typo in BOQ" and row["finding"]["topic"] == "Cover to rebar"
    assert "Cover to rebar" in row["text"] and row["decided_by"] == "Fb_Reviewer"

    c.patch(f"/api/runs/{run_id}/findings/D-001", json={"severity": "minor"})       # re-rated, still open
    d1 = next(r for r in rows(run_id) if r["finding_id"] == "D-001")
    assert d1["decision"] == "rerated" and d1["model_severity"] == "critical" and d1["final_severity"] == "minor"

    c.patch(f"/api/runs/{run_id}/findings/D-002", json={"status": "open"})            # changed their mind
    assert {r["finding_id"] for r in rows(run_id)} == {"D-001"}

    r = c.patch(f"/api/runs/{run_id}/findings", json={"ids": ["D-001", "D-002"], "status": "accepted"})
    assert r.status_code == 200
    got = {r["finding_id"]: (r["decision"], r["final_severity"]) for r in rows(run_id)}
    assert got == {"D-001": ("accepted", "minor"), "D-002": ("accepted", "minor")}


# ---------------------------------------------------------------------------
# Applying the decisions to a new scan
# ---------------------------------------------------------------------------

def fake_vectors(texts, **kw):
    """Meaning stand-in: findings about gravel, plaster or rebar each point their own way."""
    out = []
    for t in texts:
        t = t.lower()
        out.append([1.0 if "gravel" in t else 0.0, 1.0 if "plaster" in t else 0.0, 1.0 if "rebar" in t else 0.0, 0.01])
    return out


def finding(i, topic, severity="major", cls="scope_gap", **kw):
    return {"id": f"D-{i:03d}", "topic": topic, "description": topic, "issue_class": cls, "severity": severity,
            "evidence": [{"side": "a", "says": topic}], **kw}


def decide(project, findings_and_entries):
    run = {"id": storage.new_id(), "project_id": project,
           "results": {"discrepancies": [d for d, _ in findings_and_entries]}}
    for d, entry in findings_and_entries:
        rf.record(run, d["id"], entry, "Tester")


def scan(found, monkeypatch):
    monkeypatch.setattr(client, "embed", fake_vectors)
    with profile.use("workstation"):
        return rf.apply_feedback(found)


def test_findings_like_repeatedly_dismissed_ones_are_kept_out_of_rfis(monkeypatch):
    clear()
    decide("p1", [(finding(1, "Gravel over bitumen on tank 1 cover slab"), {"status": "dismissed", "note": "BOQ governs"}),
                  (finding(2, "Gravel protection to tank 2 roof"), {"status": "dismissed"})])
    found = [finding(1, "Gravel protection on suction tank cover slab", "major"),
             finding(2, "Rebar cover 40 vs 50", "major")]
    stats = scan(found, monkeypatch)
    assert stats["dismissed"] == 1
    assert found[0]["previously_dismissed"] and found[0]["severity"] == "minor" and found[0]["severity_found"] == "major"
    assert found[0]["learned"]["dismissed_before"] == 2 and found[0]["learned"]["reason"] == "BOQ governs"
    assert "previously_dismissed" not in found[1] and found[1]["severity"] == "major"      # unrelated: untouched


def test_one_dismissal_or_a_different_issue_type_is_not_enough(monkeypatch):
    clear()
    decide("p2", [(finding(1, "Gravel over bitumen"), {"status": "dismissed"})])
    found = [finding(1, "Gravel protection on cover slab")]
    assert scan(found, monkeypatch)["dismissed"] == 0 and found[0]["severity"] == "major"

    decide("p3", [(finding(1, "Gravel finish", cls="spatial_clash"), {"status": "dismissed"})])
    found = [finding(1, "Gravel protection on cover slab")]                 # 1 same-type + 1 other-type dismissal
    assert scan(found, monkeypatch)["dismissed"] == 0


def test_severity_follows_what_reviewers_chose_for_similar_findings(monkeypatch):
    clear()
    decide("p4", [(finding(1, "Plaster thickness 25 vs 20", "critical"), {"severity": "minor"}),
                  (finding(2, "Plaster mix class", "critical"), {"status": "accepted", "severity": "minor"}),
                  (finding(3, "Plaster to tank walls", "major"), {"status": "accepted"})])   # votes major
    found = [finding(1, "Plaster to external tank faces", "critical")]
    assert scan(found, monkeypatch)["rerated"] == 1
    assert found[0]["severity"] == "minor" and found[0]["severity_found"] == "critical"
    assert found[0]["learned"] == {"rated_before": "minor", "votes": 2, "of": 3}


def test_learning_never_overrides_the_safety_rules_or_split_opinions(monkeypatch):
    clear()
    decide("p5", [(finding(1, "Rebar lap length", "minor"), {"status": "accepted", "severity": "critical"}),
                  (finding(2, "Rebar spacing", "minor"), {"status": "accepted", "severity": "critical"})])
    unsure = [finding(1, "Rebar lap at slab", "minor", needs_confirmation=True)]
    assert scan(unsure, monkeypatch)["rerated"] == 0 and unsure[0]["severity"] == "minor"
    low = [finding(1, "Rebar lap at slab", "minor", low_confidence=True)]
    scan(low, monkeypatch)
    assert low[0]["severity"] == "major"                                     # raised, but never to critical on doubtful evidence

    clear()
    decide("p6", [(finding(1, "Plaster A", "major"), {"status": "accepted", "severity": "minor"}),
                  (finding(2, "Plaster B", "major"), {"status": "accepted", "severity": "critical"})])
    split = [finding(1, "Plaster C", "major")]
    assert scan(split, monkeypatch)["rerated"] == 0 and split[0]["severity"] == "major"


def test_decisions_apply_on_top_of_the_checkpointed_findings(monkeypatch):
    clear()
    decide("p7", [(finding(1, "Gravel A"), {"status": "dismissed"}), (finding(2, "Gravel B"), {"status": "dismissed"})])
    checkpointed = {"discrepancies": [finding(1, "Gravel C")], "analysis_warnings": []}
    monkeypatch.setattr(client, "embed", fake_vectors)
    with profile.use("workstation"):
        delta = rf.with_feedback(lambda state: checkpointed)({})
    assert delta["discrepancies"][0]["previously_dismissed"]
    assert "previously_dismissed" not in checkpointed["discrepancies"][0]   # the saved copy stays as the model found it
    assert any("dismissed on earlier scans" in w for w in delta["analysis_warnings"])

    with profile.use("baseline"):                                           # switched off: scan as before
        assert rf.apply_feedback([finding(1, "Gravel C")])["dismissed"] == 0


def test_admin_sees_and_can_reset_what_was_learned():
    clear()
    decide("p8", [(finding(1, "Gravel A", "major"), {"status": "dismissed"}),
                  (finding(2, "Plaster", "critical"), {"status": "accepted", "severity": "major"})])
    s = rf.summary()
    assert s["decisions"] == {"dismissed": 1, "accepted": 1}
    assert s["severity_changes"] == [{"from": "critical", "to": "major", "count": 1}]
