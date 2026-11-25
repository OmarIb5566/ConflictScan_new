"""
Scope and value-kind gates
==========================
Deterministic checks that decide whether two statements can be compared at
all, BEFORE any model sees them. Found necessary by the 25 Nov 2026 audit:
58% of the findings compared a pump-station clause with tank drawings, and
others compared a bare "2500" with "minimum thirty square metres".

1. Structure scope - which structure / facility a statement is about.
   * A specification clause takes it from the document's own headings
     (section heading first, then chapter), else from its stated location.
   * A drawing item takes it from its sheet title or file name; an element
     name the vision model wrote is only trusted when the drawing itself
     shows that kind of structure.
   Two statements are comparable unless both have a known scope and the
   scopes are unrelated (a valve chamber is part of a tank or a pump station,
   a fence part of the site, ...).

2. Facility codes - drawing numbers name the facility (MR-NYA20, BR-MWU02).
   Two drawings with codes in common - or no codes - may be compared; two
   drawings of different facilities may not.

3. Value kinds - what kind of quantity a value is (length, area, rebar
   callout, grade, ...). Two numeric values of different kinds are not a
   conflict, they are about different things.

Windows compatible.
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Set

# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

# Longer phrases first: "septic tank" must not count as a "tank".
STRUCTURE_TERMS = [
    ("septic tank", "sanitation"), ("soak pit", "sanitation"), ("soakpit", "sanitation"),
    ("soakaway", "sanitation"), ("pit latrine", "sanitation"), ("latrine", "sanitation"),
    ("septic", "sanitation"), ("sewage", "sanitation"), ("sewer", "sanitation"),
    ("pumping station", "pump_station"), ("pump station", "pump_station"), ("pump house", "pump_station"),
    ("pumphouse", "pump_station"), ("booster station", "pump_station"), ("lift station", "pump_station"),
    ("valve chamber", "valve_chamber"), ("valve pit", "valve_chamber"), ("valve house", "valve_chamber"),
    ("valve room", "valve_chamber"),
    ("guard house", "guardhouse"), ("guardhouse", "guardhouse"), ("gatehouse", "guardhouse"),
    ("gate house", "guardhouse"), ("guard room", "guardhouse"), ("security room", "guardhouse"),
    ("boundary wall", "fence"), ("fencing", "fence"), ("fence", "fence"), ("gate", "fence"),
    ("drainage", "drainage"), ("storm water", "drainage"), ("culvert", "drainage"),
    ("site plan", "site"), ("site layout", "site"), ("external works", "site"), ("site works", "site"),
    ("water storage", "tank"), ("reservoir", "tank"), ("tanks", "tank"), ("tank", "tank"),
    ("substation", "substation"), ("transformer room", "substation"),
]

# A part may be shown on - and specified for - the drawings of the whole.
PART_OF = {
    "valve_chamber": {"tank", "pump_station"},
    "fence": {"site"}, "guardhouse": {"site"}, "sanitation": {"site"}, "drainage": {"site"},
}

_TERM_RES = [(re.compile(r"(?<![a-z])" + re.escape(t).replace(r"\ ", r"[\s_\-]*") + r"(?![a-z])"), g)
             for t, g in STRUCTURE_TERMS]


def structures(*texts: Any) -> Set[str]:
    """Structure groups named in the text(s)."""
    text = " ".join(str(t or "") for t in texts).lower().replace("_", " ")
    found: Set[str] = set()
    for rx, group in _TERM_RES:
        if rx.search(text):
            found.add(group)
            text = rx.sub(" ", text)          # 'septic tank' is not also a 'tank'
    return found


def _expand(groups: Set[str]) -> Set[str]:
    out = set(groups)
    for g in groups:
        out |= PART_OF.get(g, set())
    return out


def scopes_compatible(a: Iterable[str], b: Iterable[str]) -> bool:
    """Unknown scope on either side: comparable. Otherwise the structures must be the same or part / whole."""
    a, b = set(a or ()), set(b or ())
    if not a or not b:
        return True
    return bool(a & b or _expand(a) & b or a & _expand(b))


# ---------------------------------------------------------------------------
# Facility codes (drawing numbers)
# ---------------------------------------------------------------------------

_UPLOAD_PREFIX = re.compile(r"^[0-9a-f]{8}_")      # stored uploads are named '<8 hex>_<original name>'
_UNITLIKE = re.compile(r"^\d+(?:x\d+)*(?:m[23]|mm|m|kva|kw|kv|dn\d*|pn\d*)?$", re.I)


def facility_codes(*texts: Any) -> Set[str]:
    """Letter+digit tokens that name a facility in a file name or title (NYA20, MWU01) - sizes excluded."""
    codes: Set[str] = set()
    for text in texts:
        text = _UPLOAD_PREFIX.sub("", str(text or ""))
        for tok in re.split(r"[^A-Za-z0-9]+", text):
            if len(tok) < 4 or not re.search(r"[A-Za-z]", tok) or not re.search(r"\d", tok):
                continue
            if _UNITLIKE.match(tok) or re.match(r"^\d", tok):
                continue                     # 200M3, 2X2750M3, 1000M3 - sizes, not names
            codes.add(tok.upper())
    return codes


def codes_compatible(a: Iterable[str], b: Iterable[str]) -> bool:
    a, b = set(a or ()), set(b or ())
    return not a or not b or bool(a & b)


# ---------------------------------------------------------------------------
# Specification headings -> scope of each clause
# ---------------------------------------------------------------------------

_CHAPTER_RE = re.compile(r"^\s*(?:▶\s*)?(?:chapter|part|section|division)\s+[\w.]+", re.I)
_NUMBERED_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,3}){0,3})\.?\s+([A-Z][A-Za-z]{2,}\b[^\n]{0,90})$")
_ARROW_RE = re.compile(r"^\s*▶\s*(.+)$")


def heading_index(text: str) -> List[Dict[str, Any]]:
    """Headings with their position: level 1 = chapter / part, level 2 = numbered section."""
    out: List[Dict[str, Any]] = []
    pos = 0
    for line in (text or "").splitlines(keepends=True):
        stripped = line.strip()
        if 3 <= len(stripped) <= 200:
            m = _ARROW_RE.match(stripped)
            if m:                                   # "▶ PTS ... Chapter III Water Storage Tanks — 3.10 Valve Chamber"
                parts = re.split(r"\s+(?:—|--)\s+", m.group(1))
                out.append({"pos": pos, "level": 1, "text": " ".join(parts[:-1]) or parts[0]})
                if len(parts) > 1:
                    out.append({"pos": pos, "level": 2, "text": parts[-1]})
            elif _CHAPTER_RE.match(stripped) and len(stripped.split()) <= 8 and "," not in stripped:
                out.append({"pos": pos, "level": 1, "text": stripped})
            else:
                n = _NUMBERED_RE.match(stripped)
                if n and not stripped.endswith((".", ";", ",")) and len(stripped.split()) <= 12:
                    level = 1 if "." not in n.group(1) else 2
                    out.append({"pos": pos, "level": level, "text": stripped})
        pos += len(line)
    return out


def _find(text: str, phrase: str) -> int:
    words = re.findall(r"\w+", str(phrase or ""))[:8]
    if len(words) < 3:
        return -1
    m = re.search(r"\W+".join(re.escape(w) for w in words), text, re.I)
    return m.start() if m else -1


_SENTENCE_END = re.compile(r"[.;!?](?=\s|$)|\n\s*\n|\n\s*[•▶\-–]\s")
MAX_SENTENCE_CHARS = 500


def full_sentence(text: str, pos: int, quote: str, headings: Optional[List[Dict[str, Any]]] = None) -> str:
    """
    The whole sentence a quote sits in. Extractors often start a quote
    mid-sentence ('50x50x2 mm posts at ...' without the 'galvanised SHS'
    before it), which hides the very words that make a conflict.
    """
    window = max(0, pos - MAX_SENTENCE_CHARS)
    start = window                       # no sentence end before it: the window start (too long -> quote kept)
    for m in _SENTENCE_END.finditer(text, window, pos):
        start = m.end()
    for h in headings or []:              # a heading line ends the previous "sentence" too
        if h["pos"] < pos:
            line_end = text.find("\n", h["pos"])
            if 0 <= line_end < pos:
                start = max(start, line_end + 1)
    end_m = _SENTENCE_END.search(text, pos + max(len(quote or "") - 5, 0))
    end = end_m.start() + (1 if end_m and text[end_m.start()] in ".;!?" else 0) if end_m else len(text)
    sentence = re.sub(r"\s+", " ", text[start:end]).strip(" •▶-–")
    if not sentence or len(sentence) > MAX_SENTENCE_CHARS or len(sentence) < len((quote or "").strip()) * 0.8:
        return quote
    return sentence


def assign_document_scope(profile: Dict[str, Any], text: str) -> None:
    """
    Stamp each requirement / quantity of a text document with `scope_heading`
    and `scopes`, and widen its quote to the full sentence it was taken from.
    """
    headings = heading_index(text)
    for item in (profile.get("requirements") or []) + (profile.get("quantities") or []):
        pos = -1
        for phrase in (item.get("quote"), item.get("statement"), item.get("description")):
            pos = _find(text, phrase)
            if pos >= 0:
                break
        if pos >= 0 and item.get("quote"):
            item["quote"] = full_sentence(text, pos, item["quote"], headings)
        section = chapter = ""
        if pos >= 0:
            for h in headings:
                if h["pos"] > pos:
                    break
                if h["level"] == 1:
                    chapter, section = h["text"], ""
                else:
                    section = h["text"]
        if not (section or chapter):                 # the clause number in the reference, if any
            m = re.search(r"\b(\d{1,2}\.\d{1,3}(?:\.\d{1,3})?)\b", str(item.get("reference") or ""))
            if m:
                h = next((h for h in headings if h["text"].lstrip("▶ ").startswith(m.group(1))), None)
                section = h["text"] if h else ""
        item["scope_heading"] = " — ".join(p for p in (chapter, section) if p)
        # The clause's own subject first ("The soakpit shall ..."), then its section, chapter, location.
        subject = " ".join(re.findall(r"\w+", str(item.get("quote") or item.get("statement")
                                                  or item.get("description") or ""))[:8])
        scopes = (structures(subject) or structures(section) or structures(chapter)
                  or structures(item.get("location")))
        item["scopes"] = sorted(scopes)


def annotate_drawing_items(profile: Dict[str, Any]) -> None:
    """Stamp drawing items with `scopes` and `codes` from their sheet title and file name."""
    titles = {}
    for s in profile.get("sheets") or []:
        titles[(s.get("file"), str(s.get("sheet_reference")))] = " ".join(
            str(s.get(k) or "") for k in ("sheet_title", "areas") if s.get(k))
    for item in (profile.get("requirements") or []) + (profile.get("quantities") or []):
        if "scopes" in item and "codes" in item:
            continue
        file_name = str(item.get("source_file") or "")
        title = titles.get((file_name, str(item.get("sheet"))), "")
        drawing = structures(title) or structures(file_name)
        shown = structures(title) | structures(file_name)
        own = structures(item.get("location"), item.get("section_ref"), item.get("topic"))
        trusted = {g for g in own if scopes_compatible({g}, shown)} if shown else own
        item.setdefault("scopes", sorted(trusted or drawing))
        item.setdefault("codes", sorted(facility_codes(file_name)))


def item_scopes(item: Dict[str, Any]) -> Set[str]:
    if "scopes" in item:
        return set(item.get("scopes") or ())
    return structures(item.get("location"))


# ---------------------------------------------------------------------------
# Value kinds
# ---------------------------------------------------------------------------

_NUMWORD = (r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|"
            r"fifty|sixty|seventy|eighty|ninety|hundred|thousand)")
_KIND_RES = [
    ("rebar", re.compile(r"(?<![A-Za-z])(?:\d+\s*)?[TYRHØφ]\s?\d{1,2}(?:\s*[@/-]\s*\d+|\b)|\bbars?\s+(?:dia|ø)", re.I)),
    ("grade", re.compile(r"\bC\s?\d{2}\s?/\s?\d{2}\b|\bC\d{2}\b|\bgrade\s+[A-Z]?\d+|\bS\s?[2-4]\d{2}\b|\bB\s?500", re.I)),
    ("gauge", re.compile(r"\bgauge\b|\bSWG\b|\bBWG\b", re.I)),
    ("strength", re.compile(r"N/mm|MPa|kg/cm|psi\b|\d\s*bars?\b(?!\s*(?:dia|top|bottom))", re.I)),
    ("area", re.compile(r"m²|m2\b|sq\.?\s?m|square\s+met", re.I)),
    ("volume", re.compile(r"m³|m3\b|cubic|litres?|liters?", re.I)),
    ("dimensions", re.compile(r"\d\s*[x×*]\s*\d", re.I)),
    ("weight", re.compile(r"\btons?\b|\btonnes?\b|\bkN\b|\bkg\b(?!/)", re.I)),
    ("ratio", re.compile(r"%|\b1\s?:\s?\d|percent|slope|gradient|ratio", re.I)),
    ("count", re.compile(r"\b\d+\s*(?:no\.?|nos|nr|pcs|units?|sets?|pieces)\b", re.I)),
    ("length", re.compile(r"\d\s*(?:mm|cm|m|metres?|meters?)\b|" + _NUMWORD + r"[\w\s-]*?(?:millimet|centimet|met(?:re|er))", re.I)),
    ("electrical", re.compile(r"\b\d+\s*(?:kva|kw|kv|v|a|w|hz)\b", re.I)),
]


def value_kinds(value: Any) -> Set[str]:
    """Kinds of quantity in a value; {'number'} for a bare number; set() for text without numbers."""
    text = str(value or "").strip()
    if not text or not re.search(r"\d|" + _NUMWORD, text, re.I):
        return set()
    kinds = {k for k, rx in _KIND_RES if rx.search(text)}
    if kinds & {"area", "volume"}:
        kinds.discard("length")           # "thirty square metres" is an area, not a length
    if "dimensions" in kinds:
        kinds.add("length")
    if not kinds and re.fullmatch(r"[\s\d.,|+\-]+", text):
        kinds.add("number")
    return kinds


_DIMENSIONAL_ATTR = re.compile(
    r"thick|width|wide|height|high|depth|deep|length|long|size|dimension|diam|spacing|distance|clear|"
    r"cover|level|elev|span|opening|setback|section", re.I)


_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
             "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_LEN_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(mm|cm|m|met(?:re|er)s?|millimet\w*|centimet\w*)?\b", re.I)
_UNIT_MM = {"mm": 1, "cm": 10, "m": 1000}
MAX_MAGNITUDE_RATIO = 10          # real conflicts are 200 vs 250, not 40 m vs 450 mm


def _words_to_number(text: str) -> Optional[float]:
    total, current, seen = 0, 0, False
    for w in re.findall(r"[a-z]+", text.lower()):
        if w in _WORD_NUM:
            current += _WORD_NUM[w]
            seen = True
        elif w == "hundred" and seen:
            current = max(1, current) * 100
        elif w == "thousand" and seen:
            total += max(1, current) * 1000
            current = 0
        elif w in ("and", "point"):
            continue
        elif seen:
            break
    return float(total + current) if seen else None


def length_mm(value: Any) -> Optional[float]:
    """First length in a value, in millimetres. Bare decimals below 50 are metres (levels, distances), other bare numbers mm."""
    text = str(value or "")
    m = _LEN_RE.search(text)
    if m:
        number = float(m.group(1).replace(",", "."))
        unit = (m.group(2) or "").lower()
        if unit.startswith("mill"):
            unit = "mm"
        elif unit.startswith("cent"):
            unit = "cm"
        elif unit.startswith("met"):
            unit = "m"
        if not unit:
            unit = "m" if ("." in m.group(1) or "," in m.group(1)) and number < 50 else "mm"
        return number * _UNIT_MM[unit]
    words = _words_to_number(text)
    if words is None:
        return None
    unit = re.search(r"(millimet|centimet|met(?:re|er))", text, re.I)
    if not unit:
        return None
    return words * (1 if unit.group(1).lower().startswith("mill") else 10 if unit.group(1).lower().startswith("cent") else 1000)


def kinds_compatible(a_value: Any, b_value: Any, a_topic: str = "", b_topic: str = "") -> bool:
    """False only when both values are numeric and plainly about different kinds of quantity."""
    ka, kb = value_kinds(a_value), value_kinds(b_value)
    if not ka or not kb:
        return True                       # a type / material / text value - compare by meaning
    lengths = {"length", "dimensions", "number"}
    if ka & lengths and kb & lengths:
        la, lb = length_mm(a_value), length_mm(b_value)
        if la and lb and max(la, lb) / min(la, lb) > MAX_MAGNITUDE_RATIO:
            return False                  # orders of magnitude apart: two different measurements
    if ka & kb:
        return True
    for bare, other, topic in ((ka, kb, b_topic + " " + a_topic), (kb, ka, a_topic + " " + b_topic)):
        if bare == {"number"}:
            # drawings give lengths as bare millimetres - comparable with a length, nothing else
            return bool(other & {"length", "dimensions"}) and bool(_DIMENSIONAL_ATTR.search(topic))
    return False


# ---------------------------------------------------------------------------
# Element match (for checklist links)
# ---------------------------------------------------------------------------

_GENERIC = frozenset("""
the of and for to in on at with from a an all each typical typ detail section plan elevation view sheet
item items element general note notes value values size type
""".split())


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w


def element_tokens(*texts: Any) -> Set[str]:
    words = re.findall(r"[a-z]{3,}", " ".join(str(t or "") for t in texts).lower())
    return {_stem(w) for w in words if w not in _GENERIC}


def same_element(drawing_item: Dict[str, Any], element: str, attribute: str = "") -> bool:
    """The drawing item names the checklist element (shared word), e.g. 'fence post' ~ 'fencing posts'."""
    topic = str(drawing_item.get("topic") or "")
    item_element = topic.split(" - ")[0]
    return bool(element_tokens(item_element, drawing_item.get("element_id")) & element_tokens(element))
