"""Project cases into DatasetItemSpecs via the spec repo's render() (contract §3.1)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from .cases import Case, require_cases, select_cases
from .errors import ContractError
from .manifest import Benchmark, DatasetSpec


@dataclass(frozen=True)
class Item:
    id: str
    input: dict
    expected_output: Any
    metadata: dict | None
    case: Case

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "input": self.input,
            "expected_output": self.expected_output,
            "metadata": self.metadata,
        }


def _item_schema() -> dict:
    return json.loads(resources.files("benchkit.schemas").joinpath("item.schema.json").read_text())


_ITEM_VALIDATOR: jsonschema.Draft202012Validator | None = None


def item_validator() -> jsonschema.Draft202012Validator:
    global _ITEM_VALIDATOR
    if _ITEM_VALIDATOR is None:
        _ITEM_VALIDATOR = jsonschema.Draft202012Validator(_item_schema())
    return _ITEM_VALIDATOR


def check_item_spec(spec: Any, case: Case) -> list[str]:
    """Structural checks on what render() returned; returns error strings."""
    errs: list[str] = []
    if not isinstance(spec, dict):
        return [f"render() must return a dict, got {type(spec).__name__}"]
    for e in sorted(item_validator().iter_errors(spec), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
        errs.append(f"render() result: {loc}: {e.message}")
    if errs:
        return errs
    if spec["id"] != case.id:
        errs.append(f"render() id {spec['id']!r} != case id {case.id!r}")
    try:
        json.dumps(spec, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        errs.append(f"render() result is not JSON-serialisable: {e}")
    return errs


def render_case(render_fn, case: Case, dataset: DatasetSpec | None = None) -> Item:
    try:
        spec = render_fn(dict(case.data))
    except Exception as e:  # the spec repo's code; surface as a contract error
        raise ContractError(f"render() raised for case {case.id!r}: {type(e).__name__}: {e}") from e
    errs = check_item_spec(spec, case)
    if errs:
        raise ContractError(f"case {case.id!r}: " + "; ".join(errs))
    item_id = spec["id"]
    if dataset is not None and dataset.prefix_ids:
        item_id = f"{dataset.logical}:{item_id}"
    return Item(
        id=item_id,
        input=spec["input"],
        expected_output=spec.get("expected_output"),
        metadata=spec.get("metadata"),
        case=case,
    )


def render_items(bench: Benchmark, dataset: DatasetSpec | None = None) -> list[Item]:
    """Load + select + render. Raises ContractError on any case/render problem."""
    cases = select_cases(require_cases(bench), dataset)
    render_fn = bench.render_fn()
    return [render_case(render_fn, c, dataset) for c in cases]


def write_items(items: list[Item], out_dir: Path | str) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for item in items:
        safe = item.id.replace("/", "_").replace(":", "_")
        path = out / f"{safe}.json"
        path.write_text(json.dumps(item.as_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        written.append(path)
    return written
