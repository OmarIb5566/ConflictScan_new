"""
Accuracy and speed benchmark on real past RFIs
==============================================
Each case is one real RFI from the training folder: its attachments are the
documents the engineer compared, and its subject + request is the issue the
engineer actually raised. A case is scanned with ConflictScan and scored:

    found      did any finding describe the engineer's issue?  (meaning
               similarity with bge-m3 >= --threshold, or strong keyword overlap)
    findings   how many findings were raised (fewer, if the issue is still
               found, means less noise for the engineer to triage)
    disputed   findings the independent verifier rejected
    time       wall time per pipeline stage, model time per role, cache hits

Real RFIs list only the issue that was raised, so other findings are not
necessarily wrong - "findings per case" is a noise indicator, not precision.

    python -m bench.run --cases 5 --profile baseline --profile workstation
    python -m bench.run --list                     # what is in the training folder
    python -m bench.run --compare data/bench/*     # side-by-side table of earlier runs

Results go to data/bench/<time>-<profile>/ (results.json + report.md).
Nothing leaves the machine; the case list and results stay under data/.

Windows compatible.
"""

import config  # noqa: F401

import argparse
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_DATA = os.path.join(ROOT, "RFI training data")
CONFLICT_WORDS = re.compile(r"conflict|discrepanc|clash|mismatch|differ|contradict|inconsisten|not match|"
                            r"missing|clarif|confirm", re.I)


