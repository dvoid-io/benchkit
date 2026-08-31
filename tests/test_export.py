import json
from types import SimpleNamespace as NS

import httpx
import pytest

from benchkit.errors import ContractError, UsageError
from benchkit.export import (
    export_run,
    fetch_run_scores,
    filter_rows,
    normalize_score,
    score_value,
    shape_rows,
    write_jsonl,
)


def _score(name, value, trace_id, dt="BOOLEAN", string_value=None, comment=None):
    return NS(name=name, value=value, trace_id=trace_id, data_type=dt, string_value=string_value, comment=comment)


def _run():
    return NS(
        id="run-1",
        name="r1",
        dataset_run_items=[
            NS(dataset_item_id="toy_002", trace_id="t2"),
            NS(dataset_item_id="toy_001", trace_id="t1"),
        ],
    )


def _items():
    return {
        "toy_001": NS(id="toy_001", input={"question": "q1"}, expected_output={"answer": "Paris"}, metadata={"status": "gold"}),
        "toy_002": NS(id="toy_002", input={"question": "q2"}, expected_output={"answer": "4"}, metadata=None),
    }


def _scores():
    return [
        _score("protocol_pass", 1, "t1"),
        _score("diagnostic_score", 0.5, "t1", "NUMERIC", comment="1/2"),
        _score("protocol_pass", 0, "t2"),
        _score("label", 0, "t2", "CATEGORICAL", string_value="bad"),
        _score("orphan", 1, "t9"),
    ]


def test_shape_rows():
    rows = shape_rows(_run(), _items(), _scores(), {"t1": {"answer": "Paris"}})
    assert [r["id"] for r in rows] == ["toy_001", "toy_002"]  # sorted by id
    r1, r2 = rows
    assert r1["scores"] == {"protocol_pass": True, "diagnostic_score": 0.5}
    assert r1["comments"] == {"diagnostic_score": "1/2"}
    assert r1["output"] == {"answer": "Paris"} and r1["run_name"] == "r1" and r1["trace_id"] == "t1"
    assert r1["expected_output"] == {"answer": "Paris"} and r1["metadata"] == {"status": "gold"}
    assert r2["scores"] == {"protocol_pass": False, "label": "bad"} and r2["output"] is None


