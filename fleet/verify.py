"""
Second opinions on findings
===========================
After the adjudicator model has decided which candidate pairs conflict, an
independent model (the "verifier" role, gpt-oss by default) re-reads each
finding with the exact words quoted from both sources and answers
confirmed / not_a_conflict / unsure.

Nothing is thrown away: a finding the verifier rejects is marked
`disputed` (shown as "second opinion: not a conflict" with the reason), so
the engineer decides. With vote_critical, critical findings also get a third
independent reading (the extractor role) and are disputed when two of the
three readings say they are not a conflict.

Windows compatible.
"""

from typing import Any, Dict, List

from fleet import client, profile

BATCH = 6

VERIFY_SYSTEM = """\
You are an independent senior engineer double-checking discrepancies that a
colleague reported between construction documents (BOQ, specifications,
drawings, schedules). For each numbered finding you get what each source says,
in its own words.

Decide for each one:
  "confirmed"       - the sources genuinely disagree about the same element at
                      the same place, and a contractor could not resolve it alone
  "not_a_conflict"  - wording only, rounding/tolerance, different elements or
                      places, one side simply more detailed, or the quotes do
                      not support the claim
  "unsure"          - the quotes are not enough to tell

Be strict: only the quoted words count, not the colleague's summary.

Return JSON: {"verdicts": [{"index": <n>, "verdict": "confirmed|not_a_conflict|unsure",
"reason": "<one short sentence>"}]}
"""


def _describe(i: int, d: Dict[str, Any]) -> str:
    lines = [f"--- FINDING {i} ---", f"Topic: {d.get('topic') or '-'}",
             f"Claimed problem: {d.get('description') or '-'}"]
    evidence = d.get("evidence") or []
    if evidence:
        for e in evidence[:6]:
            lines.append(f"Source ({e.get('side', '?').upper()}, {e.get('file') or ''} {e.get('reference') or ''}): "
                         f"\"{(e.get('says') or e.get('quote') or '').strip()[:500]}\"")
    else:
        lines.append(f"Set A says: \"{str(d.get('document_a_says') or '')[:500]}\"")
        lines.append(f"Set B says: \"{str(d.get('document_b_says') or '')[:500]}\"")
    return "\n".join(lines)


def _ask(role: str, batch: List[Dict[str, Any]]) -> Dict[int, Dict[str, str]]:
    user = "\n\n".join(_describe(i, d) for i, d in enumerate(batch))
    try:
        result = client.llm(role).run(system=VERIFY_SYSTEM, user=user, temperature=0.0, max_retries=1)
    except client.ModelUnavailable as exc:
        print(f"[verify] {role} unavailable: {exc}")
        return {}
    out = {}
    for v in result.get("verdicts") or []:
        if isinstance(v, dict) and isinstance(v.get("index"), int) and 0 <= v["index"] < len(batch):
            verdict = str(v.get("verdict") or "unsure").lower()
            if verdict not in ("confirmed", "not_a_conflict", "unsure"):
                verdict = "unsure"
            out[v["index"]] = {"verdict": verdict, "reason": str(v.get("reason") or "")[:300]}
    return out


def verify_findings(discrepancies: List[Dict[str, Any]]) -> Dict[str, int]:
    """Annotate findings in place with `verification` (and `disputed`). Returns counts."""
    stats = {"checked": 0, "confirmed": 0, "not_a_conflict": 0, "unsure": 0, "disputed": 0}
    if not discrepancies or not profile.feature("verify_findings") or not profile.role("verifier"):
        return stats
    model = client.resolved_model("verifier")
    batches = [discrepancies[i:i + BATCH] for i in range(0, len(discrepancies), BATCH)]
    print(f"[verify] second opinion on {len(discrepancies)} finding(s) with {model}")
    answers = client.pmap(lambda b: _ask("verifier", b), batches, role="verifier")
    for batch, verdicts in zip(batches, answers):
        for i, d in enumerate(batch):
            v = verdicts.get(i)
            if not v:
                continue
            stats["checked"] += 1
            stats[v["verdict"]] += 1
            d["verification"] = {**v, "model": model}
            if v["verdict"] == "not_a_conflict":
                d["disputed"] = True

    if profile.feature("vote_critical") and profile.role("extractor"):
        critical = [d for d in discrepancies if (d.get("severity") or "").lower() == "critical"]
        if critical:
            print(f"[verify] third reading of {len(critical)} critical finding(s)")
            third_batches = [critical[i:i + BATCH] for i in range(0, len(critical), BATCH)]
            third = client.pmap(lambda b: _ask("extractor", b), third_batches, role="extractor")
            for batch, verdicts in zip(third_batches, third):
                for i, d in enumerate(batch):
                    v = verdicts.get(i)
                    if not v:
                        continue
                    votes = ["confirmed", (d.get("verification") or {}).get("verdict", "unsure"), v["verdict"]]
                    against = votes.count("not_a_conflict")
                    d["vote"] = {"for": votes.count("confirmed"), "against": against, "unsure": votes.count("unsure")}
                    d["disputed"] = against >= 2
    stats["disputed"] = sum(1 for d in discrepancies if d.get("disputed"))
    print(f"[verify] {stats}")
    return stats