def build_cases(root: str, limit: int, max_pages: int = 40, seed_words: bool = True) -> List[Dict[str, Any]]:
    """Cases with at least two usable attachments, smallest first (fast runs), conflict-type RFIs preferred."""
    from fleet.corpus import load_rfis
    from nodes.file_ingest import classify_file
    import pymupdf

    cases = []
    for rec in load_rfis(root, with_replies=False):
        if len(rec["attachments"]) < 2 or len(rec["text"]) < 40:
            continue
        pages = 0
        for f in rec["attachments"]:
            if f.lower().endswith(".pdf"):
                try:
                    with pymupdf.open(f) as d:
                        pages += d.page_count
                except Exception:  # noqa: BLE001
                    pages += 99
            else:
                pages += 1
        if pages > max_pages:
            continue
        rec["pages"] = pages
        rec["conflict_type"] = bool(CONFLICT_WORDS.search(f"{rec['subject']} {rec['text']}"))
        cases.append(rec)
    cases.sort(key=lambda c: (not c["conflict_type"] if seed_words else 0, c["pages"]))
    picked = cases[:limit]
    for c in picked:
        kinds = []
        for f in c["attachments"]:
            try:
                kinds.append(classify_file(f, use_vision=False)[0])
            except Exception:  # noqa: BLE001
                kinds.append("document")
        docs = [f for f, k in zip(c["attachments"], kinds) if k != "drawing"]
        draws = [f for f, k in zip(c["attachments"], kinds) if k == "drawing"]
        if docs and draws:
            c["set_a"], c["set_b"] = docs, draws
        else:
            half = max(1, len(c["attachments"]) // 2)
            c["set_a"], c["set_b"] = c["attachments"][:half], c["attachments"][half:]
    return picked


def _keywords(text: str) -> set:
    stop = {"the", "and", "for", "with", "this", "that", "from", "please", "kindly", "advise", "confirm", "between",
            "drawing", "drawings", "shown", "show", "are", "not", "has", "have", "which", "per", "rfi"}
    return {w for w in re.findall(r"[a-z0-9؀-ۿ]{3,}", text.lower()) if w not in stop}


def score_case(case: Dict[str, Any], discrepancies: List[Dict[str, Any]], threshold: float) -> Dict[str, Any]:
    from fleet import client
    target = f"{case['subject']}. {case['text']}"[:2000]
    texts = [f"{d.get('topic') or ''}. {d.get('description') or ''}" for d in discrepancies]
    best, best_i, how = 0.0, -1, ""
    if texts:
        vecs = client.embed([target] + texts)
        if vecs[0]:
            for i, v in enumerate(vecs[1:]):
                s = client.cosine(vecs[0], v) if v else 0.0
                if s > best:
                    best, best_i = s, i
            how = "meaning"
        tk = _keywords(target)
        for i, t in enumerate(texts):
            overlap = len(tk & _keywords(t)) / max(1, min(len(tk), 12))
            if overlap >= 0.5 and best < threshold:
                best, best_i, how = max(best, threshold), i, "keywords"
    return {"found": best >= threshold, "similarity": round(best, 3), "matched_by": how,
            "best_finding": texts[best_i][:300] if best_i >= 0 else ""}


def run_case(case: Dict[str, Any], profile_name: str, work_root: str) -> Dict[str, Any]:
    from fleet import client, profile
    from graph import run_conflict_scan
    import jobs

    files = {}
    for slot in ("a", "b"):
        files[slot] = []
        for f in case[f"set_{slot}"]:
            kind, reason = "auto", ""
            try:
                from nodes.file_ingest import classify_file
                kind, reason = classify_file(f)
            except Exception:  # noqa: BLE001
                kind = "document"
            files[slot].append({"name": os.path.basename(f), "path": f, "kind": kind, "classified_as": reason})
        from nodes.file_ingest import pair_drawing_files
        files[slot] = pair_drawing_files(files[slot])

    run_tag = f"bench{datetime.now():%H%M%S}{abs(hash((case['rfi_no'], profile_name))) % 10000:04d}"[:32]
    stage_t: Dict[str, float] = {}
    last = [time.time()]

    def on_node(node, _delta):
        now = time.time()
        stage_t[node] = round(now - last[0], 1)
        last[0] = now

    out_dir = tempfile.mkdtemp(prefix="bench_", dir=work_root)
    token = client.current_run.set(run_tag)
    started = time.time()
    error = None
    state: Dict[str, Any] = {}
    try:
        with profile.use(profile_name):
            state = run_conflict_scan(
                user_id="benchmark", document_a_text="", document_b_text="",
                document_a_label="Set A (attachments)", document_b_label="Set B (attachments)",
                document_a_files=files["a"], document_b_files=files["b"], project_name=case["project_name"],
                rfi_grouping="consolidated", on_node_complete=on_node, output_dir=out_dir)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)[:500]
    finally:
        client.current_run.reset(token)
        shutil.rmtree(out_dir, ignore_errors=True)
    discs = state.get("discrepancies") or []
    report = jobs.fleet_report(run_tag)
    return {
        "rfi_no": case["rfi_no"], "project": case["project_name"], "subject": case["subject"],
        "pages": case["pages"], "files_a": len(case["set_a"]), "files_b": len(case["set_b"]),
        "seconds": round(time.time() - started, 1), "stages": stage_t, "error": error,
        "findings": len(discs), "disputed": sum(1 for d in discs if d.get("disputed")),
        "rfis": len(state.get("rfi_items") or []), "fleet": report,
        "_discrepancies": discs,
        "finding_topics": [f"{d.get('severity', '')}: {d.get('topic', '')}" for d in discs][:15],
    }