def test_filter_rows_and_jsonl(tmp_path):
    rows = shape_rows(_run(), _items(), _scores())
    assert [r["id"] for r in filter_rows(rows, "protocol_pass == false")] == ["toy_002"]
    assert [r["id"] for r in filter_rows(rows, "protocol_pass == true and diagnostic_score >= 0.5")] == ["toy_001"]
    assert [r["id"] for r in filter_rows(rows, "metadata and metadata['status'] == 'gold'")] == ["toy_001"]
    with pytest.raises(ContractError, match="toy_002"):
        filter_rows(rows, "metadata['status'] == 'gold'")  # None metadata on toy_002
    assert [r["id"] for r in filter_rows(rows, "label == 'bad'")] == ["toy_002"]
    assert filter_rows(rows, None) == rows
    with pytest.raises(ContractError):
        filter_rows(rows, "__import__('os')")
    with pytest.raises(ContractError):
        filter_rows(rows, "scores['x']['y'] == 1")  # runtime error surfaces with row id
    text = write_jsonl(rows, tmp_path / "sub" / "out.jsonl")
    lines = (tmp_path / "sub" / "out.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["id"] == "toy_001" and text.endswith("\n")


class FakeApi:
    def __init__(self, runs, items, scores, scores_by_run_supported=True, traces=None):
        self._runs, self._items, self._scores, self._by_run = runs, items, scores, scores_by_run_supported
        self._traces = traces or {}
        self.datasets = NS(get_run=self._get_run)
        self.dataset_items = NS(list=self._list_items)
        self.scores = NS(get_many=self._get_many)
        self.trace = NS(get=self._get_trace)
        self.get_run_calls = []

    def _get_run(self, dataset_name, run_name):
        self.get_run_calls.append(dataset_name)
        try:
            return self._runs[(dataset_name, run_name)]
        except KeyError:
            raise RuntimeError("404") from None

    def _list_items(self, dataset_name, page, limit):
        return NS(data=list(self._items.values()), meta=NS(total_pages=1))

    def _get_many(self, page, limit, **kw):
        if "dataset_run_id" in kw:
            if not self._by_run:
                raise RuntimeError("400 unknown filter")
            return NS(data=self._scores, meta=NS(total_pages=1))
        tid = kw["trace_id"]
        return NS(data=[s for s in self._scores if s.trace_id == tid], meta=NS(total_pages=1))

    def _get_trace(self, trace_id):
        if trace_id not in self._traces:
            raise RuntimeError("404")
        return NS(output=self._traces[trace_id])


def _rest_transport(routes):
    """routes: {(path_prefix): handler(request) -> Response}; others 404."""

    def handler(request: httpx.Request):
        for prefix, h in routes.items():
            if request.url.path.startswith(prefix):
                return h(request)
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


def _v2_page(request: httpx.Request):
    tid = request.url.params["traceId"]
    page = int(request.url.params["page"])
    assert request.url.params["limit"] == "100"
    assert request.headers["authorization"].startswith("Basic ")
    pages = {
        "t1": {
            1: [{"name": "protocol_pass", "value": 1, "dataType": "BOOLEAN", "traceId": "t1", "observationId": None, "comment": None},
                {"name": "diagnostic_score", "value": 0.5, "dataType": "NUMERIC", "traceId": "t1", "comment": "1/2"}],
            2: [{"name": "label", "value": 0, "stringValue": "good", "dataType": "CATEGORICAL", "traceId": "t1"}],
        },
        "t2": {1: [{"name": "protocol_pass", "value": 0, "dataType": "BOOLEAN", "traceId": "t2"}]},
    }
    data = pages.get(tid, {}).get(page, [])
    return httpx.Response(200, json={"data": data, "meta": {"page": page, "limit": 100, "totalItems": 3, "totalPages": len(pages.get(tid, {1: []}))}})


def test_fetch_run_scores_v2_paginated(capsys):
    http = httpx.Client(transport=_rest_transport({"/api/public/v2/scores": _v2_page}), auth=("pk", "sk"))
    client = NS(api=NS(scores=NS(get_many=lambda **kw: (_ for _ in ()).throw(AssertionError("sdk must not be used")))))
    scores = fetch_run_scores(client, "run-1", ["t1", "t2"], http=http, base_url="http://lf.test/")
    assert [(s.name, s.trace_id) for s in scores] == [("protocol_pass", "t1"), ("diagnostic_score", "t1"), ("label", "t1"), ("protocol_pass", "t2")]
    rows = shape_rows(_run(), _items(), scores)
    assert rows[0]["scores"] == {"protocol_pass": True, "diagnostic_score": 0.5, "label": "good"}
    assert rows[0]["comments"] == {"diagnostic_score": "1/2"}
    assert rows[1]["scores"] == {"protocol_pass": False}
    assert capsys.readouterr().err == ""


def test_fetch_run_scores_falls_back_to_v3_then_sdk(capsys):
    calls = []

    def v3(request):
        calls.append("v3")
        return httpx.Response(200, json={"data": [{"name": "a", "value": 1, "dataType": "BOOLEAN", "traceId": request.url.params["traceId"]}], "meta": {"totalPages": 1}})

    http = httpx.Client(transport=_rest_transport({"/api/public/v2/scores": lambda r: httpx.Response(500, text="boom"), "/api/public/v3/scores": v3}), auth=("pk", "sk"))
    client = NS(api=NS(scores=NS(get_many=lambda **kw: NS(data=[_score("sdk", 0, kw["trace_id"])], meta=NS(total_pages=1)))))
    scores = fetch_run_scores(client, "run-1", ["t1", "t2"], http=http, base_url="http://lf.test")
    assert [(s.name, s.trace_id, s.value) for s in scores] == [("a", "t1", 1), ("a", "t2", 1)]
    err = capsys.readouterr().err
    assert err.count("rest v2") == 1 and "HTTPStatusError" in err and "trying next source" in err  # reported once, then skipped
    # v2 and v3 both dead -> SDK
    http = httpx.Client(transport=_rest_transport({}), auth=("pk", "sk"))
    scores = fetch_run_scores(client, "run-1", ["t1"], http=http, base_url="http://lf.test")
    assert [(s.name, s.trace_id) for s in scores] == [("sdk", "t1")]
    err = capsys.readouterr().err
    assert "rest v2" in err and "rest v3" in err and err.count("failed") == 2
    # everything dead -> empty, one line per source + one per trace
    dead = NS(api=NS(scores=NS(get_many=lambda **kw: (_ for _ in ()).throw(RuntimeError("v4 only")))))
    assert fetch_run_scores(dead, "run-1", ["t1"], http=http, base_url="http://lf.test") == []
    err = capsys.readouterr().err
    assert "sdk (api.scores.get_many) failed (RuntimeError: v4 only)" in err and "no score source succeeded for trace t1" in err


def test_normalize_and_values():
    assert score_value(normalize_score({"name": "b", "value": 1.0, "dataType": "BOOLEAN"})) is True
    assert score_value(normalize_score({"name": "b", "value": 0, "dataType": "BOOLEAN"})) is False
    assert score_value(normalize_score({"name": "c", "value": 0, "stringValue": "x", "dataType": "CATEGORICAL"})) == "x"
    assert score_value(normalize_score({"name": "n", "value": "2", "dataType": "NUMERIC"})) == 2.0
    assert score_value(normalize_score({"name": "n", "value": 3})) == 3.0  # default NUMERIC
    s = normalize_score(_score("x", 1, "t1", comment="hi"))
    assert (s.name, s.trace_id, s.comment, s.data_type) == ("x", "t1", "hi", "BOOLEAN")


def test_export_run_searches_datasets_and_falls_back(toy):
    api = FakeApi({("toy-draft", "r1"): _run()}, _items(), _scores(), scores_by_run_supported=False, traces={"t1": {"answer": "Paris"}})
    client = NS(api=api)
    rows = export_run(toy, run_name="r1", client=client, http=httpx.Client(transport=_rest_transport({})), base_url="http://lf.test")
    assert api.get_run_calls == ["toy-gold", "toy-draft"]
    assert [r["id"] for r in rows] == ["toy_001", "toy_002"]
    assert rows[0]["scores"]["protocol_pass"] is True and rows[0]["output"] == {"answer": "Paris"}
    assert "orphan" not in rows[1]["scores"]
    rows = export_run(toy, run_name="r1", dataset_logical="draft", where="protocol_pass == false", with_outputs=False, client=client, http=httpx.Client(transport=_rest_transport({})), base_url="http://lf.test")
    assert [r["id"] for r in rows] == ["toy_002"] and rows[0]["output"] is None
    with pytest.raises(UsageError, match="not found"):
        export_run(toy, run_name="nope", client=client)
