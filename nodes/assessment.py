"""
Honest assessment of a scan
===========================
The scan's job is to find real conflicts - not to produce many. This module
decides two things deterministically (no model involved), so the answer
cannot be talked up:

1. assess_inputs()  - after the documents are read: is there enough in BOTH
   sets to compare? If a set is empty or unreadable the scan stops and says
   why, instead of hunting for "gaps" against nothing.

2. assess_scan()    - after the comparison: a plain verdict with the evidence
   behind it - how many statements could actually be compared, how many real
   findings (independently doubted ones and OCR-based ones counted apart),
   and what limited the scan.

Verdicts:
    insufficient_input     one set (or both) gave nothing usable - nothing was compared
    no_overlap             the sets share almost no subject matter - few comparisons possible
    consistent             compared, no conflicts found
    minor_only             only minor discrepancies (or ones resting on uncertain readings)
    conflicts              significant (critical / major) conflicts found

Windows compatible.
"""

from typing import Any, Dict, List

MIN_ITEMS = 5            # fewer statements than this in a set: compare, but say the set is thin
LOW_OVERLAP_PAIRS = 3    # fewer paired statements than this (and < 15% of the smaller set): little in common
LOW_OVERLAP_SHARE = 0.15

VERDICT_LABEL = {
    "insufficient_input": "Not enough to compare",
    "no_overlap": "Little in common to compare",
    "consistent": "No conflicts found",
    "minor_only": "Minor discrepancies only",
    "conflicts": "Conflicts found",
}


def _items(profile: Dict[str, Any]) -> int:
    return len(profile.get("requirements") or []) + len(profile.get("quantities") or [])


def _set_facts(profile: Dict[str, Any]) -> Dict[str, Any]:
    files = [f for f in profile.get("files") or [] if not f.get("duplicate_of")]   # a copy read once
    unreadable = [f.get("name") for f in files
                  if f.get("kind") != "drawing" and not f.get("chars") and not f.get("requirements")
                  and not f.get("quantities")]
    empty_drawings = [f.get("name") for f in files
                      if f.get("kind") == "drawing" and not f.get("requirements") and not f.get("quantities")]
    return {"label": profile.get("label") or "", "files": len(files), "items": _items(profile),
            "chars": profile.get("char_count") or 0, "unreadable_files": unreadable,
            "empty_drawings": empty_drawings,
            "ocr_pages": sum(int(f.get("ocr_pages") or 0) for f in files),
            "tiles": sum(int(f.get("tiles") or 0) for f in files),
            "cut_off_tiles": sum(int(f.get("cut_off_tiles") or 0) for f in files),
            "failed_tiles": sum(int(f.get("failed_tiles") or 0) for f in files)}


def assess_inputs(profile_a: Dict[str, Any], profile_b: Dict[str, Any]) -> Dict[str, Any]:
    """Enough in both sets to compare? Problems are written for the engineer, naming the files."""
    sets = {"a": _set_facts(profile_a), "b": _set_facts(profile_b)}
    problems: List[str] = []
    limitations: List[str] = []
    for slot, s in sets.items():
        name = s["label"] or f"Set {slot.upper()}"
        bad = s["unreadable_files"] + s["empty_drawings"]
        if s["items"] == 0:
            if not s["files"] and not s["chars"]:
                problems.append(f"'{name}' is empty - no files or text were given.")
            elif bad:
                problems.append(
                    f"Nothing usable could be read from '{name}' ({', '.join(bad[:5])}"
                    f"{' and others' if len(bad) > 5 else ''}). The files may be blank, image-only scans whose text "
                    f"could not be recognised, or contain no technical statements.")
            else:
                problems.append(f"'{name}' was read, but it contains no requirements, values or quantities to compare.")
        else:
            if s["items"] < MIN_ITEMS:
                limitations.append(
                    f"'{name}' gave only {s['items']} comparable statement(s); the comparison can only cover those.")
            if bad:
                limitations.append(f"Nothing could be read from {', '.join(bad[:5])} in '{name}'.")
        # Said in terms of the drawings, not of how they were read (tiles / model replies stay in diagnostics).
        if s["failed_tiles"] and s["tiles"]:
            share = max(1, round(100 * s["failed_tiles"] / s["tiles"]))
            limitations.append(
                f"About {share}% of the drawing area in '{name}' could not be read; anything there was not compared.")
        if s["cut_off_tiles"] and s["tiles"]:
            share = max(1, round(100 * s["cut_off_tiles"] / s["tiles"]))
            limitations.append(
                f"About {share}% of the drawing area in '{name}' was read only in part; some details there may "
                f"not have been compared.")
    ok = not problems
    out = {"ok": ok, "sets": sets, "problems": problems, "limitations": limitations}
    if not ok:
        out.update(verdict="insufficient_input", label=VERDICT_LABEL["insufficient_input"],
                   headline="The scan was stopped before comparing: " + " ".join(problems)
                            + " Add readable documents to the set and run it again.")
    return out


