import json

from benchkit.cases import load_cases, select_cases

from .conftest import load


def test_load_cases_ok(toy):
    cases, problems = load_cases(toy)
    assert problems == []
    assert [c.id for c in cases] == ["toy_001", "toy_002", "toy_003"]


def test_select_cases(toy):
    cases, _ = load_cases(toy)
    assert [c.id for c in select_cases(cases, toy.datasets["gold"])] == ["toy_001"]
    assert [c.id for c in select_cases(cases, toy.datasets["draft"])] == ["toy_001", "toy_002"]
    assert [c.id for c in select_cases(cases, toy.datasets["tagged"])] == ["toy_002", "toy_003"]
    assert len(select_cases(cases, None)) == 3


def test_structural_problems(bench_copy):
    root = bench_copy()
    cdir = next(root.glob("*/toy/cases"))
    (cdir / "toy_010.yaml").write_text("- not\n- a mapping\n")
    (cdir / "toy_011.yaml").write_text("status: gold\nquestion: 'x?'\n")  # no id
    (cdir / "toy_012.yaml").write_text("id: toy_001\nstatus: gold\nquestion: 'dup?'\nexpected: {answer: a}\n")
    (cdir / "toy_013.yaml").write_text("id: [unclosed\n")
    toy = load(root).benchmarks["toy"]
    cases, problems = load_cases(toy)
    assert [c.id for c in cases] == ["toy_001", "toy_002", "toy_003"]
    msgs = "\n".join(str(p) for p in problems)
    assert "toy_010.yaml: case file must be a mapping" in msgs
    assert "toy_011.yaml: case must have a non-empty string `id`" in msgs
    assert "duplicate id 'toy_001'" in msgs
    assert "toy_013.yaml: cannot parse" in msgs


def test_json_case_and_glob(bench_copy):
    root = bench_copy()
    y = (root / "bench.yaml").read_text().replace("cases: cases/**/*.yaml", "cases: cases/**/*.*")
    (root / "bench.yaml").write_text(y)
    cdir = next(root.glob("*/toy/cases"))
    (cdir / "sub").mkdir()
    (cdir / "sub" / "toy_004.json").write_text(
        json.dumps({"id": "toy_004", "status": "gold", "question": "Why?", "expected": {"answer": "because"}})
    )
    toy = load(root).benchmarks["toy"]
    cases, problems = load_cases(toy)
    assert problems == []
    assert "toy_004" in [c.id for c in cases]


def test_no_files(bench_copy):
    root = bench_copy()
    y = (root / "bench.yaml").read_text().replace("cases: cases/**/*.yaml", "cases: nothing/*.yaml")
    (root / "bench.yaml").write_text(y)
    toy = load(root).benchmarks["toy"]
    cases, problems = load_cases(toy)
    assert cases == [] and "no case files match" in problems[0].message
