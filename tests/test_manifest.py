import warnings

import pytest

from benchkit.errors import ContractError, UsageError
from benchkit.manifest import (
    Benchmark,
    check_specifier,
    eval_select,
    find_manifest,
    load_manifest,
    parse_manifest,
    resolve_attr,
)

from .conftest import OWN, TOYBENCH, load


def test_find_manifest_walks_up(in_toybench):
    assert find_manifest(TOYBENCH / "benchmarks" / "toy" / "cases") == TOYBENCH / "bench.yaml"
    assert find_manifest() == TOYBENCH / "bench.yaml"


def test_find_manifest_missing(tmp_path):
    with pytest.raises(UsageError):
        find_manifest(tmp_path)


def test_parse_ok(manifest):
    assert manifest.specifier == ">=0.1,<0.2"
    assert manifest.langfuse == {"project": "toy"}
    toy = manifest.benchmarks["toy"]
    assert isinstance(toy, Benchmark)
    assert toy.path == TOYBENCH / "benchmarks" / "toy"
    assert set(toy.datasets) == {"gold", "draft", "all", "tagged"}
    assert toy.datasets["tagged"].prefix_ids is True
    assert toy.prompts["structured"].output_schema is True
    assert toy.prompts["plain"].output_schema is False
    assert manifest.benchmark(None) is toy  # single benchmark → implicit


def test_unknown_benchmark_dataset_prompt(manifest):
    with pytest.raises(UsageError):
        manifest.benchmark("nope")
    toy = manifest.benchmarks["toy"]
    with pytest.raises(UsageError):
        toy.dataset("nope")
    with pytest.raises(UsageError):
        toy.prompt("nope")


def test_resolve_model(toy, monkeypatch):
    monkeypatch.delenv("BENCHKIT_MODEL", raising=False)
    assert toy.resolve_model(None) == "toy-model-1"  # default -> primary -> id
    assert toy.resolve_model("primary") == "toy-model-1"
    assert toy.resolve_model("gpt-x") == "gpt-x"
    monkeypatch.setenv("BENCHKIT_MODEL", "env-model")
    assert toy.resolve_model(None) == "toy-model-1"  # manifest default wins over env
    bare = Benchmark(**{**toy.__dict__, "models": {}})
    assert bare.resolve_model(None) == "env-model"


@pytest.mark.parametrize("own", ["0.2.0", "0.0.9", "1.0.0"])
def test_specifier_refusal(own):
    with pytest.raises(ContractError, match="requires benchkit"):
        check_specifier(">=0.1,<0.2", own)


def test_specifier_ok_and_dev_skip():
    check_specifier(">=0.1,<0.2", "0.1.5")
    check_specifier(">=0.1,<0.2", "0.1.1rc1")
    with pytest.raises(ContractError):
        check_specifier(">=0.1,<0.2", "0.1.0rc1")  # rc precedes the final per PEP 440
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        check_specifier(">=0.1,<0.2", "0.0.0")
        check_specifier(">=0.1,<0.2", "0.0.1.dev3+gabc")
    assert len(w) == 2
    with pytest.raises(ContractError, match="invalid"):
        check_specifier("not a spec", "0.1.0")


def test_manifest_refuses_version(toybench):
    with pytest.raises(ContractError):
        load_manifest(toybench / "bench.yaml", own_version="0.2.0")


def test_schema_errors(tmp_path):
    bad = {"benchkit": ">=0.1,<0.2", "benchmarks": {"x": {"path": "p"}}}
    with pytest.raises(ContractError) as ei:
        parse_manifest(bad, tmp_path / "bench.yaml", own_version=OWN)
    assert "benchmarks/x" in str(ei.value)
    with pytest.raises(ContractError):
        parse_manifest(["not", "a", "mapping"], tmp_path / "bench.yaml", own_version=OWN)
    bad_ref = {
        "benchkit": ">=0.1,<0.2",
        "benchmarks": {
            "x": {
                "path": "p",
                "cases": "c/*.yaml",
                "oracle_schema": "o.json",
                "output_schema": "out.json",
                "render": "no-colon",
                "evaluators": "m:E",
                "datasets": {"a": {"name": "n", "select": "True"}},
                "prompts": {},
            }
        },
    }
    with pytest.raises(ContractError, match="render"):
        parse_manifest(bad_ref, tmp_path / "bench.yaml", own_version=OWN)


def test_bad_select_rejected_at_parse(bench_copy):
    root = bench_copy()
    y = (root / "bench.yaml").read_text().replace('select: "True"', 'select: "__import__(\'os\')"')
    (root / "bench.yaml").write_text(y)
    with pytest.raises(ContractError, match="select"):
        load(root)


@pytest.mark.parametrize(
    "expr,case,expected",
    [
        ("status == 'gold'", {"status": "gold"}, True),
        ("status == 'gold'", {"status": "draft"}, False),
        ("status == 'gold'", {}, False),  # missing name -> None
        ("status in ('reviewed', 'gold')", {"status": "reviewed"}, True),
        ("'math' in tags", {"tags": ["math"]}, True),
        ("len(tags) > 1 and status != 'draft'", {"tags": ["a", "b"], "status": "gold"}, True),
        ("any(t.startswith('m') for t in tags)", {"tags": ["math"]}, None),  # attribute → rejected
        ("True", {}, True),
        ("not archived", {"archived": False}, True),
        ("meta['kind'] == 'x'", {"meta": {"kind": "x"}}, True),
        ("str(n) == '3'", {"n": 3}, True),
    ],
)
def test_eval_select(expr, case, expected):
    if expected is None:
        with pytest.raises(ContractError):
            eval_select(expr, case)
    else:
        assert eval_select(expr, case) is expected


@pytest.mark.parametrize(
    "expr",
    ["__import__('os').system('x')", "(lambda: 1)()", "open('/etc/passwd')", "x.__class__", "import os", "a = 1"],
)
def test_select_rejects_dangerous(expr):
    with pytest.raises(ContractError):
        eval_select(expr, {"x": 1, "a": 1})


def test_resolve_attr(toy):
    fn = resolve_attr("benchmarks.toy.render:render", toy.manifest_dir)
    assert callable(fn)
    with pytest.raises(ContractError):
        resolve_attr("benchmarks.toy.render:nope", toy.manifest_dir)
    with pytest.raises(ContractError):
        resolve_attr("benchmarks.nothing:x", toy.manifest_dir)
    with pytest.raises(ContractError):
        resolve_attr("nocolon", toy.manifest_dir)


def test_prompt_response_format_and_strict(bench_copy):
    root = bench_copy()
    y = (root / "bench.yaml").read_text().replace(
        "    models:\n",
        "      anthropic: { name: toy-a, label: production, output_schema: true, strict: true }\n"
        "      loose: { name: toy-l, label: production, output_schema: true, response_format: none }\n"
        "    models:\n",
    )
    (root / "bench.yaml").write_text(y)
    toy = load(root).benchmarks["toy"]
    assert (toy.prompts["structured"].response_format, toy.prompts["structured"].strict) == ("json_schema", False)
    assert (toy.prompts["anthropic"].response_format, toy.prompts["anthropic"].strict) == ("json_schema", True)
    assert (toy.prompts["loose"].response_format, toy.prompts["loose"].strict) == ("none", False)
    (root / "bench.yaml").write_text(y.replace("response_format: none", "response_format: json_object"))
    with pytest.raises(ContractError, match="response_format"):
        load(root)
