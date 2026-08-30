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
from typing import Dict, Any, List, Optional

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


def chunk_document(text: str, offset: int = 0) -> List[Dict[str, Any]]:
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

    *offset* is added to every char_start/char_end so that chunking a
    mid-document slice (e.g. one section pulled out by review-focus
    sectioning) still reports the slice's TRUE position in the original
    document - needed for citation accuracy downstream.

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
                "char_start": offset + chunk_start,
                "char_end": offset + char_end,
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
# Review-focus section jump - when the engineer names a topic, locate it via
# the document's OWN headings/section titles and only chunk those sections,
# instead of reading the whole document to decide what's relevant. This
# matters most on large specification documents (hundreds of chunks) where
# running every chunk through the full extraction prompt would take minutes
# and mostly profile material the engineer explicitly said they don't care
# about.
# ---------------------------------------------------------------------------

MIN_HEADINGS_FOR_SECTIONING = 3   # fewer than this = "no clear structure"
HEADING_REPEAT_LIMIT = 3          # a title repeated more than this = running header/footer noise

# Sections that apply across every trade/topic and are always worth keeping
# alongside whatever the review focus matches - a narrow, documented exception
# to the document-agnostic extraction principle, not a return to scanning
# body text: only heading TITLES are checked against this list.
_ALWAYS_RELEVANT_TITLE_PATTERNS = [
    r"general\s+requirements", r"general\s+conditions", r"general\s+notes",
    r"preliminar(?:y|ies)", r"scope\s+of\s+work", r"summary\s+of\s+work",
]

_HEADING_PATTERNS = [
    # level 0: SECTION/DIVISION/PART lines and numbered section codes
    (0, re.compile(
        r"^\s*(?:SECTION|DIVISION|PART)\s+[\dIVXLC]+\b.*$"
        r"|^\s*\d{2}\s?\d{2}\s?\d{2}\b.*$",
        re.MULTILINE | re.IGNORECASE,
    )),
    # level 1: numbered clauses, e.g. "1.2 Masonry Units", "4.2.1 Grout"
    (1, re.compile(
        r"^\s*\d{1,2}(?:\.\d{1,2}){1,3}\s+[A-Za-z][A-Za-z0-9 /,&'()\-]{2,80}$",
        re.MULTILINE,
    )),
    # level 2: standalone ALL-CAPS or Title Case short lines, no end punctuation
    (2, re.compile(
        r"^\s*(?=.{4,80}$)[A-Z][A-Za-z0-9 /,&'()\-]*[A-Za-z0-9)]$",
        re.MULTILINE,
    )),
]


def _detect_headings(text: str) -> List[Dict[str, Any]]:
    """
    Regex/LLM-free outline scan of *text*. Returns headings in document
    order: [{"title": str, "char_start": int, "level": int}, ...].

    Stays document-format-agnostic - not tied to any one numbering
    convention. Two noise filters keep this usable on real PDF-extracted
    text: pure-numeric/very-short lines are rejected outright, and any exact
    title that recurs more than HEADING_REPEAT_LIMIT times (a running header
    or footer repeated on every page) is kept only at its first occurrence.
    """
    if not text:
        return []

    candidates: List[Dict[str, Any]] = []
    seen_spans = set()

    for level, pattern in _HEADING_PATTERNS:
        for m in pattern.finditer(text):
            line = m.group(0).strip()
            if m.start() in seen_spans:
                continue
            if len(line) < 4 or line.isdigit():
                continue
            seen_spans.add(m.start())
            candidates.append({"title": line, "char_start": m.start(), "level": level})

    candidates.sort(key=lambda h: h["char_start"])

    title_counts: Dict[str, int] = {}
    for h in candidates:
        norm = re.sub(r"\s+", " ", h["title"]).strip().lower()
        title_counts[norm] = title_counts.get(norm, 0) + 1

    headings: List[Dict[str, Any]] = []
    seen_repeats: Dict[str, int] = {}
    for h in candidates:
        norm = re.sub(r"\s+", " ", h["title"]).strip().lower()
        if title_counts[norm] > HEADING_REPEAT_LIMIT:
            seen_repeats[norm] = seen_repeats.get(norm, 0) + 1
            if seen_repeats[norm] > 1:
                continue   # drop every occurrence after the first - running header/footer noise
        headings.append(h)

    return headings


