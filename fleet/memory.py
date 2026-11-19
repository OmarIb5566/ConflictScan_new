"""
RFI memory - the company's past RFIs and client responses, searchable by meaning
================================================================================
Sources:
    training   real RFIs from past projects (python manage.py index-rfis <folder>)
    imported   an existing project RFI log imported in the app
    issued     RFIs issued from ConflictScan (added when an RFI is issued / answered)

Used for:
- drafting: the drafter sees how the company phrased the most similar past
  RFIs (house style, level of detail), never their content as facts;
- classification hints and "this was asked before" notes on findings.

Embeddings come from the embedder role (bge-m3) and are stored with each row,
so search is a cosine ranking in memory - fine for tens of thousands of RFIs.

Windows compatible.
"""

import threading
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, insert, select, update

from fleet import client, profile

_index: Dict[str, Any] = {"at": 0.0, "rows": []}
_lock = threading.Lock()


def _text_of(subject: str, text: str) -> str:
    return f"{subject}\n{text}".strip()[:3000]


def add(items: List[Dict[str, Any]], source: str, embed: bool = True) -> int:
    """Insert or refresh memory rows: [{project_name, rfi_no, subject, text, discipline, response, issue_class}]."""
    import storage
    from db import rfi_memory
    items = [i for i in items if (i.get("subject") or i.get("text"))]
    vectors = client.embed([_text_of(i.get("subject", ""), i.get("text", "")) for i in items]) if embed else [None] * len(items)
    model = client.resolved_model("embedder") if embed else None
    n = 0
    with storage.tx() as conn:
        for item, vec in zip(items, vectors):
            values = {
                "subject": str(item.get("subject") or "")[:500], "text": str(item.get("text") or "")[:8000],
                "discipline": (item.get("discipline") or None), "issue_class": item.get("issue_class"),
                "response": item.get("response"), "embedding": vec, "embed_model": model if vec else None,
            }
            key = and_(rfi_memory.c.source == source, rfi_memory.c.project_name == str(item.get("project_name") or "")[:200],
                       rfi_memory.c.rfi_no == str(item.get("rfi_no") or "")[:120])
            if conn.execute(update(rfi_memory).where(key).values(**values)).rowcount == 0:
                conn.execute(insert(rfi_memory).values(
                    id=storage.new_id(), source=source, project_name=str(item.get("project_name") or "")[:200],
                    rfi_no=str(item.get("rfi_no") or "")[:120], created_at=storage.now(), **values))
            n += 1
    _index["at"] = 0.0
    return n


def _rows() -> List[Dict[str, Any]]:
    import storage
    from db import rfi_memory
    with _lock:
        if time.time() - _index["at"] > 300:
            with storage.tx() as conn:
                _index["rows"] = [dict(r._mapping) for r in conn.execute(select(rfi_memory).where(
                    rfi_memory.c.embedding.isnot(None)))]
            _index["at"] = time.time()
        return _index["rows"]


def search(query: str, k: int = 3, min_score: float = 0.55, exclude_project: Optional[str] = None) -> List[Dict[str, Any]]:
    if not profile.feature("rfi_memory") or not query.strip():
        return []
    rows = _rows()
    if not rows:
        return []
    vec = client.embed([query])[0]
    if not vec:
        return []
    scored = []
    for r in rows:
        if exclude_project and r["project_name"] == exclude_project:
            continue
        s = client.cosine(vec, r["embedding"])
        if s >= min_score:
            scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    return [{**{k2: r[k2] for k2 in ("source", "project_name", "rfi_no", "subject", "text", "discipline", "response")},
             "score": round(s, 3)} for s, r in scored[:k]]


def style_examples(query: str, k: int = 2) -> str:
    """Prompt fragment: how the company worded the most similar past RFIs."""
    hits = search(query, k=k)
    if not hits:
        return ""
    parts = ["", "HOW THIS COMPANY WORDED SIMILAR PAST RFIs (style reference only - do NOT copy facts, values or "
                 "locations from them):"]
    for h in hits:
        parts.append(f"- [{h['rfi_no']}] Subject: {h['subject']}\n  Request: {h['text'][:600]}")
    return "\n".join(parts)


def count() -> Dict[str, int]:
    import storage
    from db import rfi_memory
    from sqlalchemy import func
    with storage.tx() as conn:
        rows = conn.execute(select(rfi_memory.c.source, func.count()).group_by(rfi_memory.c.source)).all()
    return {r[0]: r[1] for r in rows}
