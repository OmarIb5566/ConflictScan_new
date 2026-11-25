"""
User-facing wording
===================
What an engineer reads - findings, RFIs, the register - must talk about the
documents, never about how they were processed (OCR, models, tiles, text
layers, confidence). Models sometimes echo those words from their inputs, so
every user-facing text passes through `user_text()`.

Windows compatible.
"""

import re
from typing import Any

# Phrases rewritten into plain engineering language.
_REWRITES = [
    (re.compile(r"\bOCR[- ]?(?:errors|misreads|mistakes)\b", re.I), "misprints"),
    (re.compile(r"\bOCR[- ]?(?:read|error|misread|mistake)\b", re.I), "a misprint"),
    (re.compile(r"\b(?:partly|partially) legible\b", re.I), "unclear on the drawing"),
    (re.compile(r"\billegible\b|\bunreadable\b", re.I), "unclear on the drawing"),
    (re.compile(r"\bmisread\b", re.I), "unclear"),
]

# Bracketed notes that only describe processing - removed outright.
_PROCESS_NOTE = re.compile(
    r"\s*[\(\[][^()\[\]]*(?:OCR|verify|legib|vision|model|text layer|second reading|tile)[^()\[\]]*[\)\]]", re.I)

# Sentences still mentioning processing after the rewrites are dropped.
_INTERNAL = re.compile(
    r"\b(?:OCR|optical character|vision model|language model|LLM|AI model|the model|models?\b(?= (?:read|saw|reported))|"
    r"tiles?\b|text layer|confidence (?:level|score)|low[- ]confidence|extraction|extracted by|"
    r"second reading|drawing reader|was read (?:by|from) the)", re.I)

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def user_text(value: Any) -> str:
    """Text an engineer reads, without any mention of how the documents were processed."""
    text = str(value or "")
    if not text.strip():
        return ""
    text = _PROCESS_NOTE.sub("", text)
    for rx, repl in _REWRITES:
        text = rx.sub(repl, text)
    out_lines = []
    for line in text.splitlines():
        sentences = [s for s in _SENTENCE.split(line) if s.strip()]
        kept = [s for s in sentences if not _INTERNAL.search(s)]
        if sentences and not kept:
            continue
        out_lines.append(" ".join(kept))
    text = "\n".join(out_lines)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    return text.strip()


def has_internal_terms(value: Any) -> bool:
    """For tests / checks: does the text still mention processing internals?"""
    return bool(_INTERNAL.search(str(value or "")) or re.search(r"\bOCR\b|legib", str(value or ""), re.I))
