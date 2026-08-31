import yaml
from packaging.specifiers import SpecifierSet

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


def test_scaffold_targets_the_running_benchkit(tmp_path):
    """`benchkit init` must scaffold a repo THIS benchkit can run.

    The specifier it writes into bench.yaml is checked against benchkit's own version on
    every command, so a stale scaffold means `benchkit validate` fails immediately in a
    brand-new repo — the least forgiving place to meet an error.
    """
    scaffold(tmp_path, "mybench")
    specifier = yaml.safe_load((tmp_path / "bench.yaml").read_text())["benchkit"]
    assert SpecifierSet(specifier).contains(__version__, prereleases=True), (
        f"benchkit init scaffolds `benchkit: {specifier!r}`, which excludes this benchkit "
        f"({__version__}) — bump _BENCH_YAML in init.py alongside the contract version"
    )
    assert SpecifierSet(specifier).contains(SCAFFOLD_OWN, prereleases=True)
