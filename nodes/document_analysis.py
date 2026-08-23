"""
Document Analysis Node - ConflictScan / RFI Automation
======================================================
Builds an independent, structured profile of EACH of the two input documents.

Design principles
-----------------
* Document-agnostic: the node knows nothing about BOQs, specifications, or
  material lists. It reads whatever it is given and extracts the assertions
  that document makes.
* Full coverage: every document is chunked and EVERY chunk is sent to the LLM.
  Relevant statements can sit on non-contiguous pages, so nothing is sampled
  or discarded.
* Each document is profiled in isolation. Cross-document comparison happens
  later, in nodes/conflict_scan.py - keeping extraction free of any bias about
  what the "other" document says.

LLM backend: Ollama via nodes/llm_client.py.
"""

import re
from typing import Dict, Any, List

# ---------------------------------------------------------------------------
# Lazy-loaded LLM client (avoids circular imports at module load time)
# ---------------------------------------------------------------------------
_llm_client_instance = None


def _get_llm():
    """Return the shared Ollama llm_client, importing it once on first call."""
    global _llm_client_instance
    if _llm_client_instance is None:
        from nodes.llm_client import llm_client
        _llm_client_instance = llm_client
    return _llm_client_instance


# ---------------------------------------------------------------------------
# Chunking constants
# ---------------------------------------------------------------------------
CHUNK_SIZE = 8_000    # characters per chunk
CHUNK_OVERLAP = 500   # character overlap between consecutive chunks


def chunk_document(text: str) -> List[Dict[str, Any]]:
    """
    Split *text* into overlapping chunks of ~CHUNK_SIZE characters.

    Strategy
    --------
    * Split on paragraph boundaries (double-newline) first so a sentence is
      never cut mid-word.
    * Accumulate paragraphs until the chunk limit is reached, close the chunk,
      then seed the next one with the last CHUNK_OVERLAP characters of the
      previous chunk (context carry-over).
    * Every chunk is tagged with its index and the approximate character range
      it covers in the original document.

    Returns a list of dicts with keys:
        chunk_index, total_chunks, char_start, char_end, text

    Guarantee: no character of *text* is silently dropped.
    """
    if not text:
        return []

    paragraphs = re.split(r"\n{2,}", text)

    chunks: List[Dict[str, Any]] = []
    current_parts: List[str] = []
    current_len = 0
    char_cursor = 0
    chunk_start = 0

    def _flush() -> str:
        """Close the current chunk and return the overlap seed for the next one."""
        nonlocal chunk_start
        chunk_text = "\n\n".join(current_parts)
        char_end = char_cursor
        chunks.append(
            {
                "chunk_index": len(chunks),
                "total_chunks": 0,   # patched below
                "char_start": chunk_start,
                "char_end": char_end,
                "text": chunk_text,
            }
        )
        chunk_start = max(chunk_start, char_end - CHUNK_OVERLAP)
        return chunk_text[-CHUNK_OVERLAP:] if len(chunk_text) > CHUNK_OVERLAP else chunk_text

    overlap_carry = ""

    for para in paragraphs:
        para_len = len(para) + 2  # +2 for the \n\n separator

        # A single paragraph larger than CHUNK_SIZE -> hard-split it
        if para_len > CHUNK_SIZE:
            sub_chunks = [
                para[i: i + CHUNK_SIZE]
                for i in range(0, len(para), CHUNK_SIZE - CHUNK_OVERLAP)
            ]
            for sub in sub_chunks:
                if current_parts:
                    overlap_carry = _flush()
                    current_parts = [overlap_carry] if overlap_carry else []
                    current_len = len(overlap_carry)
                current_parts.append(sub)
                current_len += len(sub)
                char_cursor += len(sub)
            continue

        if current_len + para_len > CHUNK_SIZE and current_parts:
            overlap_carry = _flush()
            current_parts = [overlap_carry] if overlap_carry else []
            current_len = len(overlap_carry)

        current_parts.append(para)
        current_len += para_len
        char_cursor += para_len

    if current_parts:
        _flush()

    total = len(chunks)
    for c in chunks:
        c["total_chunks"] = total

    return chunks


# ---------------------------------------------------------------------------
# Per-chunk extraction prompt - deliberately document-type agnostic
# ---------------------------------------------------------------------------

_CHUNK_SYSTEM = """\
You are a senior project document analyst. You are reading ONE CHUNK of a
single project document. You do NOT know what other documents exist, and you
must not speculate about them.

Your job is to record, faithfully and literally, what THIS chunk asserts.

Return a JSON object with exactly these keys:

{
  "requirements": [
    {
      "topic": "<short subject, e.g. 'Concrete grade - columns'>",
      "statement": "<what the document requires or states, in its own terms>",
      "value": "<the specific value/grade/rating/date if one is given, else null>",
      "reference": "<clause, section, item code, sheet or page shown in the text, else null>",
      "discipline": "<civil|structural|architectural|mechanical|electrical|plumbing|contractual|general>"
    }
  ],
  "quantities": [
    {
      "description": "<what is being measured or priced>",
      "amount": <number or null>,
      "unit": "<unit string or null>",
      "reference": "<item code / section, else null>"
    }
  ],
  "standards": ["<ASTM / BS / EN / ISO / ECP reference exactly as written>"],
  "entities": ["<named element, system, zone, building or party>"],
  "notes": "<anything structurally important about this chunk>"
}

Rules:
- Return ONLY a valid JSON object. No markdown fences, no preamble.
- Use [] or null for anything absent from this chunk.
- Never invent, infer, or normalise values. Quote what is written.
- Preserve every clause/section/item reference you see - later stages depend
  on being able to cite exactly where a statement came from.
"""


