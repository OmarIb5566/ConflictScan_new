"""
Accuracy gates (audit of 25 Nov 2026, run b30e2e77): a brick / pump-station
specification against water-tank drawings produced 346 findings, of which
about 9 were real. Each test pins one cause down with the shape of the real
case that triggered it.
"""

import json
import os

from test_workspace_api import api  # noqa: F401 - sets up the test database
from nodes import scope
from nodes import conflict_scan as cs
from nodes.wording import user_text, has_internal_terms

SPEC = """KARENGE - WATER TRANSMISSION (TENDER 4)
▶ PTS – Particular Technical Specifications (Tender 4), Chapter III Water Storage Tanks — 3.10 Valve Chamber Construction
3.10  Valve Chamber Construction
The metallic access door to each valve chamber shall have a clear opening of one hundred centimetres wide.
▶ PTS – Particular Technical Specifications (Tender 4), Chapter III Water Storage Tanks — 3.16 Fence, Guardhouse, and Services
3.16  Fence, Guardhouse, and Services
The guardhouse shall provide minimum thirty square metres covered floor area.
The soakpit shall not be within twenty metres of any well or borehole.
Section 4 (Metallic Works), which governs steel fabrication, applies.
▶ PTS – Particular Technical Specifications (Tender 4), Chapter XII Pumping Station PS-GAH-2 (GAHENGERI) — 12.2.3 Frame and Walls
12.2.3  Frame and Walls
The superstructure frame shall comprise reinforced concrete columns of 200×200 mm section with four Y12 bars.
External walls shall be two hundred and fifty millimetre thick brick masonry.
"""

TANK = "Drawings_Tanks_-_Structural_MR-NYA20_2_X_2750M3.pdf"
TANK2 = "Drawings_Tanks_-_Structural_BR-MWU02-200M3.pdf"
PUMP = "Drawings_Pump_Station_PS-GAH-2_PS-GAH2_ARCHITECTURE.pdf"
SITE = "a2622c35_Drawings_Tanks_-_Site_Plans_fence_gate_guard_house_septic_drainage_MR-NYA20_-2X2750M3.pdf"


def spec_profile():
    quotes = ["The metallic access door to each valve chamber shall have a clear opening",
              "The guardhouse shall provide minimum thirty square metres covered floor area",
              "The soakpit shall not be within twenty metres of any well",
              "The superstructure frame shall comprise reinforced concrete columns of 200×200 mm",
              "External walls shall be two hundred and fifty millimetre thick brick masonry"]
    values = ["100 cm", "30 square metres", "20 m", "200×200 mm", "250 mm"]
    topics = ["Valve chamber door - opening", "Guardhouse - floor area", "Soakpit - setback",
              "Columns - section size", "External walls - thickness"]
    prof = {"requirements": [{"quote": q, "statement": q, "value": v, "topic": t, "side": "b", "uid": f"b:0:r{i}",
                              "source_file": "spec.pdf"} for i, (q, v, t) in enumerate(zip(quotes, values, topics))]}
    scope.assign_document_scope(prof, SPEC)
    return prof


def drawing_item(file, topic, value, sheet="1", **kw):
    return {"topic": topic, "statement": topic, "value": value, "source": "vision", "source_file": file,
            "sheet": sheet, "side": "a", **kw}


def test_clauses_take_their_structure_from_the_documents_headings():
    reqs = spec_profile()["requirements"]
    assert [r["scopes"] for r in reqs] == [["valve_chamber"], ["guardhouse"], ["sanitation"], ["pump_station"],
                                          ["pump_station"]]
    assert "Chapter XII Pumping Station" in reqs[3]["scope_heading"]


def test_drawing_scope_comes_from_the_drawing_not_from_the_models_wording():
    prof = {"requirements": [drawing_item(TANK, "column - section size", "400"),
                             drawing_item(TANK, "pit latrine - depth", "2500"),     # not something a tank sheet shows
                             drawing_item(SITE, "pit latrine - depth", "5250")],
            "sheets": [{"file": TANK, "sheet_reference": "1", "sheet_title": "IN TANK COLUMN DETAILS"}]}
    scope.annotate_drawing_items(prof)
    tank_col, tank_latrine, site_latrine = prof["requirements"]
    assert tank_col["scopes"] == ["tank"] and tank_latrine["scopes"] == ["tank"]
    assert site_latrine["scopes"] == ["sanitation"] and site_latrine["codes"] == ["NYA20"]   # upload prefix ignored


