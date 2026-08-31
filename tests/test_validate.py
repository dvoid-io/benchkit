from benchkit.validate import leaf_strings, leak_guard, validate_benchmark

from .conftest import load


def _cases_dir(root):
    return next(root.glob("*/toy/cases"))


def _render_py(root):
    return next(root.glob("*/toy/render.py"))


def test_validate_passes(toy):
    rep = validate_benchmark(toy)
    assert rep.ok, rep.format()
    assert rep.cases == 3
    assert "OK" in rep.format()


def test_schema_failure(bench_copy):
    root = bench_copy()
    (_cases_dir(root) / "toy_004.yaml").write_text(
        "id: toy_004\nstatus: shiny\nquestion: 'Why?'\nexpected: {answer: x}\nextra: 1\n"
    )
    rep = validate_benchmark(load(root).benchmarks["toy"])
    assert not rep.ok
    msgs = rep.format()
    assert "toy_004.yaml" in msgs and "schema: status" in msgs and "additional" in msgs.lower()


def test_invariant_failure(bench_copy):
    root = bench_copy()
    (_cases_dir(root) / "toy_004.yaml").write_text(
        "id: toy_004\nstatus: gold\nquestion: 'No question mark'\nexpected: {answer: x}\n"
    )
    rep = validate_benchmark(load(root).benchmarks["toy"])
    assert [p for p in rep.problems if "invariant: question must end with '?'" in p.message]


def test_leak_guard_and_override(bench_copy):
    root = bench_copy()
    (_cases_dir(root) / "toy_004.yaml").write_text(
        "id: toy_004\nstatus: gold\nquestion: 'Is the secret word Paris?'\nexpected: {answer: x}\n"
        "world_truth: {secret: Paris, nested: [{deep: 'secret word'}]}\n"
    )
    (_cases_dir(root) / "toy_005.yaml").write_text(
        "id: toy_005\nstatus: gold\nquestion: 'Is the secret word Paris?'\nexpected: {answer: x}\n"
        "world_truth: {secret: Paris}\nleak_guard: false\n"
    )
    rep = validate_benchmark(load(root).benchmarks["toy"])
    leaks = [p for p in rep.problems if "leak guard" in p.message]
    assert len(leaks) == 2 and all("toy_004" in p.file for p in leaks)
    assert {"'Paris'" in p.message or "'secret word'" in p.message for p in leaks} == {True}


def test_leak_guard_escaped_strings():
    case = {"world_truth": {"q": 'say "hi"'}}
    assert leak_guard(case, {"t": 'he did say "hi" today'}) != []
    assert leak_guard(case, {"t": "nothing"}) == []
    assert leak_guard({"world_truth": {"x": ""}}, {"t": ""}) == []
    assert leaf_strings({"a": ["x", {"b": "y"}], "c": 1, "d": None}) == ["x", "y"]


def test_render_non_json_input(bench_copy):
    root = bench_copy()
    _render_py(root).write_text(
        "def render(case):\n    return {'id': case['id'], 'input': {'q': {1, 2}}, 'expected_output': None}\n"
    )
    rep = validate_benchmark(load(root).benchmarks["toy"])
    assert not rep.ok
    assert all("not JSON-serialisable" in p.message for p in rep.problems), rep.format()


def test_render_wrong_shape_and_raise(bench_copy):
    root = bench_copy()
    _render_py(root).write_text(
        "def render(case):\n"
        "    if case['id'] == 'toy_001': raise ValueError('boom')\n"
        "    if case['id'] == 'toy_002': return {'id': 'other', 'input': {}}\n"
        "    return {'id': case['id'], 'input': 'not a dict'}\n"
    )
    rep = validate_benchmark(load(root).benchmarks["toy"])
    msgs = rep.format()
    assert "render() raised: ValueError: boom" in msgs
    assert "render() id 'other' != case id 'toy_002'" in msgs
    assert "input: 'not a dict' is not of type 'object'" in msgs


def test_manifest_level_problem(bench_copy):
    root = bench_copy()
    next(root.glob("*/toy/schema/oracle.schema.json")).write_text("{ not json")
    rep = validate_benchmark(load(root).benchmarks["toy"])
    assert not rep.ok and "oracle_schema is not valid JSON" in rep.format()
