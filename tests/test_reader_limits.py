"""
Model replies that run away: a vision model stuck repeating one item until the
output cap. Found in a real 86-sheet scan, where ~1 in 4 drawing tiles hit the
cap and was silently read as empty.

- a cut-off JSON reply keeps its complete items (duplicates removed)
- a looping stream is stopped early instead of running to the cap
- a cut-off reply is never cached (a re-run would replay the loss)
- the drawing reader de-duplicates, counts cut-off / failed tiles and says so,
  and the scan assessment lists it as a limitation
"""

import json
import time

from test_workspace_api import api  # noqa: F401 - sets up the test database
from test_fleet import fake_server, calls_for  # noqa: F401
import storage
from fleet import client, profile
from nodes import assessment
from nodes.llm_client import OllamaLLM, salvage_truncated_json

ITEM = {"category": "members", "element": "fence post", "attribute": "height", "value": "2050"}
OTHER = {"category": "members", "element": "fence post", "attribute": "spacing", "value": "3000"}


def looping_reply(n=40):
    body = ",\n".join(json.dumps(i, indent=2) for i in [OTHER] + [ITEM] * n)
    return '{\n  "items": [\n' + body + ",\n" + json.dumps(ITEM, indent=2)[:30]    # cut off inside the next copy


def test_cut_off_reply_keeps_its_complete_items():
    out = OllamaLLM()._extract_json(looping_reply())
    assert out["items"] == [OTHER, ITEM] and out["_truncated"] and out["_repeated"] == 39
    assert salvage_truncated_json('{"a": 1') is None                  # not a list reply - nothing to salvage
    assert OllamaLLM()._extract_json('{"items": [1, 2]}') == {"items": [1, 2]}   # complete replies untouched


def test_loop_detector():
    assert client.is_looping(looping_reply(60))
    varied = ",".join(json.dumps({"element": f"post {i}", "value": str(1000 + i * 7)}) for i in range(300))
    assert not client.is_looping('{"items": [' + varied)


class FakeStream:
    def __init__(self, pieces, final=None):
        self.lines = [json.dumps({"message": {"content": p}, "done": False}).encode() for p in pieces]
        if final is not None:
            self.lines.append(json.dumps({"message": {"content": ""}, "done": True, **final}).encode())
        self.read = 0
        self.closed = False

    def iter_lines(self):
        for line in self.lines:
            self.read += 1
            yield line

    def close(self):
        self.closed = True


def test_a_looping_stream_is_stopped_early():
    text = looping_reply(400)
    pieces = [text[i:i + 50] for i in range(0, len(text), 50)]
    r = FakeStream(pieces, final={"done_reason": "length", "eval_count": 8192})
    out = client._read_stream(r)
    assert out["looped"] and out["truncated"] and r.closed
    assert r.read < len(pieces) / 4                                    # stopped long before the cap
    assert OllamaLLM()._extract_json(out["content"])["items"] == [OTHER, ITEM]

    ok = FakeStream(['{"items": ', '[1, 2]}'], final={"done_reason": "stop", "eval_count": 7, "prompt_eval_count": 3})
    out = client._read_stream(ok)
    assert out == {"content": '{"items": [1, 2]}', "prompt_tokens": 3, "output_tokens": 7, "truncated": False,
                   "looped": False}


def test_a_cut_off_reply_is_not_cached(fake_server):
    prompt = f"dense tile {time.time()}"
    fake_server["reply"] = lambda payload: looping_reply()
    real_post = client._post

    def post(server, payload, timeout=900):
        return {**real_post(server, payload, timeout), "truncated": True}

    client._post = post
    run_id = storage.new_id()
    token = client.current_run.set(run_id)
    try:
        with profile.use("workstation"):
            client.call("extractor", "sys", prompt)
            client.call("extractor", "sys", prompt)
    finally:
        client.current_run.reset(token)
        client._post = real_post
    assert len([p for p in fake_server["requests"] if p["messages"][1]["content"] == prompt]) == 2
    rows = calls_for(run_id)
    assert not any(r["cached"] for r in rows) and all("cut off" in (r["error"] or "") for r in rows)


def test_drawing_reader_dedupes_and_reports_cut_off_tiles(monkeypatch, tmp_path):
    from nodes import drawing_analysis as da

    class Vision:
        def run(self, **kw):
            return OllamaLLM()._extract_json(looping_reply())

    monkeypatch.setattr(da, "_get_vision_llm", lambda role="drawing_reader": Vision())
    tile = {"path": str(tmp_path / "t.png"), "region": "top-left", "box": (0, 0, 10, 10)}
    out = da._extract_tile("prompt", tile, "S-101", "IFC", set(), None, None, ["2050", "3000"])
    assert out["cut_off"] is True
    assert len(out["requirements"]) <= 2                               # 41 reported items, 2 distinct

    class Down:
        def run(self, **kw):
            raise ConnectionError("vision server down")

    monkeypatch.setattr(da, "_get_vision_llm", lambda role="drawing_reader": Down())
    assert da._extract_tile("prompt", tile, "S-101", "IFC", set(), None, None, [])["failed"] is True


def test_assessment_lists_unread_drawing_tiles():
    files = [{"name": "S.pdf", "kind": "drawing", "tiles": 40, "requirements": 30, "cut_off_tiles": 9, "failed_tiles": 2}]
    a = assessment.assess_inputs({"label": "IFC", "requirements": [{}] * 30, "files": files},
                                 {"label": "Spec", "requirements": [{}] * 30, "files": []})
    assert a["ok"]
    text = " ".join(a["limitations"])
    assert "About 5% of the drawing area" in text and "About 22% of the drawing area" in text
    assert "tile" not in text.lower()                                  # said in terms of the drawings


def test_cancel_stops_a_model_call_that_is_still_running(fake_server):
    """'Cancel scan' waited for a call stuck behind a busy model server (seen 26 Nov): it must stop mid-call."""
    import threading
    import pytest
    started, cancelled = threading.Event(), {"v": False}
    real_post = client._post

    def slow_post(server, payload, timeout=900):
        started.set()
        time.sleep(30)                                   # a long prompt / a queued request
        return {"content": "{}", "prompt_tokens": 1, "output_tokens": 1}

    client._post = slow_post
    token = client.cancel_check.set(lambda: cancelled["v"])
    try:
        threading.Timer(0.5, lambda: cancelled.update(v=True)).start()
        t0 = time.time()
        with pytest.raises(client.ScanCancelled), profile.use("workstation"):
            client.call("extractor", "sys", f"long prompt {time.time()}", cache=False)
        assert started.is_set() and time.time() - t0 < 4    # stopped within the poll interval, not after 30 s
    finally:
        client.cancel_check.reset(token)
        client._post = real_post


def test_a_cancelled_parallel_stage_does_not_start_queued_work():
    from fleet import client as fc
    ran = []

    def task(i):
        if i == 0:
            raise fc.ScanCancelled("Cancelled by the user.")
        time.sleep(0.2)
        ran.append(i)

    import pytest
    with pytest.raises(fc.ScanCancelled):
        fc.pmap(task, range(40), workers=2)
    assert len(ran) < 10                                 # the queued tasks were dropped