def _extract_chunk(chunk: Dict[str, Any], doc_label: str) -> Dict[str, Any]:
    """Send one chunk to the LLM and return its parsed result dict."""
    idx = chunk["chunk_index"]
    total = chunk["total_chunks"]

    user_prompt = (
        f"DOCUMENT: {doc_label}\n"
        f"[Chunk {idx + 1} of {total}  |  chars {chunk['char_start']}-{chunk['char_end']}]\n\n"
        f"{chunk['text']}"
    )

    try:
        result = _get_llm().run(
            system=_CHUNK_SYSTEM,
            user=user_prompt,
            temperature=0.1,
        )
        return {
            "requirements": result.get("requirements") or [],
            "quantities":   result.get("quantities") or [],
            "standards":    result.get("standards") or [],
            "entities":     result.get("entities") or [],
            "notes":        result.get("notes") or "",
        }
    except Exception as exc:
        print(f"[document_analysis] {doc_label} chunk {idx} LLM call failed: {exc}")
        return {
            "requirements": [],
            "quantities": [],
            "standards": [],
            "entities": [],
            "notes": f"LLM error on chunk {idx}: {exc}",
        }


# ---------------------------------------------------------------------------
# Merge per-chunk results into one document profile
# ---------------------------------------------------------------------------

def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _merge_chunk_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Merge per-chunk extraction results for a SINGLE document.

    Requirements are de-duplicated on (topic, statement); a later chunk may
    fill in a missing value or reference on an entry seen earlier.
    Quantities de-duplicate on (description, unit). Standards and entities
    become unique ordered lists.
    """
    requirements: List[Dict] = []
    seen_reqs: Dict[str, int] = {}

    quantities: List[Dict] = []
    seen_qtys: Dict[str, int] = {}

    standards: List[str] = []
    entities: List[str] = []
    notes: List[str] = []

    for chunk_result in results:
        for req in chunk_result.get("requirements", []):
            if not isinstance(req, dict):
                continue
            key = _norm(req.get("topic")) + "|" + _norm(req.get("statement"))
            if key == "|":
                continue
            if key in seen_reqs:
                existing = requirements[seen_reqs[key]]
                if req.get("value") and not existing.get("value"):
                    existing["value"] = req["value"]
                if req.get("reference") and not existing.get("reference"):
                    existing["reference"] = req["reference"]
            else:
                seen_reqs[key] = len(requirements)
                requirements.append(dict(req))

        for qty in chunk_result.get("quantities", []):
            if not isinstance(qty, dict):
                continue
            key = _norm(qty.get("description")) + "|" + _norm(qty.get("unit"))
            if key == "|":
                continue
            if key in seen_qtys:
                existing = quantities[seen_qtys[key]]
                if qty.get("amount") is not None and existing.get("amount") is None:
                    existing["amount"] = qty["amount"]
                if qty.get("reference") and not existing.get("reference"):
                    existing["reference"] = qty["reference"]
            else:
                seen_qtys[key] = len(quantities)
                quantities.append(dict(qty))

        for std in chunk_result.get("standards", []):
            if std and std not in standards:
                standards.append(std)

        for ent in chunk_result.get("entities", []):
            if ent and ent not in entities:
                entities.append(ent)

        note = str(chunk_result.get("notes") or "").strip()
        if note:
            notes.append(note)

    return {
        "requirements": requirements,
        "quantities":   quantities,
        "standards":    standards,
        "entities":     entities,
        "internal_notes": " | ".join(notes),
    }


# ---------------------------------------------------------------------------
# Regex fallback - used only when the LLM yields nothing at all for a document
# ---------------------------------------------------------------------------

_STANDARD_PATTERNS = [
    r"astm\s+[a-z]?\s*\d+",
    r"bs\s+(?:en\s+)?\d+",
    r"en\s+\d+",
    r"ecp\s+\d+",
    r"iso\s+\d+",
    r"aci\s+\d+",
    r"din\s+\d+",
]


def _fallback_profile(text: str) -> Dict[str, Any]:
    """
    Minimal regex extraction so the pipeline still produces something citable
    when the LLM is unavailable or returns empty results for a document.
    """
    lowered = text.lower()

    standards: List[str] = []
    for pat in _STANDARD_PATTERNS:
        for match in re.findall(pat, lowered, re.IGNORECASE):
            token = " ".join(match.upper().split())
            if token not in standards:
                standards.append(token)

    requirements: List[Dict[str, Any]] = []
    # Lines shaped like "Something: value" are the most common requirement form
    for line in text.splitlines():
        line = line.strip()
        if 5 < len(line) < 300 and ":" in line:
            topic, value = line.split(":", 1)
            topic, value = topic.strip(), value.strip()
            if topic and value:
                requirements.append({
                    "topic": topic[:120],
                    "statement": line[:300],
                    "value": value[:120],
                    "reference": None,
                    "discipline": "general",
                })
        if len(requirements) >= 200:   # keep the fallback bounded
            break

    quantities: List[Dict[str, Any]] = []
    qty_pattern = r"([A-Za-z][A-Za-z /\-]{3,60}?)\s+(\d[\d,\.]*)\s*(m3|m2|kg|ton|lm|ea|no\.?)\b"
    for m in re.finditer(qty_pattern, text, re.IGNORECASE):
        try:
            amount = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
        quantities.append({
            "description": m.group(1).strip(),
            "amount": amount,
            "unit": m.group(3),
            "reference": None,
        })
        if len(quantities) >= 200:
            break

    return {
        "requirements": requirements,
        "quantities": quantities,
        "standards": standards,
        "entities": [],
        "internal_notes": "Regex fallback extraction used (LLM returned no data).",
    }


# ---------------------------------------------------------------------------
# Profile one document
# ---------------------------------------------------------------------------

def build_document_profile(text: str, label: str, slot: str, source_name: str) -> Dict[str, Any]:
    """
    Chunk *text*, run every chunk through the LLM, and merge the results into
    a single profile dict matching state.DocumentProfile.
    """
    text = (text or "").strip()
    if not text:
        return {
            "slot": slot,
            "label": label,
            "source_name": source_name,
            "char_count": 0,
            "chunk_count": 0,
            "requirements": [],
            "quantities": [],
            "standards": [],
            "entities": [],
            "internal_notes": "No text supplied for this document.",
        }

    chunks = chunk_document(text)
    print(
        f"[document_analysis] '{label}': {len(text):,} chars -> {len(chunks)} chunk(s) "
        f"(size={CHUNK_SIZE:,}, overlap={CHUNK_OVERLAP})"
    )

    chunk_results: List[Dict[str, Any]] = []
    for chunk in chunks:
        idx = chunk["chunk_index"]
        print(
            f"[document_analysis]   -> '{label}' chunk {idx + 1}/{len(chunks)} "
            f"(chars {chunk['char_start']:,}-{chunk['char_end']:,})"
        )
        result = _extract_chunk(chunk, label)
        chunk_results.append(result)
        print(
            f"[document_analysis]      {len(result['requirements'])} requirement(s), "
            f"{len(result['quantities'])} quantity item(s)"
        )

    merged = _merge_chunk_results(chunk_results)

    if not merged["requirements"] and not merged["quantities"]:
        print(f"[document_analysis] '{label}': LLM returned nothing - using regex fallback")
        merged = _fallback_profile(text)

    return {
        "slot": slot,
        "label": label,
        "source_name": source_name,
        "char_count": len(text),
        "chunk_count": len(chunks),
        **merged,
    }


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

def analyze_documents(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph node: analyze_documents

    Consumes
    --------
    document_a_text / document_a_label / document_a_source
    document_b_text / document_b_label / document_b_source

    Produces
    --------
    profile_a, profile_b   : independent structured profiles
    documents_analyzed     : True
    analysis_warnings      : appended when a document is empty or thin
    step_history           : one entry
    """
    print("\n[analyze_documents] === Profiling both documents ===")

    label_a = state.get("document_a_label") or "Document A"
    label_b = state.get("document_b_label") or "Document B"

    profile_a = build_document_profile(
        state.get("document_a_text") or "",
        label_a,
        "a",
        state.get("document_a_source") or "pasted text",
    )
    profile_b = build_document_profile(
        state.get("document_b_text") or "",
        label_b,
        "b",
        state.get("document_b_source") or "pasted text",
    )

    warnings: List[str] = []
    for prof in (profile_a, profile_b):
        if prof["char_count"] == 0:
            warnings.append(
                f"'{prof['label']}' contained no readable text - nothing could be compared against it."
            )
        elif not prof["requirements"] and not prof["quantities"]:
            warnings.append(
                f"No requirements or quantities could be extracted from '{prof['label']}'. "
                f"The file may be a scanned image without a text layer."
            )

    print(
        f"[analyze_documents] '{label_a}': {len(profile_a['requirements'])} requirements, "
        f"{len(profile_a['quantities'])} quantities, {len(profile_a['standards'])} standards"
    )
    print(
        f"[analyze_documents] '{label_b}': {len(profile_b['requirements'])} requirements, "
        f"{len(profile_b['quantities'])} quantities, {len(profile_b['standards'])} standards"
    )
    print("[analyze_documents] === Profiling complete ===\n")

    return {
        "profile_a": profile_a,
        "profile_b": profile_b,
        "documents_analyzed": True,
        "analysis_warnings": warnings,
        "step_history": [
            f"analyze_documents: '{label_a}' {profile_a['chunk_count']} chunk(s), "
            f"'{label_b}' {profile_b['chunk_count']} chunk(s)"
        ],
    }
