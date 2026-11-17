"""
RFI knowledge base - ConflictScan / RFI Automation
===================================================
Loads knowledge/rfi_taxonomy.json: the kinds of conflict, gap and missing
information engineers actually raise RFIs for, learned from a corpus of real
project RFIs and written in project-neutral terms.

The taxonomy feeds three places:
    * conflict_scan prompts  - what to look for, what is NOT a discrepancy
    * severity / type defaults for findings the model leaves unclassified
    * rfi_drafting           - house style, request types, grouping rules

The JSON is the single source of truth and can be extended from any project's
RFI log with tools/analyze_rfi_log.py - nothing in here is tied to the project
the corpus came from.
"""

import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

TAXONOMY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "knowledge", "rfi_taxonomy.json")

# Coarse finding types the register understands. issue_class (taxonomy key)
# refines these; "type" stays coarse so older registers remain comparable.
VALID_TYPES = (
    "contradiction", "omission", "ambiguity", "scope_gap",
    "clash", "missing_information", "design_change",
)


@lru_cache(maxsize=1)
def load_taxonomy() -> Dict[str, Any]:
    try:
        with open(TAXONOMY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        print(f"[rfi_knowledge] Could not load {TAXONOMY_PATH}: {exc} - running without the taxonomy")
        return {"issue_classes": [], "not_discrepancies": [], "rfi_style": {}}


def issue_classes(detectable_only: bool = True) -> List[Dict[str, Any]]:
    classes = load_taxonomy().get("issue_classes") or []
    return [c for c in classes if c.get("detectable", True) or not detectable_only]


def issue_class(key: str) -> Optional[Dict[str, Any]]:
    for c in load_taxonomy().get("issue_classes") or []:
        if c.get("key") == key:
            return c
    return None


def normalise_issue_class(value: Any) -> str:
    key = re.sub(r"[^a-z]+", "_", str(value or "").lower()).strip("_")
    return key if issue_class(key) else ""


# ---------------------------------------------------------------------------
# Prompt fragments - kept compact: a small local model reads them on every call
# ---------------------------------------------------------------------------

def issue_class_prompt(keys: Optional[List[str]] = None, with_cues: bool = False) -> str:
    """One line per issue class: '<key>: <definition>' (+ cues when asked)."""
    lines = []
    for c in issue_classes():
        if keys and c["key"] not in keys:
            continue
        lines.append(f"- {c['key']}: {c['definition']}")
        if with_cues:
            for cue in c.get("cues", [])[:4]:
                lines.append(f"    e.g. {cue}")
    return "\n".join(lines)


def not_discrepancy_prompt() -> str:
    return "\n".join(f"- {rule}" for rule in load_taxonomy().get("not_discrepancies") or [])


def rfi_style_prompt() -> str:
    style = load_taxonomy().get("rfi_style") or {}
    if not style:
        return ""
    closings = style.get("closing_by_request_type") or {}
    lines = [
        f"- {style.get('opening', '')}",
        f"- {style.get('grouping', '')}",
        f"- {style.get('point_form', '')}",
        f"- {style.get('commercial_note', '')}",
        f"- {style.get('attachments', '')}",
        "- Close each point (or the RFI) according to its request type:",
        *[f"    {k}: \"{v}\"" for k, v in closings.items()],
        "- Subject patterns: " + "; ".join(style.get("subject_patterns") or []),
    ]
    return "\n".join(line for line in lines if line.strip() not in ("-", ""))


def closing_for(request_type: str) -> str:
    closings = (load_taxonomy().get("rfi_style") or {}).get("closing_by_request_type") or {}
    return closings.get(request_type) or closings.get("clarify") or "Kindly clarify."


# ---------------------------------------------------------------------------
# Defaults and keyword classification
# ---------------------------------------------------------------------------

def default_severity(key: str) -> str:
    c = issue_class(key)
    return (c or {}).get("default_severity", "minor")


def default_type(key: str) -> str:
    c = issue_class(key)
    t = (c or {}).get("type", "")
    return t if t in VALID_TYPES else ""


def request_type_for(key: str) -> str:
    c = issue_class(key)
    return (c or {}).get("request_type", "clarify")


@lru_cache(maxsize=1)
def _compiled_keywords() -> List[tuple]:
    out = []
    for c in issue_classes(detectable_only=False):
        pats = []
        for kw in c.get("keywords", []):
            try:
                pats.append(re.compile(kw, re.IGNORECASE))
            except re.error:
                pats.append(re.compile(re.escape(kw), re.IGNORECASE))
        out.append((c["key"], pats))
    return out


def classify_text(text: str, top: int = 3) -> List[tuple]:
    """
    Keyword vote over the taxonomy: [(issue_class, hits), ...] best first.
    Used to fill issue_class when the model gives none, and by
    tools/analyze_rfi_log.py to measure a project's RFI mix.
    """
    text = str(text or "")
    scores = []
    for key, pats in _compiled_keywords():
        hits = sum(1 for p in pats if p.search(text))
        if hits:
            scores.append((key, hits))
    scores.sort(key=lambda kv: -kv[1])
    return scores[:top]


def best_issue_class(text: str, fallback: str = "") -> str:
    ranked = classify_text(text, top=1)
    return ranked[0][0] if ranked else fallback
