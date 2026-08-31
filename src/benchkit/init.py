"""`benchkit init`: scaffold a spec repo; `--skills` copies the packaged Claude Code skills."""

from __future__ import annotations

import json
import shutil
from importlib import resources
from pathlib import Path
from typing import Any


def plugin_root() -> Path:
    """Where the packaged plugin lives: `benchkit/_plugin` in a wheel, `plugins/benchkit` in a checkout."""
    try:
        p = Path(str(resources.files("benchkit").joinpath("_plugin")))
        if (p / "skills").is_dir():
            return p
    except Exception:
        pass
    dev = Path(__file__).resolve().parents[2] / "plugins" / "benchkit"
    if (dev / "skills").is_dir():
        return dev
    raise FileNotFoundError("benchkit plugin skills not found (neither packaged nor in a source checkout)")


def copy_skills(target_dir: Path | str) -> list[Path]:
    """Copy plugins/benchkit/skills/* into <target>/.claude/skills/. Overwrites."""
    src = plugin_root() / "skills"
    dest_root = Path(target_dir) / ".claude" / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)
    copied = []
    for skill in sorted(p for p in src.iterdir() if p.is_dir()):
        dest = dest_root / skill.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill, dest)
        copied.append(dest)
    return copied


_ORACLE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "{name} case (oracle) schema — edit me",
    "type": "object",
    "required": ["id", "status", "input", "expected"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z0-9_-]+$"},
        "status": {"type": "string", "enum": ["draft", "reviewed", "gold"]},
        "input": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}},
        "expected": {"type": "object"},
        "world_truth": {"type": "object", "description": "hidden ground truth; must not leak into input"},
        "leak_guard": {"type": "boolean"},
    },
}

_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "{name} model output schema — edit me",
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}, "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
    "additionalProperties": True,
}

_RENDER = '''"""render(case) -> DatasetItemSpec  (benchkit contract §3.1). Pure and deterministic."""


def render(case: dict) -> dict:
    return {
        "id": case["id"],
        "input": {"text": case["input"]["text"]},          # top-level keys = the prompt's {{vars}}
        "expected_output": case["expected"],
        "metadata": {"status": case.get("status"), "tags": case.get("tags", [])},
    }
'''

_EVALUATORS = '''"""Evaluators (benchkit contract §3.2): pure, stdlib-only, never raise on malformed output."""

from benchkit import Evaluation


def answer_matches(*, input, output, expected_output, metadata, **kwargs):
    if not isinstance(output, dict) or "_error" in output:
        return Evaluation(name="answer_matches", value=False, data_type="BOOLEAN", comment="no structured output")
    want = (expected_output or {}).get("answer")
    got = output.get("answer")
    ok = isinstance(got, str) and isinstance(want, str) and got.strip().casefold() == want.strip().casefold()
    return Evaluation(name="answer_matches", value=ok, data_type="BOOLEAN", comment=None if ok else f"got {got!r}")


EVALUATORS = [answer_matches]
'''

_VALIDATE = '''"""Optional invariants (benchkit contract §3.3): each returns a list of error strings."""


def expected_has_answer(case: dict) -> list[str]:
    if "answer" not in (case.get("expected") or {}):
        return ["expected.answer is required"]
    return []


INVARIANTS = [expected_has_answer]
'''

_CASE = """id: example_001
status: draft
tags: [example]
input:
  text: "What is the capital of France?"
expected:
  answer: Paris
  rubric:
    critical: [answer_matches]
world_truth:
  note: "hidden facts go here; the leak guard checks none of these strings appear in the rendered input"
"""

_BENCH_YAML = """benchkit: ">=0.4,<0.5"
langfuse:
  project: {name}
benchmarks:
  {name}:
    path: benchmarks/{name}
    cases: cases/**/*.yaml
    oracle_schema: schema/oracle.schema.json
    output_schema: schema/output.schema.json
    render: benchmarks.{name}.render:render
    evaluators: benchmarks.{name}.evaluators:EVALUATORS
    invariants: benchmarks.{name}.validate:INVARIANTS
    datasets:
      gold:  {{ name: {name}-gold,  select: "status == 'gold'" }}
      draft: {{ name: {name}-draft, select: "status in ('reviewed', 'gold')" }}
      all:   {{ name: {name}-all,   select: "True" }}
    prompts:
      protocol:   {{ name: {name}-protocol, label: production }}
      structured: {{ name: {name}-protocol-structured, label: production, output_schema: true }}
    models:
      default: primary
"""

_PYPROJECT = """[project]
name = "{name}-bench"
version = "0"
requires-python = ">=3.11"
dependencies = [
  "benchkit @ git+https://github.com/dvoid-io/benchkit@v0.4.0",
]

[tool.uv]
package = false

[dependency-groups]
dev = ["pytest>=8"]
"""

_README = """# {name} — benchmark spec repo

Managed with [benchkit](https://github.com/dvoid-io/benchkit) (contract 0.4, see `bench.yaml`).

```sh
uv sync
uv run benchkit validate
uv run benchkit render --out build/render
uv run benchkit sync --benchmark {name} --dataset draft          # with LANGFUSE_* in the environment
uv run benchkit experiment --benchmark {name} --dataset draft --prompt structured
```
"""


def scaffold(target: Path | str, name: str, *, skills: bool = False, force: bool = False) -> list[Path]:
    root = Path(target)
    bdir = root / "benchmarks" / name
    files: dict[Path, str] = {
        root / "bench.yaml": _BENCH_YAML.format(name=name),
        root / "pyproject.toml": _PYPROJECT.format(name=name),
        root / "README.md": _README.format(name=name),
        root / "benchmarks" / "__init__.py": "",
        bdir / "__init__.py": "",
        bdir / "render.py": _RENDER,
        bdir / "evaluators.py": _EVALUATORS,
        bdir / "validate.py": _VALIDATE,
        bdir / "schema" / "oracle.schema.json": json.dumps(
            {**_ORACLE_SCHEMA, "title": _ORACLE_SCHEMA["title"].format(name=name)}, indent=2
        )
        + "\n",
        bdir / "schema" / "output.schema.json": json.dumps(
            {**_OUTPUT_SCHEMA, "title": _OUTPUT_SCHEMA["title"].format(name=name)}, indent=2
        )
        + "\n",
        bdir / "cases" / "example_001.yaml": _CASE,
    }
    written: list[Path] = []
    for path, content in files.items():
        if path.exists() and not force:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written.append(path)
    if skills:
        written.extend(copy_skills(root))
    return written