def _select_relevant_headings(
    headings: List[Dict[str, Any]], review_focus: str
) -> Optional[List[int]]:
    """
    ONE LLM call: sends only the numbered heading titles (never body text)
    plus *review_focus*, asks which are relevant. Returns indices into
    *headings*. Returns None (distinct from []) on call/parse failure, so
    the caller can tell "nothing is relevant" apart from "the call broke."

    Always additionally selects headings that look like general/preliminary
    sections (see _ALWAYS_RELEVANT_TITLE_PATTERNS), regardless of what the
    LLM returns, since cross-cutting requirements often live there.
    """
    numbered = "\n".join(f"{i}: {h['title']}" for i, h in enumerate(headings))
    try:
        result = _get_llm().run(
            system=(
                "You are a construction document analyst. You are given the "
                "outline (section/clause headings only, no body text) of a "
                "construction document, and a topic an engineer wants the "
                "scan focused on. Identify which headings are relevant to "
                "that topic - i.e. their section is likely to discuss it."
            ),
            user=(
                f"Topic: {review_focus}\n\n"
                f"Headings:\n{numbered}\n\n"
                'Return JSON: {"relevant_indices": [0, 4, 7, ...]}\n'
                "Only the integer indices shown before each heading. "
                "Empty list if none are relevant. No explanations."
            ),
            temperature=0.1,
        )
    except Exception as exc:
        print(f"[document_analysis] Heading relevance call failed: {exc}")
        return None

    raw = result.get("relevant_indices")
    if raw is None:
        print("[document_analysis] Heading relevance call returned no usable result")
        return None

    llm_idx = {int(i) for i in raw if isinstance(i, (int, float, str)) and str(i).strip().lstrip("-").isdigit()}
    llm_idx = {i for i in llm_idx if 0 <= i < len(headings)}

    always_idx = {
        i for i, h in enumerate(headings)
        if any(re.search(p, h["title"], re.IGNORECASE) for p in _ALWAYS_RELEVANT_TITLE_PATTERNS)
    }

    return sorted(llm_idx | always_idx)