def little_in_common(pairs: int, items_a: int, items_b: int) -> bool:
    smaller = max(1, min(items_a, items_b))
    return pairs < LOW_OVERLAP_PAIRS and pairs / smaller < LOW_OVERLAP_SHARE


def assess_scan(discrepancies: List[Dict[str, Any]], profile_a: Dict[str, Any], profile_b: Dict[str, Any],
                compared: Dict[str, int], input_assessment: Dict[str, Any], extra_limitations: List[str]) -> Dict[str, Any]:
    """The verdict and the plain-language headline shown first in the results."""
    real = [d for d in discrepancies if not d.get("disputed")]
    doubted = len(discrepancies) - len(real)
    uncertain = [d for d in real if d.get("low_confidence")]
    solid = [d for d in real if not d.get("low_confidence")]
    sev = {s: sum(1 for d in solid if (d.get("severity") or "").lower() == s) for s in ("critical", "major", "minor")}
    significant = sev["critical"] + sev["major"]
    pairs = compared.get("pairs", 0)
    checks = pairs + compared.get("internal_pairs", 0) + compared.get("interface_clusters", 0)
    items_a, items_b = _items(profile_a), _items(profile_b)
    limitations = list(input_assessment.get("limitations") or []) + list(extra_limitations)

    overlap_low = little_in_common(pairs, items_a, items_b)
    if not real and overlap_low:
        verdict = "no_overlap"
    elif not real:
        verdict = "consistent"
    elif significant == 0:
        verdict = "minor_only"
    else:
        verdict = "conflicts"

    basis = f"{pairs} pair(s) of statements about the same items were compared" if pairs else "no statements could be paired"
    if verdict == "no_overlap":
        headline = (f"The two sets have little in common to compare: {basis} ({items_a} statement(s) in one, "
                    f"{items_b} in the other). No conflicts were found, but that says little - check that the "
                    f"right documents are in each set.")
    elif verdict == "consistent":
        headline = f"No conflicts found: {basis}, and they agree."
    elif verdict == "minor_only":
        parts = []
        if sev["minor"]:
            parts.append(f"{sev['minor']} minor discrepanc{'y' if sev['minor'] == 1 else 'ies'}")
        if uncertain:
            parts.append(f"{len(uncertain)} point(s) to confirm on the drawings")
        headline = (f"Mostly consistent: {' and '.join(parts)}; {basis}. "
                    f"Nothing found should hold up work.")
    else:
        bits = [f"{sev['critical']} critical" if sev["critical"] else "", f"{sev['major']} major" if sev["major"] else ""]
        headline = (f"{significant} significant conflict(s) ({', '.join(b for b in bits if b)})"
                    + (f" and {sev['minor']} minor" if sev["minor"] else "")
                    + (f", plus {len(uncertain)} point(s) to confirm on the drawings" if uncertain else "")
                    + f"; {basis}.")
    if doubted:
        headline += (f" {doubted} further finding(s) were judged not to be real conflicts by an independent check "
                     f"- shown for you to confirm, not counted above.")
    if checks and real and significant <= 3 and len(real) / max(1, checks) < 0.05:
        headline += " Apart from these, the documents agree on what was compared."
    return {
        "verdict": verdict, "label": VERDICT_LABEL[verdict], "headline": headline,
        "counts": {"real": len(real), "significant": significant, **sev, "uncertain": len(uncertain),
                   "doubted": doubted, "total_raised": len(discrepancies)},
        "compared": {**compared, "items_a": items_a, "items_b": items_b},
        "limitations": limitations,
    }