def test_pump_station_clause_is_never_compared_with_a_tank_drawing():
    spec = spec_profile()["requirements"]
    columns, walls = spec[3], spec[4]
    tank = {"requirements": [drawing_item(TANK, "column - vertical bars", "T16@200"),
                             drawing_item(TANK, "column - section size", "400")]}
    pump = {"requirements": [drawing_item(PUMP, "external wall - thickness", "200mm Thick")]}
    for p in (tank, pump):
        scope.annotate_drawing_items(p)
    assert not cs._gate(tank["requirements"][1], columns)       # tank column vs pump-station columns
    assert cs._gate(pump["requirements"][0], walls)             # pump-station wall vs pump-station walls: compared


def test_values_of_different_kinds_are_not_compared():
    assert not scope.kinds_compatible("10000", "30 square metres", "guardhouse - floor area", "floor area")
    assert not scope.kinds_compatible("T16@200", "200×200 mm")
    assert not scope.kinds_compatible("2500", "minimum thirty square metres")
    assert scope.kinds_compatible("200mm Thick", "250 mm")
    assert scope.kinds_compatible("400", "200×200 mm", "column - section size", "section size")
    assert scope.kinds_compatible("PRECAST CONCRETE FENCING POST", "galvanised SHS 50x50x2 mm posts")
    assert scope.kinds_compatible("4T16", "four Y12")


def test_checklist_tags_only_pair_the_element_they_name():
    spec = spec_profile()
    columns = spec["requirements"][3]
    slab = drawing_item(PUMP, "slab - thickness", "250", baseline_ids=["B-001"])
    column = drawing_item(PUMP, "column - section size", "250x250", baseline_ids=["B-001"])
    drawings = {"requirements": [slab, column], "baseline": {"reference_slot": "b", "items": [
        {"id": "B-001", "source_kind": "requirement", "source_uid": columns["uid"], "element": "reinforced concrete columns",
         "attribute": "section size"}]}}
    scope.annotate_drawing_items(drawings)
    pairs, _a, _b = cs._baseline_linked_pairs(drawings, spec)
    assert pairs == [(column, columns)]


def test_drawings_of_different_facilities_are_not_compared_internally():
    a = drawing_item(TANK, "column - section size", "230", grid="A", element_id="C1")
    b = drawing_item(TANK2, "column - section size", "150", grid="A", element_id="C1")
    c = drawing_item(TANK, "column - section size", "300", sheet="2", grid="A", element_id="C1")
    prof = {"requirements": [a, b, c]}
    scope.annotate_drawing_items(prof)
    pairs = cs.internal_candidates(prof["requirements"])
    assert pairs == [(a, c)]                       # same tank, two sheets - not two different tanks


def finding(i, b_says, a_says, ref, **kw):
    return {"topic": f"Column bars {i}", "issue_class": "doc_value_conflict", "severity": "major",
            "description": f"Bars differ ({a_says}).", "scope": "between",
            "evidence": [{"side": "a", "file": TANK, "reference": ref, "says": a_says, "drawing": True, **kw},
                         {"side": "b", "file": "spec.pdf", "reference": "12.2.3", "says": b_says, "drawing": False}]}


def test_one_clause_differing_at_many_places_is_one_finding():
    clause = "four Y12 vertical bars"
    raw = [finding(i, clause, f"T16@{150 + i * 50}", f"Sheet {i}") for i in range(5)]
    raw.append(finding(9, "Y8 links at 150 mm centres", "R10@200", "Sheet 9"))
    out = cs._dedupe_and_rank(raw)
    assert len(out) == 2
    grouped = next(d for d in out if d.get("locations"))
    assert grouped["locations"] == 5 and "5 places" in grouped["description"]
    assert len({e["reference"] for e in grouped["evidence"] if e["side"] == "a"}) == 5