def _build_sections(
    text: str, headings: List[Dict[str, Any]], selected_idx: List[int]
) -> List[Dict[str, Any]]:
    """
    For each selected heading, slice its span - from its char_start to the
    next heading with level <= its own level, or end of document - then
    sort and merge overlapping/adjacent spans (e.g. a parent SECTION and one
    of its own nested clauses both selected) into single, non-duplicated
    sections.

    Returns [{"char_start": int, "char_end": int, "text": str}, ...].
    """
    spans: List[List[int]] = []
    for idx in selected_idx:
        h = headings[idx]
        end = len(text)
        for other in headings[idx + 1:]:
            if other["level"] <= h["level"]:
                end = other["char_start"]
                break
        spans.append([h["char_start"], end])

    spans.sort()
    merged: List[List[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return [
        {"char_start": start, "char_end": end, "text": text[start:end]}
        for start, end in merged
    ]


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

def build_document_profile(
    text: str, label: str, slot: str, source_name: str,
    review_focus: str = "",
) -> Dict[str, Any]:
    """
    Chunk *text*, run every chunk through the LLM, and merge the results into
    a single profile dict matching state.DocumentProfile.

    When *review_focus* is given, only the section(s) of the document whose
    heading is judged relevant to that topic are chunked and scanned - see
    _detect_headings / _select_relevant_headings / _build_sections. This
    never touches the rest of the document's body text at all; it navigates
    by the document's own structure rather than scanning everything.

    Invariant: whenever focus_status != "sectioned", the full document is
    chunked and scanned (chunk_count == total_chunk_count) - a bad/absent
    heading structure or a failed relevance call can never cause content to
    be silently dropped.
    """
    text = (text or "").strip()
    if not text:
        return {
            "slot": slot,
            "label": label,
            "source_name": source_name,
            "char_count": 0,
            "chunk_count": 0,
            "total_chunk_count": 0,
            "focus_status": "no_focus",
            "requirements": [],
            "quantities": [],
            "standards": [],
            "entities": [],
            "internal_notes": "No text supplied for this document.",
        }

    all_chunks = chunk_document(text)
    review_focus = (review_focus or "").strip()

    if not review_focus:
        chunks, focus_status = all_chunks, "no_focus"
    else:
        headings = _detect_headings(text)
        if len(headings) < MIN_HEADINGS_FOR_SECTIONING:
            print(
                f"[document_analysis] '{label}': only {len(headings)} heading(s) detected - "
                f"no clear section structure, scanning the full document instead"
            )
            chunks, focus_status = all_chunks, "no_headings_found"
        else:
            selected = _select_relevant_headings(headings, review_focus)
            if selected is None:
                print(f"[document_analysis] '{label}': section relevance check failed - scanning the full document instead")
                chunks, focus_status = all_chunks, "selection_failed"
            elif not selected:
                print(f"[document_analysis] '{label}': review focus matched no sections - scanning the full document instead")
                chunks, focus_status = all_chunks, "no_relevant_headings"
            else:
                sections = _build_sections(text, headings, selected)
                chunks = []
                for sec in sections:
                    chunks.extend(chunk_document(sec["text"], offset=sec["char_start"]))
                for i, c in enumerate(chunks):
                    c["chunk_index"] = i
                for c in chunks:
                    c["total_chunks"] = len(chunks)
                focus_status = "sectioned"
                print(
                    f"[document_analysis] '{label}': review focus matched {len(selected)}/{len(headings)} "
                    f"heading(s) -> {len(sections)} section(s), {len(chunks)}/{len(all_chunks)} chunk(s) selected"
                )

    print(
        f"[document_analysis] '{label}': {len(text):,} chars -> {len(all_chunks)} chunk(s) total "
        f"(size={CHUNK_SIZE:,}, overlap={CHUNK_OVERLAP}), scanning {len(chunks)}"
    )

    chunk_results: List[Dict[str, Any]] = []
    for scan_pos, chunk in enumerate(chunks, start=1):
        print(
            f"[document_analysis]   -> '{label}' scanning {scan_pos}/{len(chunks)} "
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
        "total_chunk_count": len(all_chunks),
        "focus_status": focus_status,
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
    review_focus = (state.get("review_focus") or "").strip()

    profile_a = build_document_profile(
        state.get("document_a_text") or "",
        label_a,
        "a",
        state.get("document_a_source") or "pasted text",
        review_focus,
    )
    profile_b = build_document_profile(
        state.get("document_b_text") or "",
        label_b,
        "b",
        state.get("document_b_source") or "pasted text",
        review_focus,
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

        status = prof.get("focus_status")
        if status == "sectioned":
            warnings.append(
                f"Review focus '{review_focus}' narrowed '{prof['label']}' to "
                f"{prof['chunk_count']}/{prof['total_chunk_count']} chunk(s) before analysis. "
                f"Content outside these sections was not scanned."
            )
        elif status == "no_headings_found":
            warnings.append(
                f"Review focus '{review_focus}' requested for '{prof['label']}', but no clear "
                f"section structure was detected - the full document was scanned instead."
            )
        elif status == "no_relevant_headings":
            warnings.append(
                f"Review focus '{review_focus}' matched no sections in '{prof['label']}' - "
                f"the full document was scanned instead."
            )
        elif status == "selection_failed":
            warnings.append(
                f"Could not evaluate section relevance for '{prof['label']}' (LLM error) - "
                f"the full document was scanned instead."
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
