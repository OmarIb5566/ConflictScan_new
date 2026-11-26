"""
Learning from reviewers' decisions
==================================
Every Accept / Dismiss / severity change on a finding is stored with the
finding exactly as the model produced it (table review_feedback): the model's
type, severity, wording and quotes, next to what the reviewer decided. Each
new scan compares its findings with those decisions, by meaning (embedder
role, bge-m3), and adjusts:

* repeatedly dismissed - when reviewers dismissed at least MIN_VOTES findings
  of the same type that say the same thing, and kept far fewer, the new
  finding is marked "previously_dismissed": severity minor, left out of the
  drafted RFIs. It stays in the register so a reviewer can still open it.
* re-rated - when reviewers kept at least MIN_VOTES such findings and most of
  them settled on another severity, the new finding takes that severity. The
  scan's own safety rules still apply: a point to confirm stays minor, a
  low-confidence finding is never raised to critical.

Nothing is deleted and no model is retrained: the decisions steer the
calibration, which costs one embedding batch per scan (seconds).

Windows compatible.
"""

import hashlib
import math
import os
import threading
from collections import Counter
from typing import Any, Dict, List, Optional

SIMILARITY = float(os.environ.get("CONFLICTSCAN_LEARN_SIMILARITY", "0.85"))   # cosine, bge-m3
MIN_VOTES = int(os.environ.get("CONFLICTSCAN_LEARN_MIN_VOTES", "2"))
SEVERITIES = ("critical", "major", "minor")

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# What a finding "says" - the text compared between scans
# ---------------------------------------------------------------------------

def _quotes(d: Dict[str, Any]) -> List[str]:
    out = [str(e.get("says") or e.get("quote") or "") for e in d.get("evidence") or []]
    out += [str(d.get(k) or "") for k in ("document_a_says", "document_b_says")]
    seen, keep = set(), []
    for q in out:
        q = " ".join(q.split())[:300]
        if q and q not in seen:
            seen.add(q)
            keep.append(q)
    return keep[:4]


def finding_text(d: Dict[str, Any]) -> str:
    """Type, topic, description and quotes - no IDs, file names or run details."""
    parts = [f"{(d.get('issue_class') or d.get('type') or '').replace('_', ' ')}: {d.get('topic') or ''}",
             " ".join(str(d.get("description") or "").split())[:600]]
    parts += [f"- {q}" for q in _quotes(d)]
    return "\n".join(p for p in parts if p.strip())[:2000]