def test_a_value_the_sheet_does_not_carry_is_only_a_point_to_confirm():
    out = cs._dedupe_and_rank([finding(1, "four Y12 vertical bars", "T25", "Sheet 1", value_unverified=True)])
    assert out[0]["severity"] == "minor" and out[0]["needs_confirmation"] and out[0]["low_confidence"]
    from nodes.rfi_drafting import rfi_drafting_agent
    assert rfi_drafting_agent({"discrepancies": out, "rfi_template_path": os.devnull})["rfi_items"] == []


def test_verifier_verdict_follows_its_four_answers():
    from fleet.verify import _verdict
    yes = {"same_structure": True, "same_element": True, "same_kind": True, "real_difference": True}
    assert _verdict(yes) == "confirmed"
    assert _verdict({**yes, "same_structure": False}) == "not_a_conflict"
    assert _verdict({**yes, "same_kind": "false"}) == "not_a_conflict"
    assert _verdict({"same_structure": True}) == "unsure"
    assert _verdict({"verdict": "confirmed"}) == "confirmed"            # a model answering the old way


def test_the_same_document_uploaded_twice_is_read_once(tmp_path):
    from nodes.document_analysis import _drop_duplicate_documents
    text = SPEC * 3
    jobs = [("b", 0, {"name": "spec.docx", "kind": "text", "text": text}),
            ("b", 1, {"name": "spec.pdf", "kind": "text", "text": text.replace("Tender 4", "Tender  4")}),
            ("b", 2, {"name": "boq.xlsx", "kind": "text", "text": "Item 1 | brickwork | 137.75 m2 | rate"}),
            ("a", 0, {"name": "other.pdf", "kind": "text", "text": text})]          # other set: kept
    kept, dropped = _drop_duplicate_documents(jobs)
    assert [(s, e["name"]) for s, _i, e in kept] == [("a", "other.pdf"), ("b", "spec.pdf"), ("b", "boq.xlsx")]
    assert [(s, e["name"], twin) for s, e, twin in dropped] == [("b", "spec.docx", "spec.pdf")]


def test_users_never_read_how_the_documents_were_processed():
    rfi_point = ("Items 4 and 5 indicate a column size of '4000'. These values are flagged as OCR errors "
                 "('partly legible') and likely represent 400 mm sections.")
    cleaned = user_text(rfi_point)
    assert not has_internal_terms(cleaned) and "400 mm" in cleaned
    assert user_text("Slab 250 mm (value not found in the sheet's text - verify). The vision model read tile 3.") \
        == "Slab 250 mm."
    from nodes.rfi_drafting import clean_text
    assert not has_internal_terms(clean_text("Are the values '4000' OCR errors for '400' mm?"))
    d = cs._normalise({"topic": "x", "description": "Read by OCR from a partly legible tile. Bars differ.",
                       "suggested_question": "Confirm the bars (OCR-read)."})
    assert d["description"] == "Bars differ." and not has_internal_terms(d["suggested_question"])


def test_drawing_reader_writes_a_compact_reply():
    from nodes.drawing_analysis import _TILE_SYSTEM, _item_to_requirement
    assert '"statement"' not in _TILE_SYSTEM and '"discipline"' not in _TILE_SYSTEM
    req = _item_to_requirement({"element": "wall", "attribute": "thickness", "value": "200", "category": "members"},
                               "S-1", "top-left", set())
    assert req["statement"] == "wall thickness: 200"
    assert "verify" not in req["statement"]


def test_a_restarted_scan_resumes_after_its_finished_stages(tmp_path):
    from nodes.checkpoint import checkpointed
    from nodes.run_paths import scan_dirs
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    calls = []

    def stage(state):
        calls.append(1)
        return {"profile_a": {"n": len(calls)}}

    node = checkpointed("analyze_documents", stage)
    state = {"document_a_files": [{"name": "a.txt", "path": str(f)}], "document_b_files": []}
    with scan_dirs(checkpoint_dir=str(tmp_path / "cp")):
        assert node(state) == {"profile_a": {"n": 1}}
        assert node(state) == {"profile_a": {"n": 1}} and len(calls) == 1          # resumed, not redone
        f.write_text("changed", encoding="utf-8")
        assert node(state) == {"profile_a": {"n": 2}}                             # new inputs: redone
    assert node(state) == {"profile_a": {"n": 3}}                                 # no scan folder: no checkpoint


