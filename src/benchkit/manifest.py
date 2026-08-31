"""bench.yaml: locate, parse, validate, resolve (contract §1–§2)."""

from __future__ import annotations

import ast
import importlib
import json
import os
import sys
import warnings
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from . import __version__
from .errors import ContractError, UsageError

MANIFEST_NAME = "bench.yaml"


def _schema() -> dict:
    return json.loads(resources.files("benchkit.schemas").joinpath("bench.schema.json").read_text())


def find_manifest(start: Path | str | None = None) -> Path:
    """Walk up from `start` (default cwd) until a bench.yaml is found."""
    here = Path(start or os.getcwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / MANIFEST_NAME
        if candidate.is_file():
            return candidate
    raise UsageError(f"no {MANIFEST_NAME} found walking up from {here}")


@dataclass(frozen=True)
class DatasetSpec:
    logical: str
    name: str
    select: str
    prefix_ids: bool = False
    description: str | None = None


@dataclass(frozen=True)
class PromptSpec:
    logical: str
    name: str
    label: str | None = None
    version: int | None = None
    type: str | None = None
    output_schema: bool = False
    response_format: str = "json_schema"  # "json_schema" | "none"
    strict: bool = False


@dataclass(frozen=True)
class Benchmark:
    name: str
    manifest_dir: Path
    path: Path
    cases_glob: str
    oracle_schema_path: Path
    output_schema_path: Path
    render_ref: str
    evaluators_ref: str
    invariants_ref: str | None
    datasets: dict[str, DatasetSpec]
    prompts: dict[str, PromptSpec]
    models: dict[str, str] = field(default_factory=dict)

    # -- schema files ------------------------------------------------------
    def load_oracle_schema(self) -> dict:
        return _load_json(self.oracle_schema_path, "oracle_schema")

    def load_output_schema(self) -> dict:
        return _load_json(self.output_schema_path, "output_schema")

    # -- extension points --------------------------------------------------
    def render_fn(self):
        fn = resolve_attr(self.render_ref, self.manifest_dir)
        if not callable(fn):
            raise ContractError(f"{self.name}: render {self.render_ref!r} is not callable")
        return fn

    def evaluators(self) -> list:
        evs = resolve_attr(self.evaluators_ref, self.manifest_dir)
        if not isinstance(evs, (list, tuple)) or not all(callable(e) for e in evs):
            raise ContractError(
                f"{self.name}: evaluators {self.evaluators_ref!r} must be a list of callables"
            )
        return list(evs)

    def invariants(self) -> list:
        if not self.invariants_ref:
            return []
        inv = resolve_attr(self.invariants_ref, self.manifest_dir)
        if not isinstance(inv, (list, tuple)) or not all(callable(i) for i in inv):
            raise ContractError(
                f"{self.name}: invariants {self.invariants_ref!r} must be a list of callables"
            )
        return list(inv)

    def dataset(self, logical: str) -> DatasetSpec:
        try:
            return self.datasets[logical]
        except KeyError:
            raise UsageError(
                f"{self.name}: unknown dataset {logical!r} (have: {', '.join(self.datasets)})"
            ) from None

    def prompt(self, logical: str) -> PromptSpec:
        try:
            return self.prompts[logical]
        except KeyError:
            raise UsageError(
                f"{self.name}: unknown prompt {logical!r} (have: {', '.join(self.prompts) or 'none'})"
            ) from None

    def resolve_model(self, requested: str | None) -> str | None:
        """--model > models.default > $BENCHKIT_MODEL; aliases in `models` resolve one hop."""
        name = requested or self.models.get("default") or os.environ.get("BENCHKIT_MODEL")
        if name is None:
            return None
        seen = {name}
        while name in self.models and self.models[name] not in seen:
            name = self.models[name]
            seen.add(name)
        return name


@dataclass(frozen=True)
class Manifest:
    path: Path
    specifier: str
    langfuse: dict
    benchmarks: dict[str, Benchmark]

    @property
    def dir(self) -> Path:
        return self.path.parent

    def benchmark(self, name: str | None) -> Benchmark:
        """Select by name; if omitted and exactly one benchmark exists, use it."""
        if name is None:
            if len(self.benchmarks) == 1:
                return next(iter(self.benchmarks.values()))
            raise UsageError(
                "--benchmark is required (manifest has: " + ", ".join(self.benchmarks) + ")"
            )
        try:
            return self.benchmarks[name]
        except KeyError:
            raise UsageError(
                f"unknown benchmark {name!r} (manifest has: {', '.join(self.benchmarks)})"
            ) from None

    def select_benchmarks(self, name: str | None) -> list[Benchmark]:
        return [self.benchmark(name)] if name else list(self.benchmarks.values())


def _load_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ContractError(f"{what} not found: {path}") from None
    except json.JSONDecodeError as e:
        raise ContractError(f"{what} is not valid JSON: {path}: {e}") from None
    if not isinstance(data, dict):
        raise ContractError(f"{what} must be a JSON object (a JSON Schema): {path}")
    return data


def check_specifier(specifier: str, own_version: str | None = None) -> None:
    """Refuse (ContractError) a manifest whose specifier excludes our version (contract §7).

    Unversioned dev builds (`0.0.0`, the hatch-vcs fallback) cannot be meaningfully
    checked: warn and continue.
    """
    own = own_version or __version__
    try:
        spec = SpecifierSet(specifier)
    except InvalidSpecifier:
        raise ContractError(f"bench.yaml: invalid `benchkit` specifier {specifier!r}") from None
    try:
        ver = Version(own)
    except InvalidVersion:
        raise ContractError(f"benchkit: own version {own!r} is not PEP 440") from None
    if ver == Version("0.0.0") or ver.is_devrelease:
        warnings.warn(
            f"benchkit {own} is an unreleased dev build; skipping `benchkit: {specifier}` check",
            stacklevel=2,
        )
        return
    if not spec.contains(ver, prereleases=True):
        raise ContractError(
            f"bench.yaml requires benchkit {specifier!r} but this is benchkit {own}"
        )


def parse_manifest(data: Any, path: Path, *, own_version: str | None = None) -> Manifest:
    if not isinstance(data, dict):
        raise ContractError(f"{path}: manifest must be a mapping")
    validator = jsonschema.Draft202012Validator(_schema())
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        lines = [f"{path}: manifest does not match bench.schema.json:"]
        for e in errors:
            loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
            lines.append(f"  - {loc}: {e.message}")
        raise ContractError("\n".join(lines))
    check_specifier(data["benchkit"], own_version)

    manifest_dir = path.parent.resolve()
    benchmarks: dict[str, Benchmark] = {}
    for bname, b in data["benchmarks"].items():
        bpath = (manifest_dir / b["path"]).resolve()
        datasets = {
            k: DatasetSpec(
                logical=k,
                name=v["name"],
                select=v["select"],
                prefix_ids=bool(v.get("prefix_ids", False)),
                description=v.get("description"),
            )
            for k, v in b["datasets"].items()
        }
        for ds in datasets.values():
            compile_select(ds.select)  # fail early on a malformed expression
        prompts = {
            k: PromptSpec(
                logical=k,
                name=v["name"],
                label=v.get("label"),
                version=v.get("version"),
                type=v.get("type"),
                output_schema=bool(v.get("output_schema", False)),
                response_format=str(v.get("response_format", "json_schema")),
                strict=bool(v.get("strict", False)),
            )
            for k, v in (b.get("prompts") or {}).items()
        }
        benchmarks[bname] = Benchmark(
            name=bname,
            manifest_dir=manifest_dir,
            path=bpath,
            cases_glob=b["cases"],
            oracle_schema_path=bpath / b["oracle_schema"],
            output_schema_path=bpath / b["output_schema"],
            render_ref=b["render"],
            evaluators_ref=b["evaluators"],
            invariants_ref=b.get("invariants"),
            datasets=datasets,
            prompts=prompts,
            models=dict(b.get("models") or {}),
        )
    return Manifest(
        path=path,
        specifier=data["benchkit"],
        langfuse=dict(data.get("langfuse") or {}),
        benchmarks=benchmarks,
    )


def load_manifest(path: Path | str | None = None, *, own_version: str | None = None) -> Manifest:
    p = Path(path) if path else find_manifest()
    if p.is_dir():
        p = p / MANIFEST_NAME
    try:
        data = yaml.safe_load(p.read_text())
    except FileNotFoundError:
        raise UsageError(f"manifest not found: {p}") from None
    except yaml.YAMLError as e:
        raise ContractError(f"{p}: invalid YAML: {e}") from None
    return parse_manifest(data, p.resolve(), own_version=own_version)


# --- module:attr resolution ---------------------------------------------------

_ACTIVE_ROOT: str | None = None


def _activate_root(root: str) -> None:
    """Put the manifest dir at sys.path[0]. If a *different* manifest dir was active earlier
    in this process (tests, embedding), drop modules imported from it so package names such
    as `benchmarks` resolve against the new root."""
    global _ACTIVE_ROOT
    if _ACTIVE_ROOT and root != _ACTIVE_ROOT:
        prev = _ACTIVE_ROOT
        for name, mod in list(sys.modules.items()):
            file = getattr(mod, "__file__", None) or ""
            paths = list(getattr(mod, "__path__", []) or [])
            if (file and file.startswith(prev + os.sep)) or any(str(p).startswith(prev + os.sep) for p in paths):
                del sys.modules[name]
        importlib.invalidate_caches()
    if root != _ACTIVE_ROOT:
        importlib.invalidate_caches()
    _ACTIVE_ROOT = root
    if not sys.path or sys.path[0] != root:
        if root in sys.path:
            sys.path.remove(root)
        sys.path.insert(0, root)


def resolve_attr(ref: str, manifest_dir: Path) -> Any:
    """Import `module:attr` with the manifest dir at sys.path[0] (contract §1)."""
    if ":" not in ref:
        raise ContractError(f"bad module:attr reference {ref!r}")
    module_name, attr = ref.split(":", 1)
    root = str(Path(manifest_dir).resolve())
    _activate_root(root)
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ContractError(f"cannot import {module_name!r} (from {ref!r}): {e}") from None
    try:
        return getattr(module, attr)
    except AttributeError:
        raise ContractError(f"module {module_name!r} has no attribute {attr!r}") from None


# --- restricted `select` evaluation ------------------------------------------

_SELECT_BUILTINS: dict[str, Any] = {
    "len": len,
    "any": any,
    "all": all,
    "str": str,
    "int": int,
    "set": set,
    "True": True,
    "False": False,
    "None": None,
}

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Mod,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Tuple,
    ast.List,
    ast.Set,
    ast.Dict,
    ast.Subscript,
    ast.Slice,
    ast.Call,
    ast.IfExp,
    ast.GeneratorExp,
    ast.ListComp,
    ast.comprehension,
    ast.Store,  # comprehension targets
)


def compile_select(expr: str):
    """Parse + whitelist-check a select expression; returns a code object."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ContractError(f"invalid select expression {expr!r}: {e.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ContractError(
                f"select expression {expr!r}: {type(node).__name__} is not allowed"
            )
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name) or node.func.id not in _SELECT_BUILTINS
        ):
            allowed = sorted(k for k in _SELECT_BUILTINS if k[0].islower())
            raise ContractError(f"select expression {expr!r}: only {allowed} may be called")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ContractError(f"select expression {expr!r}: dunder names are not allowed")
    return compile(tree, "<select>", "eval")


class _CaseNamespace(dict):
    """Names are case keys; missing names resolve to None (so `status == 'gold'` is False)."""

    def __missing__(self, key):
        return None


def eval_select(expr: str, case: dict) -> bool:
    code = compile_select(expr)
    ns = _CaseNamespace(_SELECT_BUILTINS)
    ns.update({k: v for k, v in case.items() if isinstance(k, str)})
    try:
        return bool(eval(code, {"__builtins__": {}}, ns))  # noqa: S307 - whitelisted AST
    except Exception as e:
        raise ContractError(
            f"select {expr!r} failed on case {case.get('id')!r}: {type(e).__name__}: {e}"
        ) from None
