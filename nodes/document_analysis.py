"""
Document Analysis Node - ConflictScan / RFI Automation
======================================================
Builds a structured profile of EACH of the two document sets: every file of a
set is read and profiled on its own (scanned pages OCR-read via
nodes/file_ingest.py, drawings via nodes/drawing_analysis.py), then merged
into one set profile in which every item keeps the file it came from.

Design principles
-----------------
* Document-agnostic: the node knows nothing about BOQs, specifications, or
  material lists. It reads whatever it is given and extracts the assertions
  that document makes.
* Full coverage: every document is chunked and EVERY chunk is sent to the LLM.
  Relevant statements can sit on non-contiguous pages, so nothing is sampled
  or discarded.
* Text documents are profiled in isolation. Cross-document comparison happens
  later, in nodes/conflict_scan.py - keeping extraction free of any bias about
  what the "other" document says. The exception is a drawing: it is read
  against a baseline checklist built from the other document (what to look
  for, never the required values) - see nodes/drawing_analysis.py.

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
    # The extractor role of the AI fleet (config/fleet.toml).
    from fleet import llm
    return llm("extractor")


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
You are a senior multi-discipline project document analyst (civil, structural,
architectural, mechanical, electrical, contracts). You are reading ONE CHUNK of
a single project document - a BOQ, specification, schedule, report, letter or
instruction. You do NOT know what other documents exist, and you must not
speculate about them.

Your job is to record, faithfully and literally, what THIS chunk asserts.

The document may be bilingual (Arabic and English) and may have been
transcribed from a scan by OCR. Read both languages. Write "topic" and
"statement" in English; copy values, item numbers, codes and references
exactly as written (Arabic item numbers included).

Return a JSON object with exactly these keys:

{
  "requirements": [
    {
      "topic": "<short subject, e.g. 'Lighting pole - height'>",
      "statement": "<what the document requires or states, in its own terms>",
      "quote": "<the exact words in this chunk that state it, copied character for character, at most 40 words>",
      "value": "<the specific value/rating/size/type/date if one is given, else null>",
      "reference": "<clause / section / BOQ item no. / sheet, plus 'p. N' from the nearest preceding [Page N] marker, else null>",
      "discipline": "<civil|structural|architectural|mechanical|electrical|plumbing|contractual|general>",
      "element_id": "<tag, mark or name identifying the element or system (equipment tag, panel, room, member mark), else null>",
      "location": "<building / block / zone / floor / room the statement applies to, as written, else null>",
      "open_item": <true when the document itself leaves this open: TBD, to be confirmed, by others, hold, refer to another package, blank quantity - else false>
    }
  ],
  "quantities": [
    {
      "description": "<what is being measured or priced>",
      "amount": <number or null>,
      "unit": "<unit string or null>",
      "reference": "<item code / section, plus 'p. N', else null>",
      "location": "<building / zone the quantity applies to, else null>"
    }
  ],
  "standards": ["<ASTM / BS / EN / ISO / NFPA / ECP / utility-company reference exactly as written>"],
  "entities": ["<named element, system, zone, building or party>"],
  "notes": "<anything important about this chunk>"
}

Rules:
- Return ONLY a valid JSON object. No markdown fences, no preamble.
- Use [] or null for anything absent from this chunk.
- Never invent, infer, or normalise values. Quote what is written.
- A BOQ line is one requirement (its full description as the statement, its
  item number as the reference) plus one quantity entry.
- Preserve every clause/section/item reference you see - later stages depend
  on being able to cite exactly where a statement came from.
"""