def summarise(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok = [r for r in results if not r.get("error")]
    n = len(ok) or 1
    return {
        "cases": len(results), "failed": len(results) - len(ok),
        "issue_found_rate": round(sum(1 for r in ok if r["score"]["found"]) / n, 3),
        "mean_similarity": round(sum(r["score"]["similarity"] for r in ok) / n, 3),
        "findings_per_case": round(sum(r["findings"] for r in ok) / n, 2),
        "disputed_per_case": round(sum(r["disputed"] for r in ok) / n, 2),
        "seconds_per_case": round(sum(r["seconds"] for r in ok) / n, 1),
        "model_seconds_per_case": round(sum((r["fleet"] or {}).get("model_seconds", 0) for r in ok) / n, 1),
        "cache_hit_share": round(sum((r["fleet"] or {}).get("cached", 0) for r in ok)
                                 / max(1, sum((r["fleet"] or {}).get("calls", 0) for r in ok)), 3),
    }


def write_report(out: str, profile_name: str, results: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    lines = [f"# Benchmark - profile `{profile_name}`", "", f"{datetime.now():%Y-%m-%d %H:%M}", "",
             "| Metric | Value |", "|---|---|"]
    lines += [f"| {k.replace('_', ' ')} | {v} |" for k, v in summary.items()]
    lines += ["", "| RFI | Pages | Found | Similarity | Findings | Disputed | Seconds |", "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['rfi_no']} - {r['subject'][:50]} | {r['pages']} | {'yes' if r['score']['found'] else 'no'} | "
                     f"{r['score']['similarity']} | {r['findings']} | {r['disputed']} | {r['seconds']}"
                     + (f" (error: {r['error'][:60]})" if r.get("error") else "") + " |")
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def compare(paths: List[str]) -> None:
    rows = []
    for p in paths:
        f = os.path.join(p, "results.json")
        if os.path.isfile(f):
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            rows.append((os.path.basename(p), data["summary"]))
    if not rows:
        print("No results found.")
        return
    keys = list(rows[0][1].keys())
    print(f"{'metric':28s}" + "".join(f"{name[-24:]:>26s}" for name, _ in rows))
    for k in keys:
        print(f"{k:28s}" + "".join(f"{str(s.get(k)):>26s}" for _, s in rows))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.run")
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--cases", type=int, default=5)
    ap.add_argument("--max-pages", type=int, default=40)
    ap.add_argument("--profile", action="append")
    ap.add_argument("--threshold", type=float, default=0.62)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--compare", nargs="*")
    ap.add_argument("--no-cache", action="store_true", help="measure cold runs (the response cache is bypassed)")
    a = ap.parse_args(argv)

    if a.compare is not None:
        compare(sorted(p for pat in (a.compare or [os.path.join(ROOT, "data", "bench", "*")]) for p in glob.glob(pat)))
        return 0
    import storage
    storage.init_db()
    cases = build_cases(a.data, a.cases, a.max_pages)
    if a.list:
        for c in cases:
            print(f"{c['rfi_no']:32s} {c['pages']:3d}p  A:{len(c['set_a'])} B:{len(c['set_b'])}  {c['subject'][:70]}")
        return 0
    if a.no_cache:
        from fleet import client
        client.CACHE_ENABLED = False
    work_root = os.path.join(storage.DATA_DIR, "scratch")
    os.makedirs(work_root, exist_ok=True)
    for profile_name in a.profile or ["workstation"]:
        out = os.path.join(storage.DATA_DIR, "bench", f"{datetime.now():%Y%m%d-%H%M}-{profile_name}")
        os.makedirs(out, exist_ok=True)
        results = []
        for n, case in enumerate(cases, start=1):
            print(f"\n=== [{profile_name}] case {n}/{len(cases)}: {case['rfi_no']} - {case['subject'][:70]} "
                  f"({case['pages']} pages) ===", flush=True)
            r = run_case(case, profile_name, work_root)
            r["score"] = score_case(case, r.pop("_discrepancies"), a.threshold)
            print(f"[{profile_name}] {case['rfi_no']}: found={r['score']['found']} sim={r['score']['similarity']} "
                  f"findings={r['findings']} {r['seconds']}s", flush=True)
            results.append(r)
            with open(os.path.join(out, "partial.json"), "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        summary = summarise(results)
        write_report(out, profile_name, results, summary)
        with open(os.path.join(out, "results.json"), "w", encoding="utf-8") as f:
            json.dump({"profile": profile_name, "summary": summary, "results": results}, f, indent=2,
                      ensure_ascii=False, default=str)
        print(f"\n[{profile_name}] {summary}\nWritten: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
