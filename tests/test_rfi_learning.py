"""
Tests for the RFI-corpus-driven enhancements: taxonomy, location-aware
alignment, internal-consistency and interface candidates, finding
calibration/merging, RFI consolidation, multi-file ingestion, and a full graph
run over two multi-file sets with the LLM stubbed out.

    python -m pytest tests -q
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nodes import conflict_scan as cs                      # noqa: E402
from nodes import rfi_knowledge as rk                      # noqa: E402
from nodes.file_ingest import (                            # noqa: E402
    classify_file, decode_symbol_font, pair_drawing_files, read_document,
)
from nodes.rfi_drafting import build_token_values, group_discrepancies  # noqa: E402
from nodes.vision_client import collapse_repeats           # noqa: E402


# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

def test_taxonomy_loads_and_classifies_generic_language():
    keys = {c["key"] for c in rk.issue_classes(detectable_only=False)}
    assert {"doc_value_conflict", "spatial_clash", "missing_information", "scope_gap_boq"} <= keys
    assert rk.best_issue_class("The footings of adjacent panels overlap in plan - clash") == "spatial_clash"
    assert rk.best_issue_class("Item shown on drawings but not included in the BOQ") == "scope_gap_boq"
    assert rk.best_issue_class("Kindly provide the missing slab thickness") == "missing_information"
    assert "NOT a physical location" in rk.not_discrepancy_prompt()
    assert "which_governs" in rk.rfi_style_prompt()


def test_taxonomy_regexes_have_no_control_characters():
    for c in rk.issue_classes(detectable_only=False):
        for kw in c["keywords"]:
            assert not any(ord(ch) < 32 for ch in kw), (c["key"], kw)


# ---------------------------------------------------------------------------
# Location-aware alignment - the failure seen on real drawings
# ---------------------------------------------------------------------------

def _drawing_item(file, value, sheet="Page 1", **extra):
    return {
        "topic": "Level datum - elevation", "statement": f"level {value}", "value": value,
        "reference": f"Sheet {sheet} | Zone bottom-right of sheet | Detail n/a",
        "source": "vision", "source_file": file, "sheet": sheet, **extra,
    }


def test_two_drawings_without_shared_anchor_are_never_compared():
    a = _drawing_item("fence.pdf", "+154.00")
    b = _drawing_item("retaining_wall.pdf", "+151.865")
    pairs, _ua, _ub = cs.align_requirements([a], [b])
    assert pairs == []                                  # same sheet position is not the same place


def test_same_section_or_same_real_sheet_is_comparable():
    a = _drawing_item("x.pdf", "+154.00", section_ref="SEC 01-01")
    b = _drawing_item("y.pdf", "+153.85", section_ref="Section 1-1")
    assert cs._comparable(a, b)
    c = _drawing_item("rev0.pdf", "+154.00", sheet="S-208")
    d = _drawing_item("rev1.pdf", "+153.85", sheet="S-208")
    assert cs._comparable(c, d)                          # same sheet number across revisions


def test_different_sections_or_tags_are_incompatible():
    a = _drawing_item("x.pdf", "1", section_ref="SEC 01-01")
    b = _drawing_item("x.pdf", "2", section_ref="SEC 3-3")
    assert not cs._locations_compatible(a, b)
    assert not cs._locations_compatible({"element_id": "B1"}, {"element_id": "B2"})
    assert cs._locations_compatible({"element_id": "TR-04"}, {"element_id": "tr4"})


def test_document_vs_drawing_pairs_on_keywords():
    boq = {"topic": "Lighting pole height", "statement": "Lighting pole 6 m", "value": "6 m", "source_file": "boq.pdf"}
    dwg = {"topic": "Lighting pole height", "statement": "Pole 8 m", "value": "8 m", "source": "vision",
           "source_file": "gd.pdf", "sheet": "GD-EL-01"}
    pairs, _ua, _ub = cs.align_requirements([boq], [dwg])
    assert len(pairs) == 1


# ---------------------------------------------------------------------------
# Internal consistency and interface candidates
# ---------------------------------------------------------------------------

def test_internal_candidates_find_load_fed_from_two_sources():
    r1 = {"element_id": "TH05", "topic": "TH05 - fed from", "value": "TR02", "statement": "TH05 fed from TR02",
          "source": "vision", "source_file": "sld.pdf", "sheet": "TR2"}
    r2 = {"element_id": "TH-05", "topic": "TH05 - fed from", "value": "TR03", "statement": "TH05 fed from TR03",
          "source": "vision", "source_file": "sld.pdf", "sheet": "TR3"}
    same = dict(r2, value="TR02")
    assert len(cs.internal_candidates([r1, r2])) == 1
    assert cs.internal_candidates([r1, same]) == []      # consistent repetition is not a finding


def test_interface_clusters_need_two_files_and_geometry():
    a = {"topic": "Fence footing - bottom level", "statement": "footing B.O.F +153.85", "value": "+153.85",
         "section_ref": "SEC 01-01", "source_file": "fence.pdf", "discipline": "structural", "side": "a"}
    b = {"topic": "Retaining wall - T.O.C", "statement": "wall top of concrete +154.00", "value": "+154.00",
         "section_ref": "SEC 1-1", "source_file": "rw.pdf", "discipline": "civil", "side": "b"}
    clusters = cs.interface_clusters([a, b])
    assert len(clusters) == 1 and len(clusters[0]["items"]) == 2
    assert cs.interface_clusters([a, dict(a, value="x")]) == []   # one file, one discipline


# ---------------------------------------------------------------------------
# Calibration, merging, consolidation
# ---------------------------------------------------------------------------

def test_ocr_evidence_never_critical_and_near_duplicates_merge():
    raw = [
        {"topic": "Transformer rating TR4", "issue_class": "doc_value_conflict", "severity": "critical",
         "description": "TR4 rating 1000 kVA on MV SLD vs 1250 kVA in BOQ", "element": "TR4",
         "evidence": [{"file": "boq.pdf", "reference": "p. 3", "says": "1250 kVA", "low_confidence": True}]},
        {"topic": "Transformer rating TR4", "issue_class": "doc_value_conflict", "severity": "major",
         "description": "TR4 rating 1000 kVA on MV SLD vs 1250 kVA in the BOQ", "element": "TR-04",
         "evidence": [{"file": "sld.pdf", "reference": "EP-TR-MV-SLD-100", "says": "1000 kVA"}]},
    ]
    out = cs._dedupe_and_rank(raw)
    assert len(out) == 1
    assert out[0]["severity"] == "major"
    assert len(out[0]["evidence"]) == 2
    assert out[0]["id"] == "D-001"


def test_distinct_items_with_templated_text_do_not_merge():
    q = lambda item: {"topic": f"Quantity - {item}", "category": "quantities", "issue_class": "doc_value_conflict",
                      "severity": "major", "description": f"The measured quantity for '{item}' is not consistent "
                                                          f"between the two sets: quantity differs by 20%."}
    assert len(cs._dedupe_and_rank([q("Concrete C30 footings"), q("Concrete C40 columns")])) == 2
    d = lambda tag: {"topic": f"Transformer {tag} rating", "issue_class": "doc_value_conflict", "severity": "major",
                     "description": "Rating differs between the BOQ and the single-line diagram."}
    assert len(cs._dedupe_and_rank([d("TR4"), d("TR6")])) == 2


def test_one_sided_or_interpretive_findings_are_not_critical():
    one_sided = {"topic": "Feeder details", "issue_class": "design_change", "severity": "critical",
                 "description": "Feeder detail must be confirmed.", "evidence": [{"side": "b", "says": "P04/TR02 630A"}]}
    interpretive = {"topic": "Busway rating", "issue_class": "doc_value_conflict", "severity": "critical",
                    "description": "Busway to be confirmed.", "interpretive": True,
                    "evidence": [{"side": "a", "says": "x"}, {"side": "b", "says": "y"}]}
    clash = {"topic": "Footings overlap", "issue_class": "spatial_clash", "severity": "critical",
             "description": "Adjacent footings overlap.", "evidence": [{"side": "a", "says": "F1 1.8 m wide at 1.5 m c/c"}]}
    out = {d["topic"]: d["severity"] for d in cs._dedupe_and_rank([one_sided, interpretive, clash])}
    assert out == {"Feeder details": "major", "Busway rating": "major", "Footings overlap": "critical"}


def test_unclassified_finding_gets_taxonomy_defaults():
    out = cs._dedupe_and_rank([{"topic": "Footings overlap", "description": "adjacent footings clash in plan"}])
    assert out[0]["issue_class"] == "spatial_clash"
    assert out[0]["type"] == "clash" and out[0]["severity"] == "critical"


def _disc(i, discipline, location, topic, severity="major"):
    return {"id": f"D-{i:03d}", "discipline": discipline, "location": location, "topic": topic,
            "severity": severity, "issue_class": "doc_value_conflict", "element": ""}


def test_rfi_grouping_consolidates_by_discipline_and_place():
    discs = [
        _disc(1, "electrical", "Infra", "Transformer kiosk rating"),
        _disc(2, "electrical", "Infra", "Lighting pole height"),
        _disc(3, "mechanical", "Plot 01", "Fire hydrant valve diameter"),
        _disc(4, "electrical", "Plot 01", "Fire alarm detectors in parking"),
    ]
    groups = group_discrepancies(discs)
    assert [[d["id"] for d in g] for g in groups] == [["D-001", "D-002"], ["D-003"], ["D-004"]]
    assert len(group_discrepancies(discs, "single")) == 4


def test_token_values_carry_attachments_and_area():
    rfi = {"rfi_no": "RFI-001", "attachments": ["sld.pdf - Sheet TR2", "boq.pdf - p. 3"], "area": "Infra"}
    values = build_token_values(rfi, {"rfi_form_meta": {}}, "A", "B")
    assert values["attachments"] == "1. sld.pdf - Sheet TR2\n2. boq.pdf - p. 3"
    assert values["location"] == "Infra" and values["attachment_count"] == "2"


# ---------------------------------------------------------------------------
# Drawing reader guards (seen on real runs)
# ---------------------------------------------------------------------------

def test_invented_values_are_flagged_against_the_tiles_text():
    from nodes.drawing_analysis import _verify_values
    strings = ["TR 2 | TO TR3", "P01/TR02 | 630A | P05/TR03 | 400A", "TO TH05 | Spare", "380V,50HZ,30KA"]
    reqs = [
        {"value": "630A", "statement": "breaker rating"},
        {"value": "4/0 AWG Copper", "statement": "cable size"},          # not on the sheet
        {"value": "P01/TR02", "statement": "feeder"},
    ]
    assert _verify_values(reqs, strings) == 1
    assert reqs[1]["legibility"] == "partial" and reqs[1]["low_confidence"]
    assert "legibility" not in reqs[0]


def test_prompt_echoes_and_parroted_checklist_items_are_dropped():
    from nodes.drawing_analysis import _checklist_phrases, _item_to_requirement
    baseline = {"items": [{"id": "B-004", "element": "Transformer # 09", "attribute": "Required location"}]}
    checklist = _checklist_phrases(baseline)
    overview = {"views": ["1.1"], "areas": [], "discipline": "electrical"}
    echo = {"element": "Tile reference", "attribute": "Location of tile", "value": "Sheet Page 1; tile 12 of 12",
            "statement": "tile 12 of 12"}
    view = {"element": "Section/View Label", "attribute": "View Reference", "value": "1.1", "statement": "view 1.1"}
    parrot = {"element": "Transformer # 09", "attribute": "Required location", "value": None,
              "statement": "Transformer 09 location", "element_id": "B-004"}
    real = {"element": "Feeder P01/TR02", "attribute": "fed from", "value": "TR02", "statement": "P01 fed from TR02",
            "element_id": "B-001"}
    assert _item_to_requirement(echo, "Page 1", "top", set(), overview, checklist) is None
    assert _item_to_requirement(view, "Page 1", "top", set(), overview, checklist) is None
    assert _item_to_requirement(parrot, "Page 1", "top", set(), overview, checklist) is None
    kept = _item_to_requirement(real, "Page 1", "top", set(), overview, checklist)
    assert kept and kept["element_id"] == ""                 # checklist id is not a drawing tag


def test_blank_tiles_are_detected(tmp_path):
    from PIL import Image, ImageDraw
    from nodes.drawing_analysis import _ink_fraction
    blank = tmp_path / "blank.png"
    Image.new("RGB", (1600, 1600), "white").save(blank)
    busy = tmp_path / "busy.png"
    img = Image.new("RGB", (1600, 1600), "white")
    d = ImageDraw.Draw(img)
    for y in range(0, 1600, 40):
        d.line((0, y, 1600, y), fill="black", width=4)
    img.save(busy)
    assert _ink_fraction(blank) < 0.003 < _ink_fraction(busy)


# ---------------------------------------------------------------------------
# Verbatim quotes and concise RFI points
# ---------------------------------------------------------------------------

def test_verbatim_quote_is_always_from_the_source():
    from nodes.document_analysis import verbatim_quote
    chunk = "[Page 3]\nItem 2.4 - Access door, fire rated 45 Min, height 1200 mm.\nItem 2.5 - Louvers, aluminium."
    good = {"quote": "fire rated 45  Min", "statement": "Access door fire rating", "value": "45 Min"}
    assert verbatim_quote(good, chunk) == "fire rated 45 Min"
    invented = {"quote": "doors shall be 45 minutes rated", "topic": "Access door fire rating",
                "statement": "Access door is fire rated", "value": "45 Min"}
    assert verbatim_quote(invented, chunk) == "Item 2.4 - Access door, fire rated 45 Min, height 1200 mm."
    assert verbatim_quote({"quote": "", "statement": "glass type double", "value": "6+12+6"}, chunk) == ""


def test_drawing_quote_uses_the_sheet_text():
    from nodes.drawing_analysis import _drawing_quote
    strings = ["P01/TR02 | 630A", "TO TH05 | Spare"]
    assert _drawing_quote({"value": "630A"}, strings) == "P01/TR02 | 630A"
    assert _drawing_quote({"value": "TH05"}, strings) == "TO TH05 | Spare"
    assert _drawing_quote({"value": "B(250X400)"}, []) == "B(250X400)"


def test_references_drop_sheet_zones():
    assert cs.compact_reference("Sheet Page 1 | Zone top-left of sheet | Detail n/a") == "Sheet Page 1"
    assert cs.compact_reference("Sheet S-208 | Grid X95/Y14 | Detail SEC 3-3") == "Sheet S-208, Grid X95/Y14, Detail SEC 3-3"


def test_rfi_points_are_short_and_quote_sources():
    from nodes.rfi_drafting import _assemble, clean_text
    assert clean_text("1- a<br>2- b<BR/>3- c") == "1- a\n2- b\n3- c"
    disc = {"id": "D-001", "severity": "major", "issue_class": "cross_discipline_mismatch", "topic": "Roof beam size",
            "description": "Beam size differs. Long explanation follows here.", "discipline": "structural",
            "evidence": [{"side": "a", "document": "Arch IFC", "file": "A.pdf", "reference": "Sheet A-502", "says": "BEAM 250X500"},
                         {"side": "b", "document": "Str IFC", "file": "S.pdf", "reference": "Sheet S-208", "says": "B1 200x500"}]}
    wordy = " ".join(["word"] * 120)
    rfi = _assemble("RFI-001", [disc], {"points": [{"discrepancy_id": "D-001", "summary": wordy + "<br>more",
                                                    "question": "Please advise which size shall be followed?"}]},
                    "01 Jan", "Arch", "Str")
    point = rfi["points"][0]
    assert len(point["summary"].split()) <= 41 and "<br>" not in point["text"]
    assert '- Arch IFC (A.pdf, Sheet A-502): "BEAM 250X500"' in point["text"]
    assert '- Str IFC (S.pdf, Sheet S-208): "B1 200x500"' in point["text"]
    fallback = _assemble("RFI-002", [disc], {}, "01 Jan", "Arch", "Str")["points"][0]
    assert fallback["summary"] == "Beam size differs."


# ---------------------------------------------------------------------------
# Ingestion helpers
# ---------------------------------------------------------------------------

def test_collapse_repeats_keeps_numbers():
    assert collapse_repeats("qty 1000 | 1111 | 10 | 10 | 10") == "qty 1000 | 1111 | 10 | 10 | 10"
    assert collapse_repeats("cable " + "word word2 " * 30 + "end") == "cable word word2 end"


def test_symbol_font_decoding():
    assert decode_symbol_font(" ok") == "SHOP ok"
    assert decode_symbol_font("plain") == "plain"


def test_classify_and_read_generated_files(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    a3 = tmp_path / "sheet.pdf"
    doc = pymupdf.open()
    doc.new_page(width=1191, height=842).insert_text((72, 72), "GROUND FLOOR PLAN")
    doc.save(a3)
    a4 = tmp_path / "spec.pdf"
    doc = pymupdf.open()
    doc.new_page(width=595, height=842).insert_text((72, 72), "SECTION 26 05 19 - Cables shall be copper. " * 3)
    doc.save(a4)
    assert classify_file(str(a3), use_vision=False)[0] == "drawing"
    assert classify_file(str(a4), use_vision=False)[0] == "document"
    text = read_document(str(a4), ocr=False)["text"]
    assert text.startswith("[Page 1]") and "copper" in text


def test_docx_tables_are_read(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_paragraph("Bill of Quantities")
    t = d.add_table(rows=2, cols=3)
    for i, row in enumerate((("Item", "Description", "Qty"), ("1.1", "Lighting pole 6 m", "40"))):
        for j, v in enumerate(row):
            t.rows[i].cells[j].text = v
    path = tmp_path / "boq.docx"
    d.save(path)
    text = read_document(str(path), ocr=False)["text"]
    assert "1.1 | Lighting pole 6 m | 40" in text


def test_dxf_pairs_with_same_named_pdf():
    files = [{"name": "S-101.pdf", "path": "x/S-101.pdf", "kind": "drawing"},
             {"name": "S-101.dxf", "path": "x/S-101.dxf", "kind": "drawing"},
             {"name": "boq.xlsx", "path": "x/boq.xlsx", "kind": "document"}]
    paired = pair_drawing_files(files)
    assert len(paired) == 2
    drawing = next(f for f in paired if f["kind"] == "drawing")
    assert drawing["dxf_path"] == "x/S-101.dxf" and drawing["path"] == "x/S-101.pdf"


# ---------------------------------------------------------------------------
# Full graph run over two multi-file sets, LLM stubbed
# ---------------------------------------------------------------------------

def _fake_llm(system, user, temperature=0.2, **_kw):
    """Deterministic stand-in for each prompt the pipeline sends."""
    if "ONE CHUNK" in system:
        reqs = []
        for line in user.splitlines():
            if line.count("|") == 3:
                topic, value, ref, eid = (p.strip() for p in line.split("|"))
                reqs.append({"topic": topic, "statement": f"{topic} {value}", "value": value,
                             "reference": ref, "discipline": "electrical", "element_id": eid,
                             "location": "Infra", "open_item": False})
        return {"requirements": reqs, "quantities": [], "standards": [], "entities": [], "notes": ""}
    if "set for internal" in system:          # internal consistency
        return {"discrepancies": [{"pair_index": 0, "topic": "TH05 source", "issue_class": "internal_inconsistency",
                                   "type": "contradiction", "severity": "major",
                                   "description": "TH05 shown fed from two transformers."}]}
    if "pairs of statements" in system:       # cross-set pair review
        return {"discrepancies": [{"pair_index": 0, "topic": "TR4 rating", "issue_class": "doc_value_conflict",
                                   "type": "contradiction", "severity": "major",
                                   "description": "TR4 rating differs between BOQ and SLD."}]}
    if "Requests For Information" in system:  # drafting - leave it to the fallback for one group
        return {"rfis": []}
    return {"discrepancies": []}


def test_full_graph_on_two_file_sets(monkeypatch, tmp_path):
    import nodes.llm_client as llm
    monkeypatch.setattr(llm.llm_client, "run", _fake_llm)
    monkeypatch.chdir(tmp_path)

    boq = tmp_path / "boq.txt"
    boq.write_text("Transformer TR4 rating | 1250 kVA | BOQ item 1.2 | TR4\n", encoding="utf-8")
    sld1 = tmp_path / "sld_tr2.txt"
    sld1.write_text("Transformer TR4 rating | 1000 kVA | SLD-100 | TR4\n\nTH05 fed from | TR02 | SLD-TR2 | TH05\n", encoding="utf-8")
    sld2 = tmp_path / "sld_tr3.txt"
    sld2.write_text("TH05 fed from | TR03 | SLD-TR3 | TH05\n", encoding="utf-8")

    from graph import run_conflict_scan
    state = run_conflict_scan(
        user_id="test",
        document_a_text="", document_b_text="",
        document_a_label="Tender BOQ", document_b_label="IFC Electrical",
        document_a_files=[{"name": "boq.txt", "path": str(boq), "kind": "document"}],
        document_b_files=[{"name": "sld_tr2.txt", "path": str(sld1), "kind": "document"},
                          {"name": "sld_tr3.txt", "path": str(sld2), "kind": "document"}],
        project_name="Test", rfi_template_path=os.path.join(ROOT, "templates_rfi", "none.docx"),
    )
    assert not state.get("error_message")
    classes = {d["issue_class"] for d in state["discrepancies"]}
    assert {"doc_value_conflict", "internal_inconsistency"} <= classes
    within = [d for d in state["discrepancies"] if d["scope"] == "within_b"]
    assert within and {e["file"] for e in within[0]["evidence"]} == {"sld_tr2.txt", "sld_tr3.txt"}
    # Both electrical findings at the same place become one RFI with two points
    assert len(state["rfi_items"]) == 1
    assert len(state["rfi_items"][0]["points"]) == 2
    assert state["register_path"] and os.path.exists(state["register_path"])


def test_graph_streams_progress_and_can_stop(monkeypatch, tmp_path):
    """The web app's background worker path: per-node callbacks, same final state, cooperative stop."""
    import nodes.llm_client as llm
    from graph import run_conflict_scan, ScanCancelled
    monkeypatch.setattr(llm.llm_client, "run", _fake_llm)
    monkeypatch.chdir(tmp_path)
    boq = tmp_path / "boq.txt"
    boq.write_text("Transformer TR4 rating | 1250 kVA | BOQ item 1.2 | TR4\n", encoding="utf-8")
    sld = tmp_path / "sld.txt"
    sld.write_text("Transformer TR4 rating | 1000 kVA | SLD-100 | TR4\n", encoding="utf-8")
    kwargs = dict(
        user_id="test", document_a_text="", document_b_text="",
        document_a_label="Tender BOQ", document_b_label="IFC Electrical",
        document_a_files=[{"name": "boq.txt", "path": str(boq), "kind": "document"}],
        document_b_files=[{"name": "sld.txt", "path": str(sld), "kind": "document"}],
        project_name="Test", rfi_template_path=os.path.join(ROOT, "templates_rfi", "none.docx"),
    )
    seen = []
    state = run_conflict_scan(**kwargs, on_node_complete=lambda node, delta: seen.append(node))
    assert seen == ["receive_input", "analyze_documents", "conflict_scan", "draft_rfi", "export"]
    assert state["discrepancies"] and state["rfi_items"] and state.get("export_complete")

    stopped = []
    with pytest.raises(ScanCancelled):
        run_conflict_scan(**kwargs, on_node_complete=lambda node, delta: stopped.append(node),
                          should_stop=lambda: len(stopped) >= 2)
    assert stopped == ["receive_input", "analyze_documents"]


