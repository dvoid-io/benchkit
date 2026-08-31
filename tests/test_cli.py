import json

from click.testing import CliRunner

from benchkit import __version__
from benchkit.cli import main

from .conftest import TOYBENCH


def run(*args, **kw):
    return CliRunner().invoke(main, list(args), catch_exceptions=False, **kw)


def test_version():
    r = run("--version")
    assert r.exit_code == 0 and r.output.strip() == f"benchkit {__version__}"


def test_validate_ok(in_toybench):
    r = run("validate")
    assert r.exit_code == 0, r.output
    assert "toy: 3 case(s), 0 problem(s) — OK" in r.output
    assert run("validate", "--benchmark", "toy").exit_code == 0
    assert run("validate", "--benchmark", "nope").exit_code == 2


def test_validate_failure_exit_1(bench_copy, monkeypatch):
    root = bench_copy()
    next(root.glob("*/toy/cases")).joinpath("toy_009.yaml").write_text("id: toy_009\nstatus: gold\nquestion: 'no mark'\nexpected: {answer: x}\n")
    monkeypatch.chdir(root)
    r = run("validate")
    assert r.exit_code == 1 and "toy_009.yaml: invariant" in r.output


def test_no_manifest_exit_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = run("validate")
    assert r.exit_code == 2 and "no bench.yaml" in r.output


def test_render_and_sync_dry_run(in_toybench, tmp_path):
    r = run("render", "--out", str(tmp_path / "o"))
    assert r.exit_code == 0 and "wrote 3 item(s)" in r.output
    assert json.loads((tmp_path / "o" / "toy_001.json").read_text())["id"] == "toy_001"
    r = run("render", "-d", "gold", "-o", str(tmp_path / "g"))
    assert r.exit_code == 0 and (tmp_path / "g" / "toy_001.json").exists() and not (tmp_path / "g" / "toy_002.json").exists()
    r = run("sync", "--dry-run", "--benchmark", "toy", "--dataset", "all")
    assert r.exit_code == 0 and "would send 3 item(s)" in r.output and "toy-all" in r.output
    assert run("sync", "--dry-run", "--dataset", "nope").exit_code == 2
    assert run("sync").exit_code == 2  # --dataset required (click usage error)


def test_env_missing_exit_3(in_toybench, clean_env):
    r = run("doctor")
    assert r.exit_code == 3 and "missing: LANGFUSE_BASE_URL" in r.output
    r = run("sync", "--dataset", "gold")
    assert r.exit_code == 3 and "missing environment" in r.output
    r = run("experiment", "--dataset", "gold", "--prompt", "structured")
    assert r.exit_code == 3
    r = run("export", "--run-name", "x")
    assert r.exit_code == 3


def test_experiment_bad_version_exit_2(in_toybench, langfuse_env, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gw")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    r = run("experiment", "--dataset", "gold", "--prompt", "structured", "--version", "yesterday")
    assert r.exit_code == 2 and "ISO 8601" in r.output


def test_init_scaffold_and_skills(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = run("init", "--name", "demo", "--skills")
    assert r.exit_code == 0, r.output
    assert (tmp_path / "bench.yaml").exists()
    assert (tmp_path / ".claude" / "skills" / "benchkit-sync" / "SKILL.md").exists()
    assert (tmp_path / ".claude" / "skills" / "benchkit-author-case" / "SKILL.md").exists()
    # the scaffold validates
    r = run("validate")
    assert r.exit_code == 0, r.output
    r = run("sync", "--dry-run", "--dataset", "all")
    assert r.exit_code == 0 and "would send 1 item(s)" in r.output


def test_toybench_untouched():
    # guard: tests must never write into the fixture
    assert not (TOYBENCH / ".benchkit").exists()


def test_experiment_structured_flags(in_toybench, langfuse_env, monkeypatch):
    r = run("experiment", "--help")
    assert "--response-format" in r.output and "--strict / --no-strict" in r.output
    r = run("experiment", "--dataset", "gold", "--prompt", "structured", "--response-format", "json_object")
    assert r.exit_code == 2  # click choice
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gw")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("BENCHKIT_STRICT", "maybe")
    monkeypatch.setattr("benchkit.experiment.get_client", lambda: (_ for _ in ()).throw(AssertionError("must resolve options first")))
    r = run("experiment", "--dataset", "gold", "--prompt", "structured")
    assert r.exit_code == 2 and "BENCHKIT_STRICT" in r.output
