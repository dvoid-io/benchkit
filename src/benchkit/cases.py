"""Case files: glob, parse (YAML/JSON), structural checks (contract §2)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import ContractError
from .manifest import Benchmark, DatasetSpec, eval_select


@dataclass(frozen=True)
class Case:
    path: Path
    data: dict

    @property
    def id(self) -> str:
        return str(self.data["id"])


@dataclass(frozen=True)
class Problem:
    file: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}: {self.message}"


def _parse_file(path: Path):
    text = path.read_text()
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def case_files(bench: Benchmark) -> list[Path]:
    root = bench.path
    return sorted(p for p in root.glob(bench.cases_glob) if p.is_file())


def load_cases(bench: Benchmark) -> tuple[list[Case], list[Problem]]:
    """Load every case file; return (cases, problems). Structural errors do not raise."""
    cases: list[Case] = []
    problems: list[Problem] = []
    seen: dict[str, Path] = {}
    files = case_files(bench)
    if not files:
        problems.append(
            Problem(str(bench.path), f"no case files match {bench.cases_glob!r}")
        )
    for path in files:
        rel = _rel(path, bench)
        try:
            data = _parse_file(path)
        except (yaml.YAMLError, json.JSONDecodeError) as e:
            problems.append(Problem(rel, f"cannot parse: {e}"))
            continue
        except OSError as e:
            problems.append(Problem(rel, f"cannot read: {e}"))
            continue
        if not isinstance(data, dict):
            problems.append(Problem(rel, "case file must be a mapping (one case per file)"))
            continue
        cid = data.get("id")
        if not isinstance(cid, str) or not cid.strip():
            problems.append(Problem(rel, "case must have a non-empty string `id`"))
            continue
        if cid in seen:
            problems.append(
                Problem(rel, f"duplicate id {cid!r} (also in {_rel(seen[cid], bench)})")
            )
            continue
        seen[cid] = path
        cases.append(Case(path=path, data=data))
    return cases, problems


def require_cases(bench: Benchmark) -> list[Case]:
    cases, problems = load_cases(bench)
    if problems:
        raise ContractError(
            f"{bench.name}: case files have errors:\n" + "\n".join(f"  - {p}" for p in problems)
        )
    return cases


def select_cases(cases: list[Case], dataset: DatasetSpec | None) -> list[Case]:
    if dataset is None:
        return list(cases)
    return [c for c in cases if eval_select(dataset.select, c.data)]


def _rel(path: Path, bench: Benchmark) -> str:
    try:
        return str(path.relative_to(bench.manifest_dir))
    except ValueError:
        return str(path)
