import re
from pathlib import Path

import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from benchkit import __version__
from benchkit.init import copy_skills, plugin_root, scaffold
from benchkit.validate import validate_benchmark

from .conftest import SCAFFOLD_OWN, load


def test_plugin_root_has_skills():
    root = plugin_root()
    assert (root / "skills" / "benchkit-sync" / "SKILL.md").is_file()
    assert (root / "skills" / "benchkit-author-case" / "SKILL.md").is_file()
    assert (root / ".claude-plugin" / "plugin.json").is_file()


def test_scaffold_is_valid_spec_repo(tmp_path):
    written = scaffold(tmp_path, "mybench")
    names = {str(p.relative_to(tmp_path)) for p in written}
    assert {"bench.yaml", "pyproject.toml", "benchmarks/mybench/render.py", "benchmarks/mybench/cases/example_001.yaml"} <= names
    m = load(tmp_path, SCAFFOLD_OWN)
    b = m.benchmarks["mybench"]
    rep = validate_benchmark(b)
    assert rep.ok, rep.format()
    assert b.datasets["gold"].name == "mybench-gold" and b.prompts["structured"].output_schema
    # idempotent: existing files are kept unless --force
    assert scaffold(tmp_path, "mybench") == []
    (tmp_path / "bench.yaml").write_text("broken")
    assert scaffold(tmp_path, "mybench", force=True)
    assert "benchkit:" in (tmp_path / "bench.yaml").read_text()


def test_copy_skills(tmp_path):
    copied = copy_skills(tmp_path)
    assert sorted(p.name for p in copied) == ["benchkit-author-case", "benchkit-sync"]
    assert (tmp_path / ".claude" / "skills" / "benchkit-sync" / "SKILL.md").read_text().startswith("---\nname: benchkit-sync")
    assert copy_skills(tmp_path)  # overwrite is fine
    written = scaffold(tmp_path / "r", "x", skills=True)
    assert any(".claude/skills/benchkit-sync" in str(p) for p in written)


def test_scaffold_targets_the_declared_contract(tmp_path):
    """`benchkit init` must scaffold a repo this benchkit can run.

    The specifier it writes into bench.yaml is checked against benchkit's own version on
    every command, so a stale scaffold means `benchkit validate` fails immediately in a
    brand-new repo — the least forgiving place to meet an error.

    CONTRACT.md is the source of truth for the version, because a working checkout reports
    a dev version derived from the *previous* tag: right after a contract bump and before
    the release is cut, `__version__` legitimately sits below the new floor. A released
    build has no such excuse and is asserted directly.
    """
    scaffold(tmp_path, "mybench")
    specifier = yaml.safe_load((tmp_path / "bench.yaml").read_text())["benchkit"]
    spec = SpecifierSet(specifier)

    contract = (Path(__file__).resolve().parents[1] / "CONTRACT.md").read_text()
    declared = re.search(r"Contract version: `(\d+\.\d+)`", contract).group(1)
    assert spec.contains(f"{declared}.0", prereleases=True), (
        f"benchkit init scaffolds `benchkit: {specifier!r}`, which excludes the contract "
        f"version {declared} declared in CONTRACT.md — bump _BENCH_YAML in init.py with it"
    )
    assert spec.contains(SCAFFOLD_OWN, prereleases=True)

    if not Version(__version__).is_devrelease:
        assert spec.contains(__version__, prereleases=True), (
            f"released benchkit {__version__} does not satisfy its own scaffold {specifier!r}"
        )