MAX_QUOTE_CHARS = 300


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def verbatim_quote(req: Dict[str, Any], source_text: str) -> str:
    """
    The document's own words for a statement, guaranteed to occur in the source.

    The model's "quote" is kept only when it really appears in the chunk
    (whitespace-insensitive). Otherwise the source line that best matches the
    statement - and contains its value, when there is one - is used. '' when
    nothing in the source supports the statement.
    """
    source = _squash(source_text)
    if not source:
        return ""
    quote = _squash(req.get("quote"))
    if quote and len(quote) >= 4 and quote.lower() in source.lower():
        start = source.lower().index(quote.lower())
        return source[start:start + len(quote)][:MAX_QUOTE_CHARS]

    value = _squash(req.get("value")).lower()
    want = set(re.findall(r"[a-z0-9]{3,}", f"{req.get('topic')} {req.get('statement')} {value}".lower()))
    best, best_score = "", 0
    for line in re.split(r"\n|(?<=[.;])\s+", str(source_text)):
        line = _squash(line)
        if len(line) < 4 or re.fullmatch(r"\[Page \d+\]", line):
            continue
        if value and value not in line.lower():
            continue
        score = len(want & set(re.findall(r"[a-z0-9]{3,}", line.lower())))
        if score > best_score:
            best, best_score = line, score
    return best[:MAX_QUOTE_CHARS] if best_score >= 2 or (value and best) else ""


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
        requirements = [r for r in (result.get("requirements") or []) if isinstance(r, dict)]
        for req in requirements:
            req["quote"] = verbatim_quote(req, chunk["text"])
        return {
            "requirements": requirements,
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
                for field in ("value", "reference", "element_id", "location", "grid", "section_ref", "quote"):
                    if req.get(field) and not existing.get(field):
                        existing[field] = req[field]
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

    from fleet import pmap

    def _scan(args):
        scan_pos, chunk = args
        print(
            f"[document_analysis]   -> '{label}' scanning {scan_pos}/{len(chunks)} "
            f"(chars {chunk['char_start']:,}-{chunk['char_end']:,})"
        )
        return _extract_chunk(chunk, label)

    chunk_results: List[Dict[str, Any]] = pmap(_scan, list(enumerate(chunks, start=1)), role="extractor")
    for result in chunk_results:
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
# Document sets - every file on a side is profiled on its own, then merged
# into one set profile. Text documents go through the chunking pipeline
# above; drawings are rendered/tiled/vision-read (nodes/drawing_analysis.py).
# Both return the same profile shape, and every statement keeps the file it
# came from, so a finding can cite "file | sheet/page | clause" across a set
# of many attachments.
# ---------------------------------------------------------------------------

_PAGE_REF = re.compile(r"\bp(?:age)?\.?\s*(\d+)\b", re.IGNORECASE)


def _tag_items(profile: Dict[str, Any], slot: str, file_name: str, file_index: int) -> None:
    """Stamp every item with its side, source file and a stable id (baseline links use it)."""
    for i, req in enumerate(profile.get("requirements") or []):
        req.setdefault("source_file", file_name)
        req["side"] = slot
        req.setdefault("uid", f"{slot}:{file_index}:r{i}")
    for i, qty in enumerate(profile.get("quantities") or []):
        qty.setdefault("source_file", file_name)
        qty["side"] = slot
        qty.setdefault("uid", f"{slot}:{file_index}:q{i}")


def _mark_ocr_items(profile: Dict[str, Any], ocr_pages: List[int], total_pages: int) -> None:
    """Items read from OCR'd pages are flagged low-confidence - quoted values may carry OCR errors."""
    if not ocr_pages:
        return
    ocr = set(ocr_pages)
    mostly_ocr = total_pages and len(ocr) >= total_pages / 2
    for item in (profile.get("requirements") or []) + (profile.get("quantities") or []):
        m = _PAGE_REF.search(str(item.get("reference") or ""))
        if (m and int(m.group(1)) in ocr) or (not m and mostly_ocr):
            item["low_confidence"] = True
            item["text_source"] = "ocr"


DUPLICATE_SIMILARITY = 0.85      # share of 5-word phrases two documents have in common to be one document


def _shingles(text: str) -> set:
    words = re.findall(r"\w+", (text or "").lower())
    return {" ".join(words[i:i + 5]) for i in range(max(0, len(words) - 4))}


def _drop_duplicate_documents(jobs):
    """
    The same document uploaded twice in one set (e.g. a specification as .docx
    and as .pdf) is read once: reading it twice doubles every clause, and two
    readings of one text never extract identically. The PDF copy is kept when
    there is one (its page numbers make references citable).
    Returns (jobs to profile, [(slot, skipped entry, name of the copy kept)]).
    """
    from nodes.file_ingest import read_document
    for _slot, _idx, entry in jobs:
        if entry.get("kind") != "text" and entry.get("path") and "_doc" not in entry:
            try:
                entry["_doc"] = read_document(entry["path"])
            except Exception as exc:  # noqa: BLE001 - profiled (and reported) as usual
                print(f"[analyze_documents] could not pre-read {entry.get('name')}: {exc}")
    def text_of(entry):
        return entry.get("text") if entry.get("kind") == "text" else (entry.get("_doc") or {}).get("text", "")
    ordered = sorted(jobs, key=lambda j: (j[0], 0 if str(j[2].get("name", "")).lower().endswith(".pdf") else 1, j[1]))
    kept, dropped, seen = [], [], []
    for slot, idx, entry in ordered:
        sh = _shingles(text_of(entry))
        twin = next((name for s2, name, other in seen if s2 == slot and sh and other
                     and len(sh & other) / max(1, min(len(sh), len(other))) >= DUPLICATE_SIMILARITY), None)
        if twin:
            print(f"[analyze_documents] '{entry.get('name')}' duplicates '{twin}' - read once")
            entry.pop("_doc", None)
            dropped.append((slot, entry, twin))
            continue
        seen.append((slot, entry.get("name") or "document", sh))
        kept.append((slot, idx, entry))
    kept.sort(key=lambda j: (j[0], j[1]))
    return kept, dropped


def _profile_text_file(entry: Dict[str, Any], slot: str, label: str, review_focus: str):
    name = entry.get("name") or "document"
    ocr_pages: List[int] = []
    pages = 0
    file_warnings: List[str] = []
    if entry.get("kind") == "text":
        text = entry.get("text") or ""
    else:
        doc = entry.pop("_doc", None)       # read once by the duplicate check
        if doc is None:
            from nodes.file_ingest import read_document
            doc = read_document(entry["path"])
        text, ocr_pages, pages, file_warnings = doc["text"], doc["ocr_pages"], doc["pages"], doc["warnings"]

    profile = build_document_profile(text, label, slot, name, review_focus)
    _mark_ocr_items(profile, ocr_pages, pages)
    from nodes.scope import assign_document_scope
    assign_document_scope(profile, text)          # which structure each clause is about (its headings)
    summary = {
        "name": name,
        "kind": "text" if entry.get("kind") == "text" else "document",
        "classified_as": entry.get("classified_as", ""),
        "role": entry.get("role", ""),
        "chars": profile["char_count"],
        "chunks": profile["chunk_count"],
        "total_chunks": profile["total_chunk_count"],
        "focus_status": profile["focus_status"],
        "pages": pages,
        "ocr_pages": len(ocr_pages),
        "requirements": len(profile["requirements"]),
        "quantities": len(profile["quantities"]),
        "warnings": file_warnings,
    }
    return profile, summary


def _profile_drawing_file(entry: Dict[str, Any], slot: str, label: str, review_focus: str,
                          baseline: Optional[Dict[str, Any]]):
    from nodes.drawing_analysis import build_drawing_profile

    name = entry.get("name") or "drawing"
    profile = build_drawing_profile(
        entry.get("path"), entry.get("dxf_path"), label, slot, name, review_focus, baseline,
    )
    summary = {
        "name": name,
        "kind": "drawing",
        "classified_as": entry.get("classified_as", ""),
        "role": entry.get("role", ""),
        "sheets": len(profile.get("sheets") or []),
        "tiles": profile.get("chunk_count", 0),
        "cut_off_tiles": profile.get("cut_off_tiles", 0),
        "failed_tiles": profile.get("failed_tiles", 0),
        "requirements": len(profile.get("requirements") or []),
        "quantities": len(profile.get("quantities") or []),
        "warnings": list(profile.get("warnings") or []),
    }
    return profile, summary


def _merge_set(slot: str, label: str, parts: List[Dict[str, Any]], summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Concatenate per-file profiles into one set profile (items keep their source_file)."""
    requirements: List[Dict[str, Any]] = []
    quantities: List[Dict[str, Any]] = []
    standards: List[str] = []
    entities: List[str] = []
    notes: List[str] = []
    for part in parts:
        requirements.extend(part.get("requirements") or [])
        quantities.extend(part.get("quantities") or [])
        for std in part.get("standards") or []:
            if std and std not in standards:
                standards.append(std)
        for ent in part.get("entities") or []:
            if ent and ent not in entities:
                entities.append(ent)
        if part.get("internal_notes"):
            notes.append(f"[{part.get('source_name')}] {part['internal_notes']}")

    statuses = [p.get("focus_status") for p in parts if not p.get("drawing_inputs")]
    focus_status = "sectioned" if "sectioned" in statuses else next((s for s in statuses if s != "no_focus"), "no_focus")

    merged: Dict[str, Any] = {
        "slot": slot,
        "label": label,
        "source_name": " + ".join(str(p.get("source_name") or "") for p in parts),
        "char_count": sum(p.get("char_count", 0) for p in parts),
        "chunk_count": sum(p.get("chunk_count", 0) for p in parts),
        "total_chunk_count": sum(p.get("total_chunk_count", 0) for p in parts),
        "focus_status": focus_status,
        "requirements": requirements,
        "quantities": quantities,
        "standards": standards,
        "entities": entities,
        "internal_notes": " | ".join(notes),
        "files": summaries,
    }

    drawings = [p for p in parts if p.get("drawing_inputs")]
    if drawings:
        merged.update({
            "drawing_inputs": {
                "pdf": any(p["drawing_inputs"].get("pdf") for p in drawings),
                "dxf": any(p["drawing_inputs"].get("dxf") for p in drawings),
                "visual_source": drawings[0]["drawing_inputs"].get("visual_source"),
                "drawing_files": len(drawings),
            },
            "sheets": [
                {**sheet, "file": p.get("source_name")} for p in drawings for sheet in (p.get("sheets") or [])
            ],
            "baseline": next((p.get("baseline") for p in drawings if p.get("baseline")), None),
            "baseline_coverage": [c for p in drawings for c in (p.get("baseline_coverage") or [])],
            "dxf_summary": next((p.get("dxf_summary") for p in drawings if p.get("dxf_summary")), None),
            "warnings": [w for p in drawings for w in (p.get("warnings") or [])],
        })
    return merged


# ---------------------------------------------------------------------------
# LangGraph node
# ---------------------------------------------------------------------------

def analyze_documents(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph node: analyze_documents

    Consumes
    --------
    document_a_files / document_b_files : the two sets (see state.SourceFile)
    document_a_label / document_b_label, review_focus

    Order
    -----
    1. Every text document of both sets is read (OCR for scanned pages) and
       profiled independently.
    2. Drawings are read next, each set's drawings against ONE baseline built
       from the other set's text documents - or, when the other set is
       drawings only and was already read, from that set.

    Produces
    --------
    profile_a, profile_b   : merged set profiles; every item carries its source_file
    documents_analyzed     : True
    analysis_warnings      : unreadable files, OCR use, thin extraction, focus narrowing
    step_history           : one entry
    """
    print("\n[analyze_documents] === Profiling both document sets ===")

    labels = {"a": state.get("document_a_label") or "Document A", "b": state.get("document_b_label") or "Document B"}
    review_focus = (state.get("review_focus") or "").strip()
    files = {slot: list(state.get(f"document_{slot}_files") or []) for slot in ("a", "b")}

    parts: Dict[str, List[Dict[str, Any]]] = {"a": [], "b": []}
    summaries: Dict[str, List[Dict[str, Any]]] = {"a": [], "b": []}

    # 1. Text documents (and pasted text) of both sets
    # Files are profiled side by side when every model stays loaded (AI fleet "resident"
    # mode); on a single GPU ("staged") one file at a time avoids swapping models.
    from fleet import pmap, profile as fleet_profile
    text_jobs = [(slot, idx, entry) for slot in ("a", "b") for idx, entry in enumerate(files[slot])
                 if entry.get("kind") != "drawing"]
    text_jobs, duplicates = _drop_duplicate_documents(text_jobs)
    for slot, entry, original in duplicates:
        summaries[slot].append({
            "name": entry.get("name") or "document", "kind": "document", "classified_as": entry.get("classified_as", ""),
            "role": entry.get("role", ""), "chars": 0, "chunks": 0, "total_chunks": 0, "focus_status": "no_focus",
            "pages": 0, "ocr_pages": 0, "requirements": 0, "quantities": 0, "duplicate_of": original,
            "warnings": [f"'{entry.get('name')}' has the same content as '{original}' - it was read once."],
        })

    def _profile_job(job):
        slot, idx, entry = job
        print(f"[analyze_documents] '{labels[slot]}' file {idx + 1}/{len(files[slot])}: {entry.get('name')} (document)")
        return _profile_text_file(entry, slot, labels[slot], review_focus)

    workers = 4 if fleet_profile.active()["mode"] == "resident" else 1
    for (slot, idx, entry), (profile, summary) in zip(text_jobs, pmap(_profile_job, text_jobs, workers=workers)):
        _tag_items(profile, slot, entry.get("name") or "document", idx)
        parts[slot].append(profile)
        summaries[slot].append(summary)

    text_profiles = {slot: _merge_set(slot, labels[slot], parts[slot], summaries[slot]) for slot in ("a", "b")}
    final: Dict[str, Dict[str, Any]] = {}

    # 2. Drawings, each set against a baseline from the other
    for slot in ("a", "b"):
        other = "b" if slot == "a" else "a"
        drawing_entries = [(i, e) for i, e in enumerate(files[slot]) if e.get("kind") == "drawing"]
        baseline = None
        if drawing_entries:
            reference = text_profiles[other] if text_profiles[other]["requirements"] else final.get(other)
            if reference and (reference.get("requirements") or reference.get("quantities")):
                from nodes.drawing_analysis import build_baseline_set
                baseline = build_baseline_set(reference, other, review_focus)
        for idx, entry in drawing_entries:
            print(f"[analyze_documents] '{labels[slot]}' file {idx + 1}/{len(files[slot])}: {entry.get('name')} (drawing)")
            profile, summary = _profile_drawing_file(entry, slot, labels[slot], review_focus, baseline)
            _tag_items(profile, slot, entry.get("name") or "drawing", idx)
            parts[slot].append(profile)
            summaries[slot].append(summary)
        final[slot] = _merge_set(slot, labels[slot], parts[slot], summaries[slot])

    profile_a, profile_b = final["a"], final["b"]

    warnings: List[str] = []
    for prof in (profile_a, profile_b):
        label = prof["label"]
        for summary in prof.get("files") or []:
            warnings.extend(summary.get("warnings") or [])
            name = summary["name"]
            if summary["kind"] == "drawing":
                continue
            if summary["chars"] == 0:
                warnings.append(f"'{label}' / '{name}' contained no readable text - nothing could be compared against it.")
            elif not summary["requirements"] and not summary["quantities"]:
                warnings.append(
                    f"No requirements or quantities could be extracted from '{label}' / '{name}'."
                )
            status = summary.get("focus_status")
            if status == "sectioned":
                warnings.append(
                    f"Review focus '{review_focus}' narrowed '{label}' / '{name}' to "
                    f"{summary['chunks']}/{summary['total_chunks']} chunk(s) before analysis. "
                    f"Content outside these sections was not scanned."
                )
            elif status == "no_headings_found":
                warnings.append(
                    f"Review focus '{review_focus}' requested for '{label}' / '{name}', but no clear "
                    f"section structure was detected - the full document was scanned instead."
                )
            elif status == "no_relevant_headings":
                warnings.append(
                    f"Review focus '{review_focus}' matched no sections in '{label}' / '{name}' - "
                    f"the full document was scanned instead."
                )
            elif status == "selection_failed":
                warnings.append(
                    f"Could not evaluate section relevance for '{label}' / '{name}' (LLM error) - "
                    f"the full document was scanned instead."
                )

    for prof in (profile_a, profile_b):
        print(
            f"[analyze_documents] '{prof['label']}': {len(prof.get('files') or [])} file(s), "
            f"{len(prof['requirements'])} requirements, {len(prof['quantities'])} quantities, "
            f"{len(prof['standards'])} standards"
        )
    print("[analyze_documents] === Profiling complete ===\n")

    describe = lambda p: f"'{p['label']}' {len(p.get('files') or [])} file(s), {p['chunk_count']} chunk(s)/tile(s)"
    from nodes.assessment import assess_inputs
    inputs = assess_inputs(profile_a, profile_b)
    out = {
        "profile_a": profile_a,
        "profile_b": profile_b,
        "documents_analyzed": True,
        "input_assessment": inputs,
        "analysis_warnings": warnings,
        "step_history": [f"analyze_documents: {describe(profile_a)}; {describe(profile_b)}"],
    }
    if not inputs["ok"]:
        # Honest stop: comparing against an empty or unreadable set can only invent "gaps".
        print(f"[analyze_documents] Not enough to compare - scan stopped. {' '.join(inputs['problems'])}")
        out.update(discrepancies=[], scan_summary=inputs["headline"], confidence_level="low",
                   assessment={**inputs, "counts": {"real": 0, "total_raised": 0}, "compared": {"pairs": 0}},
                   scan_complete=True)
        out["step_history"] = out["step_history"] + ["conflict_scan: not run - not enough readable content to compare"]
    return out
