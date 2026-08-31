import json

import pytest

from benchkit.errors import ContractError
from benchkit.render import render_items, write_items

from .conftest import load


def test_render_items_all_and_selected(toy):
    items = render_items(toy)
    assert [i.id for i in items] == ["toy_001", "toy_002", "toy_003"]
    assert items[0].input == {"question": "What is the capital of France?"}
    assert items[0].expected_output["answer"] == "Paris"
    assert items[0].metadata == {"status": "gold", "tags": ["geo"]}
    assert [i.id for i in render_items(toy, toy.datasets["gold"])] == ["toy_001"]


def test_prefix_ids(toy):
    items = render_items(toy, toy.datasets["tagged"])
    assert [i.id for i in items] == ["tagged:toy_002", "tagged:toy_003"]
    assert items[0].as_dict()["id"] == "tagged:toy_002"


def test_write_items(toy, tmp_path):
    paths = write_items(render_items(toy, toy.datasets["tagged"]), tmp_path / "out")
    assert [p.name for p in paths] == ["tagged_toy_002.json", "tagged_toy_003.json"]
    data = json.loads(paths[0].read_text())
    assert data == {
        "id": "tagged:toy_002",
        "input": {"question": "What is 2 + 2?"},
        "expected_output": {"answer": "4", "rubric": {"critical": ["answer_matches", "confident"]}},
        "metadata": {"status": "reviewed", "tags": ["math"]},
    }


def test_render_items_raises_on_problem(bench_copy):
    root = bench_copy()
    next(root.glob("*/toy/cases")).joinpath("bad.yaml").write_text("- x\n")
    with pytest.raises(ContractError, match="case files have errors"):
        render_items(load(root).benchmarks["toy"])