def test_a_tile_reply_is_bounded_by_the_text_on_the_tile():
    from nodes.drawing_analysis import tile_output_budget
    from nodes.llm_client import NUM_PREDICT
    assert tile_output_budget(["T12@150 T&B"]) == 650           # one callout cannot hold dozens of facts
    assert tile_output_budget(["x"] * 60) == NUM_PREDICT        # a dense tile keeps the full budget
    assert tile_output_budget(None) == NUM_PREDICT              # text unknown (scan / OCR): full budget


def test_values_orders_of_magnitude_apart_are_different_measurements():
    # seen in the acceptance run: site distances and section dimensions compared with setbacks / heights
    assert not scope.kinds_compatible("40.0m, 50.0m", "a height of 450 mm maximum")
    assert not scope.kinds_compatible("0.1500", "not within twenty metres")
    assert not scope.kinds_compatible("0.0495m", "closer than 1 meter")
    assert scope.kinds_compatible("200mm Thick", "two hundred and fifty millimetre thick")
    assert scope.kinds_compatible("200X400mm", "200×450 mm")
    assert scope.length_mm("two hundred and fifty millimetre") == 250


def test_two_drawing_files_are_compared_only_when_their_numbers_name_the_same_facility():
    gah = drawing_item("Drawings_Tanks_-_Structural_ST-GAH-1000M3_STRUCTURAL.pdf", "wall - bars", "T12@150",
                       grid="A", element_id="W1")
    mwu = drawing_item(TANK2, "wall - bars", "J12@150", grid="A", element_id="W1")
    prof = {"requirements": [gah, mwu]}
    scope.annotate_drawing_items(prof)
    assert cs.internal_candidates(prof["requirements"]) == []


def test_a_clause_is_quoted_as_its_whole_sentence():
    # the extractor quoted '50x50x2 mm posts ...' and lost 'galvanised SHS' - the words that make the conflict
    text = ("3.16 Fence, Guardhouse, and Services\nThe security fence shall have a plinth wall, with galvanised SHS\n"
            "50x50x2 mm posts at maximum three metre centres, topped with a tension wire. The gate shall be steel.")
    prof = {"requirements": [{"quote": "50x50x2 mm posts at maximum three metre centres", "value": "50x50x2 mm"}]}
    scope.assign_document_scope(prof, text)
    q = prof["requirements"][0]["quote"]
    assert q.startswith("The security fence") and "galvanised SHS 50x50x2" in q and q.endswith("tension wire.")


def test_steel_grades_and_gauges_are_kinds_of_value():
    assert not scope.kinds_compatible("500 X 250", "hot-rolled grade S275")      # an RC beam size vs a steel grade
    assert not scope.kinds_compatible("200", "18 gauge galvanized steel")
    assert cs._gate_value({"value": None, "quote": "within twenty metres"}) == "within twenty metres"


def test_a_bare_number_on_a_drawing_is_only_a_point_to_confirm():
    # every false finding of acceptance run 3 rested on one: '800', '0.2300', '2.5000', '40.0'
    bare = finding(1, "a height of 450 mm maximum", "800", "Sheet 6")
    bare["evidence"][0]["value"] = "800"
    labelled = finding(2, "two hundred and fifty millimetre thick brick masonry", "200mm Thick", "Sheet 3")
    labelled["evidence"][0]["value"] = "200mm Thick"
    out = {d["topic"]: d for d in cs._dedupe_and_rank([bare, labelled])}
    assert out["Column bars 1"]["needs_confirmation"] and out["Column bars 1"]["severity"] == "minor"
    assert not out["Column bars 2"].get("needs_confirmation") and out["Column bars 2"]["severity"] == "major"