def _signature(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot(d: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("id", "topic", "description", "issue_class", "type", "category", "severity", "element", "location",
            "impact", "suggested_question", "low_confidence", "needs_confirmation", "disputed", "verification")
    out = {k: d.get(k) for k in keys if d.get(k) not in (None, "", [])}
    out["evidence"] = [{k: e.get(k) for k in ("side", "document", "file", "reference", "says", "value", "sheet") if e.get(k)}
                       for e in (d.get("evidence") or [])[:6]]
    return out


# ---------------------------------------------------------------------------
# Recording a decision (called by the review endpoints)
# ---------------------------------------------------------------------------

def _decision(d: Dict[str, Any], entry: Dict[str, Any]):
    """(decision, final severity) for a review entry, or (None, None) when it is back to untouched."""
    status = entry.get("status") or "open"
    override = entry.get("severity") if entry.get("severity") in SEVERITIES else None
    model_sev = d.get("severity_found") or d.get("severity")
    if status == "dismissed":
        return "dismissed", None
    if status == "accepted":
        return "accepted", override or model_sev
    if override and override != model_sev:
        return "rerated", override
    return None, None


def record(run: Dict[str, Any], finding_id: str, entry: Dict[str, Any], by: Optional[str] = None) -> Optional[str]:
    """Store (or withdraw) the reviewer's decision on one finding of a run. Returns the decision stored."""
    import storage
    from sqlalchemy import and_, delete, insert, select, update
    from db import review_feedback as t
    d = next((x for x in (run.get("results") or {}).get("discrepancies") or [] if x.get("id") == finding_id), None)
    if d is None:
        return None
    text = finding_text(d)
    sig = _signature(text)
    decision, final = _decision(d, entry)
    key = and_(t.c.project_id == run["project_id"], t.c.signature == sig)
    now = storage.now()
    with _lock, storage.tx() as conn:
        if decision is None:       # set back to open: withdraw what this run said about it
            conn.execute(delete(t).where(and_(key, t.c.run_id == run["id"])))
            return None
        values = {"run_id": run["id"], "finding_id": finding_id, "decision": decision,
                  "model_severity": d.get("severity_found") or d.get("severity"), "final_severity": final,
                  "issue_class": d.get("issue_class") or d.get("type"), "finding": _snapshot(d), "text": text,
                  "note": (entry.get("note") or None), "decided_by": by or entry.get("by"), "updated_at": now}
        if conn.execute(update(t).where(key).values(**values)).rowcount == 0:
            conn.execute(insert(t).values(id=storage.new_id(), project_id=run["project_id"], signature=sig,
                                          created_at=now, **values))
    return decision


def record_many(run: Dict[str, Any], review: Dict[str, Any], ids: List[str], by: Optional[str] = None) -> None:
    findings = (review or {}).get("findings", {})
    for i in ids:
        try:
            record(run, i, findings.get(i) or {}, by)
        except Exception as exc:  # noqa: BLE001 - never fail a review because of the learning store
            print(f"[feedback] could not record {i}: {exc}")


# ---------------------------------------------------------------------------
# Using the decisions in a new scan
# ---------------------------------------------------------------------------

def _load_rows() -> List[Dict[str, Any]]:
    """All decisions, with an embedding for each (missing ones are embedded now and saved)."""
    import storage
    from sqlalchemy import select, update
    from db import review_feedback as t
    from fleet import client
    with storage.tx() as conn:
        rows = [dict(r._mapping) for r in conn.execute(select(
            t.c.id, t.c.project_id, t.c.decision, t.c.model_severity, t.c.final_severity, t.c.issue_class,
            t.c.text, t.c.note, t.c.embedding, t.c.embed_model, t.c.updated_at))]
    model = client.resolved_model("embedder")
    todo = [r for r in rows if not r["embedding"] or r["embed_model"] != model]
    if todo:
        vecs = client.embed([r["text"] for r in todo])
        with storage.tx() as conn:
            for r, v in zip(todo, vecs):
                if v:
                    r["embedding"], r["embed_model"] = v, model
                    conn.execute(update(t).where(t.c.id == r["id"]).values(embedding=v, embed_model=model))
    return [r for r in rows if r["embedding"]]


def _same_kind(d: Dict[str, Any], row: Dict[str, Any]) -> bool:
    kind = d.get("issue_class") or d.get("type")
    return not kind or not row["issue_class"] or kind == row["issue_class"]


def _adjust(d: Dict[str, Any], near: List[Dict[str, Any]]) -> Optional[str]:
    dismissed = [r for r in near if r["decision"] == "dismissed"]
    kept = [r for r in near if r["decision"] != "dismissed"]
    if len(dismissed) >= MIN_VOTES and len(dismissed) >= 2 * len(kept):
        notes = [r["note"] for r in sorted(dismissed, key=lambda r: str(r["updated_at"]), reverse=True) if r["note"]]
        d["learned"] = {"dismissed_before": len(dismissed), "kept_before": len(kept),
                        "reason": (notes[0][:300] if notes else None)}
        d["previously_dismissed"] = True
        if d.get("severity") != "minor":
            d["severity_found"] = d.get("severity")
            d["severity"] = "minor"
        return "dismissed"
    votes = Counter(r["final_severity"] for r in kept if r["final_severity"] in SEVERITIES)
    n = sum(votes.values())
    if n < MIN_VOTES:
        return None
    sev, count = votes.most_common(1)[0]
    if count < math.ceil(2 * n / 3):
        return None                       # reviewers do not agree - leave the model's grade
    if d.get("needs_confirmation"):
        sev = "minor"
    elif d.get("low_confidence") and sev == "critical":
        sev = "major"
    if sev == d.get("severity"):
        return None
    d["learned"] = {"rated_before": sev, "votes": count, "of": n}
    d["severity_found"] = d.get("severity")
    d["severity"] = sev
    return "rerated"


def apply_feedback(discrepancies: List[Dict[str, Any]]) -> Dict[str, int]:
    """Adjust findings in place from past reviewer decisions. Returns counts of what changed."""
    from fleet import client, profile
    stats = {"dismissed": 0, "rerated": 0, "decisions": 0}
    if not discrepancies or not profile.feature("learn_from_reviews"):
        return stats
    try:
        rows = _load_rows()
    except Exception as exc:  # noqa: BLE001 - no learning store / no embedder: scan as before
        print(f"[feedback] past decisions not available: {exc}")
        return stats
    stats["decisions"] = len(rows)
    if not rows:
        return stats
    vecs = client.embed([finding_text(d) for d in discrepancies])
    for d, v in zip(discrepancies, vecs):
        if not v or d.get("learned"):
            continue
        near = [r for r in rows if _same_kind(d, r) and client.cosine(v, r["embedding"]) >= SIMILARITY]
        if near:
            what = _adjust(d, near)
            if what:
                stats[what] += 1
    print(f"[feedback] {stats['decisions']} past decision(s): {stats['dismissed']} finding(s) like dismissed ones, "
          f"{stats['rerated']} re-rated")
    return stats


def with_feedback(fn):
    """Wrap the comparison node: its checkpoint keeps the model's findings, the decisions are applied on top."""
    def node(state: Dict[str, Any]) -> Dict[str, Any]:
        delta = fn(state)
        found = delta.get("discrepancies") or []
        if not found:
            return delta
        import copy
        found = copy.deepcopy(found)          # never alter the checkpointed copy
        stats = apply_feedback(found)
        if not (stats["dismissed"] or stats["rerated"]):
            return delta
        warnings = list(delta.get("analysis_warnings") or [])
        if stats["dismissed"]:
            warnings.append(f"{stats['dismissed']} finding(s) closely match ones reviewers dismissed on earlier scans. "
                            f"They are kept as minor, marked 'dismissed before', and left out of the drafted RFIs.")
        if stats["rerated"]:
            warnings.append(f"{stats['rerated']} finding(s) were given the severity reviewers chose for similar "
                            f"findings before; the severity as found is shown on each.")
        return {**delta, "discrepancies": found, "analysis_warnings": warnings}
    node.__name__ = getattr(fn, "__name__", "conflict_scan")
    return node


def summary() -> Dict[str, Any]:
    """For the admin console: what the scans have learned so far."""
    import storage
    from sqlalchemy import func, select
    from db import review_feedback as t
    with storage.tx() as conn:
        by_decision = {r[0]: r[1] for r in conn.execute(select(t.c.decision, func.count()).group_by(t.c.decision))}
        by_class = [{"issue_class": r[0], "decision": r[1], "count": r[2]} for r in conn.execute(
            select(t.c.issue_class, t.c.decision, func.count()).group_by(t.c.issue_class, t.c.decision))]
        rerated = [{"from": r[0], "to": r[1], "count": r[2]} for r in conn.execute(
            select(t.c.model_severity, t.c.final_severity, func.count()).where(
                t.c.final_severity.isnot(None), t.c.final_severity != t.c.model_severity).group_by(
                t.c.model_severity, t.c.final_severity))]
    return {"decisions": by_decision, "by_issue_type": by_class, "severity_changes": rerated,
            "similarity": SIMILARITY, "min_votes": MIN_VOTES}
