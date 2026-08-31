"""`benchkit validate`: schema + invariants + render dry-run + leak guard (contract §3, §4)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from .cases import Case, Problem, _rel, load_cases
from .errors import ContractError
from .manifest import Benchmark
from .render import check_item_spec


@dataclass
class ValidationReport:
    benchmark: str
    cases: int = 0
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def format(self) -> str:
        head = f"{self.benchmark}: {self.cases} case(s), {len(self.problems)} problem(s)"
        if not self.problems:
            return head + " — OK"
        return head + "\n" + "\n".join(f"  - {p}" for p in self.problems)


def leaf_strings(value: Any) -> list[str]:
    """All non-empty leaf string values of a nested structure."""
    out: list[str] = []
    if isinstance(value, str):
        if value:
            out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(leaf_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(leaf_strings(v))
    return out


def leak_guard(case: dict, rendered_input: Any) -> list[str]:
    """Cheap leak guard (contract §3.1): no leaf string of case['world_truth'] may appear
    verbatim inside json.dumps(input). Disabled by `leak_guard: false` on the case."""
    if case.get("leak_guard") is False or "world_truth" not in case:
        return []
    dumped = json.dumps(rendered_input, ensure_ascii=False)
    leaks = []
    for s in leaf_strings(case["world_truth"]):
        escaped = json.dumps(s, ensure_ascii=False)[1:-1]
        if s in dumped or escaped in dumped:
            leaks.append(s)
    return [f"leak guard: world_truth value {s!r} appears in rendered input" for s in leaks]


def _schema_validator(schema: dict) -> jsonschema.Draft202012Validator:
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        raise ContractError(f"oracle_schema is not a valid JSON Schema: {e.message}") from None
    return jsonschema.Draft202012Validator(schema)


def validate_case_schema(validator: jsonschema.Draft202012Validator, case: Case) -> list[str]:
    errs = []
    for e in sorted(validator.iter_errors(case.data), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
        errs.append(f"schema: {loc}: {e.message}")
    return errs


def validate_benchmark(bench: Benchmark) -> ValidationReport:
    report = ValidationReport(benchmark=bench.name)
    cases, problems = load_cases(bench)
    report.problems.extend(problems)
    report.cases = len(cases)

    # Manifest-level resources: surface as a single problem each, then stop (nothing else is meaningful).
    try:
        validator = _schema_validator(bench.load_oracle_schema())
        bench.load_output_schema()
        render_fn = bench.render_fn()
        invariants = bench.invariants()
        bench.evaluators()
    except ContractError as e:
        report.problems.append(Problem(str(bench.manifest_dir / "bench.yaml"), str(e)))
        return report

    for case in cases:
        rel = _rel(case.path, bench)
        errs = validate_case_schema(validator, case)
        for inv in invariants:
            try:
                out = inv(dict(case.data))
            except Exception as e:  # invariant bug -> report, keep going
                errs.append(f"invariant {getattr(inv, '__name__', inv)!r} raised: {type(e).__name__}: {e}")
                continue
            errs.extend(f"invariant: {m}" for m in (out or []))
        # render dry-run + leak guard
        try:
            spec = render_fn(dict(case.data))
        except Exception as e:
            errs.append(f"render() raised: {type(e).__name__}: {e}")
        else:
            spec_errs = check_item_spec(spec, case)
            errs.extend(spec_errs)
            if not spec_errs:
                errs.extend(leak_guard(case.data, spec["input"]))
        report.problems.extend(Problem(rel, m) for m in errs)
    return report