def test_scan_writes_only_into_its_own_folders(monkeypatch, tmp_path):
    """Two scans never share an exports/ or debug_logs/ folder, and nothing lands in the working directory."""
    import nodes.llm_client as llm
    from graph import run_conflict_scan
    monkeypatch.setattr(llm.llm_client, "run", _fake_llm)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    boq = tmp_path / "boq.txt"
    boq.write_text("Transformer TR4 rating | 1250 kVA | BOQ item 1.2 | TR4\n", encoding="utf-8")
    sld = tmp_path / "sld.txt"
    sld.write_text("Transformer TR4 rating | 1000 kVA | SLD-100 | TR4\n", encoding="utf-8")
    outs = []
    for n in (1, 2):
        out = tmp_path / f"scan{n}"
        state = run_conflict_scan(
            user_id="test", document_a_text="", document_b_text="",
            document_a_label="Tender BOQ", document_b_label="IFC Electrical",
            document_a_files=[{"name": "boq.txt", "path": str(boq), "kind": "document"}],
            document_b_files=[{"name": "sld.txt", "path": str(sld), "kind": "document"}],
            project_name="Test", rfi_template_path=os.path.join(ROOT, "templates_rfi", "none.docx"),
            output_dir=str(out), debug_dir=str(tmp_path / f"debug{n}"),
        )
        assert os.path.dirname(state["register_path"]) == str(out)
        outs.append(state["register_path"])
    assert outs[0] != outs[1] and all(os.path.exists(p) for p in outs)
    assert list(cwd.iterdir()) == []


def test_run_paths_default_to_temp_and_no_debug(monkeypatch):
    from nodes import run_paths
    monkeypatch.delenv("CONFLICTSCAN_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("CONFLICTSCAN_DEBUG_DIR", raising=False)
    with run_paths.scan_dirs():
        out = run_paths.output_dir()
        assert os.path.isdir(out) and not out.startswith(ROOT)
        assert run_paths.output_dir() == out            # stable within one scan
        assert run_paths.debug_dir() is None
    assert run_paths.template_dir() == os.path.join(ROOT, "templates_rfi")

