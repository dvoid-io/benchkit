"""`benchkit replay`: re-run the evaluators over an already-recorded run's outputs.

No model call, no Langfuse, no network — the outputs are read from a run export
(`items.jsonl`), the items are re-rendered from the case files, and every evaluator is
re-derived and diffed against the scores that were recorded at run time.

This is the loop for changing how a benchmark judges what it already collected: edit an
evaluator or an oracle, replay, and see exactly which items changed verdict and why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ContractError, UsageError
from .manifest import Benchmark
from .render import Item, render_items

ITEMS_FILE = "items.jsonl"


# ---------------------------------------------------------------------------
# recorded rows


@dataclass(frozen=True)
class Recorded:
    id: str
    output: Any
    scores: dict[str, Any]
    comments: dict[str, str]
    raw: dict


def load_recorded(path: Path | str) -> list[Recorded]:
    """Read a run export: a JSONL file, or a directory containing `items.jsonl`."""
    p = Path(path)
    if p.is_dir():
        p = p / ITEMS_FILE
    if not p.is_file():
        raise UsageError(f"no run export at {path} (expected a .jsonl file or a directory holding {ITEMS_FILE})")
    rows: list[Recorded] = []
    for n, line in enumerate(p.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise UsageError(f"{p}:{n}: not valid JSON: {e}") from None
        if not isinstance(row, dict) or "id" not in row:
            raise UsageError(f"{p}:{n}: every row must be an object with an 'id'")
        rows.append(
            Recorded(
                id=str(row["id"]),
                output=row.get("output"),
                scores=dict(row.get("scores") or {}),
                comments=dict(row.get("comments") or {}),
                raw=row,
            )
        )
    if not rows:
        raise UsageError(f"{p}: no rows")
    return rows


# ---------------------------------------------------------------------------
# evaluation


def _flatten(result: Any) -> list:
    if result is None:
        return []
    return list(result) if isinstance(result, (list, tuple)) else [result]


def evaluate(bench: Benchmark, item: Item, output: Any) -> tuple[list, list[str]]:
    """Run the benchmark's evaluators (plus benchkit's own) over one recorded output.

    Returns (evaluations, errors). An evaluator that raises is reported rather than
    propagated: replay is a development loop, and one broken evaluator should not hide
    the other fifty items' results.
    """
    from .experiment import composite_evaluations, contract_valid

    output_schema = bench.load_output_schema() if any(p.output_schema for p in bench.prompts.values()) else None
    evaluators = ([contract_valid] if output_schema is not None else []) + bench.evaluators()
    kwargs = {
        "input": item.input,
        "output": output,
        "expected_output": item.expected_output,
        "metadata": item.metadata,
    }
    evaluations: list = []
    errors: list[str] = []
    for fn in evaluators:
        try:
            evaluations.extend(_flatten(fn(**kwargs)))
        except Exception as e:  # noqa: BLE001 - the spec repo's code
            errors.append(f"{getattr(fn, '__name__', fn)}: {type(e).__name__}: {e}")
    try:
        evaluations.extend(_flatten(composite_evaluations(evaluations=evaluations, **kwargs)))
    except Exception as e:  # noqa: BLE001
        errors.append(f"composite_evaluations: {type(e).__name__}: {e}")
    return evaluations, errors


def _value(ev) -> Any:
    v = getattr(ev, "value", None)
    dtype = str(getattr(ev, "data_type", "") or "").upper()
    if dtype == "BOOLEAN":
        return bool(v)
    if dtype == "CATEGORICAL":
        return getattr(ev, "string_value", None) or v
    return v


# ---------------------------------------------------------------------------
# replay


@dataclass
class ReplayRow:
    """One recorded item, re-scored.

    `changed` holds only names the run actually recorded AND that we still emit, whose
    value moved — an absent recorded score is "never measured", not "measured as null".
    A name we now emit that was not recorded is `added`; one that was recorded and is no
    longer emitted is `removed`. Both are real signals when an evaluator set is edited,
    and neither is a regression.
    """

    id: str
    scores: dict[str, Any]
    comments: dict[str, str]
    recorded_scores: dict[str, Any]
    changed: dict[str, dict]
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    missing_case: bool = False

    @property
    def moved(self) -> bool:
        return bool(self.changed or self.added or self.removed)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "scores": self.scores,
            "comments": self.comments,
            "recorded_scores": self.recorded_scores,
            "changed": self.changed,
        }
        for key in ("added", "removed", "errors"):
            if getattr(self, key):
                d[key] = getattr(self, key)
        if self.missing_case:
            d["missing_case"] = True
        return d


@dataclass
class ReplayReport:
    run: str
    rows: list[ReplayRow]
    unmatched: list[str]

    @property
    def changed_rows(self) -> list[ReplayRow]:
        return [r for r in self.rows if r.moved]

    def score_names(self) -> list[str]:
        names: set[str] = set()
        for r in self.rows:
            names.update(r.scores)
            names.update(r.recorded_scores)
        return sorted(names)

    def totals(self) -> dict[str, dict]:
        """Per-evaluator booleans: how many were true then, how many now."""
        out: dict[str, dict] = {}
        for name in self.score_names():
            # Compare like with like: only rows that recorded this name can contribute a
            # "was", so a name nobody recorded shows was_n = 0 rather than a phantom delta.
            rows = [r for r in self.rows if isinstance(r.recorded_scores.get(name), bool)]
            was = [r.recorded_scores[name] for r in rows]
            now = [r.scores[name] for r in rows if isinstance(r.scores.get(name), bool)]
            if not was and not now:
                continue
            out[name] = {
                "was": sum(was), "was_n": len(was),
                "now": sum(now), "now_n": len(now),
                "delta": sum(now) - sum(was),
            }
        return out

    def format(self) -> str:
        lines = [f"replay {self.run}: {len(self.rows)} item(s), {len(self.changed_rows)} changed"]
        if self.unmatched:
            lines.append(f"  no case for: {', '.join(self.unmatched)}")
        errored = [r for r in self.rows if r.errors]
        if errored:
            lines.append(f"  evaluator errors on {len(errored)} item(s):")
            for r in errored[:5]:
                lines.append(f"    {r.id}: {r.errors[0]}")
        added = sorted({n for r in self.rows for n in r.added})
        removed = sorted({n for r in self.rows for n in r.removed})
        if added:
            lines.append(f"  newly emitted (not in the recorded run): {', '.join(added)}")
        if removed:
            lines.append(f"  no longer emitted (was recorded): {', '.join(removed)}")
        totals = self.totals()
        moved = {k: v for k, v in totals.items() if v["delta"]}
        if moved:
            lines.append("  evaluator                          was      now    delta")
            for name, t in sorted(moved.items(), key=lambda kv: -abs(kv[1]["delta"])):
                lines.append(f"    {name:<32} {t['was']:>3}/{t['was_n']:<3} {t['now']:>3}/{t['now_n']:<3} {t['delta']:+d}")
        else:
            lines.append("  no evaluator totals moved")
        for r in self.changed_rows[:20]:
            bits = [f"{k} {v['was']}→{v['now']}" for k, v in sorted(r.changed.items())]
            bits += [f"+{n}" for n in r.added] + [f"-{n}" for n in r.removed]
            lines.append(f"    {r.id}: {', '.join(bits)}")
        if len(self.changed_rows) > 20:
            lines.append(f"    … and {len(self.changed_rows) - 20} more")
        return "\n".join(lines)


def replay_run(
    bench: Benchmark,
    run: Path | str,
    *,
    dataset_logical: str | None = None,
    only_changed: bool = False,
) -> ReplayReport:
    recorded = load_recorded(run)
    ds = bench.dataset(dataset_logical) if dataset_logical else None
    items = {i.id: i for i in render_items(bench, ds)}
    if not items:
        raise ContractError(f"{bench.name}: no cases rendered (dataset={dataset_logical!r})")

    rows: list[ReplayRow] = []
    unmatched: list[str] = []
    for rec in recorded:
        item = items.get(rec.id)
        if item is None:
            unmatched.append(rec.id)
            rows.append(ReplayRow(rec.id, {}, {}, rec.scores, {}, missing_case=True))
            continue
        evaluations, errors = evaluate(bench, item, rec.output)
        scores = {ev.name: _value(ev) for ev in evaluations}
        comments = {ev.name: ev.comment for ev in evaluations if getattr(ev, "comment", None)}
        both = set(scores) & set(rec.scores)
        changed = {
            name: {"was": rec.scores[name], "now": scores[name]}
            for name in both
            if _differs(rec.scores[name], scores[name])
        }
        rows.append(
            ReplayRow(
                rec.id, scores, comments, rec.scores, changed,
                added=sorted(set(scores) - set(rec.scores)) if rec.scores else [],
                removed=sorted(set(rec.scores) - set(scores)),
                errors=errors,
            )
        )
    if only_changed:
        rows = [r for r in rows if r.moved or r.errors]
    return ReplayReport(run=str(run), rows=rows, unmatched=unmatched)


def _differs(was: Any, now: Any) -> bool:
    if isinstance(was, (int, float)) and isinstance(now, (int, float)) and not isinstance(was, bool) and not isinstance(now, bool):
        return abs(float(was) - float(now)) > 1e-9
    return was != now


def write_jsonl(report: ReplayReport, out: Path | str | None) -> str:
    text = "".join(json.dumps(r.as_dict(), ensure_ascii=False, sort_keys=True) + "\n" for r in report.rows)
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return text
