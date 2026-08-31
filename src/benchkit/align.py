"""Generic oracle <-> model-output alignment helpers (pure functions, stdlib only).

These are building blocks for spec-repo evaluators that follow the "oracle style":
unresolved *variables* (a dimension with a candidate set), *hypotheses* (assignment
vectors over variables), *partitions* (a grouping of items into equivalence classes) and
structured *propositions* `{pred, args, value}`. Nothing here knows any particular
benchmark; identity is decided purely by content, so model-side ids never need to
match oracle-side ids.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# normalisation


def norm_str(value: Any) -> str:
    """Case-insensitive, whitespace-collapsed string form used for all comparisons."""
    if value is None:
        return ""
    return " ".join(str(value).split()).casefold()


def norm_set(values: Iterable[Any] | None) -> frozenset[str]:
    return frozenset(norm_str(v) for v in (values or []))


# ---------------------------------------------------------------------------
# results


@dataclass
class Matching:
    """Result of a one-to-one matching between model-side and oracle-side objects."""

    pairs: list[tuple[int, int]] = field(default_factory=list)  # (model_index, oracle_index)
    unmatched_model: list[int] = field(default_factory=list)
    unmatched_oracle: list[int] = field(default_factory=list)

    @property
    def model_to_oracle(self) -> dict[int, int]:
        return dict(self.pairs)

    @property
    def oracle_to_model(self) -> dict[int, int]:
        return {o: m for m, o in self.pairs}

    @property
    def complete(self) -> bool:
        return not self.unmatched_model and not self.unmatched_oracle

    @property
    def precision(self) -> float:
        n = len(self.pairs) + len(self.unmatched_model)
        return len(self.pairs) / n if n else 1.0

    @property
    def recall(self) -> float:
        n = len(self.pairs) + len(self.unmatched_oracle)
        return len(self.pairs) / n if n else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def _match_by_key(model: Sequence[Any], oracle: Sequence[Any], key) -> Matching:
    """Greedy one-to-one matching on a hashable key; first-come within equal keys."""
    m = Matching()
    buckets: dict[Hashable, list[int]] = {}
    for j, o in enumerate(oracle):
        buckets.setdefault(key(o), []).append(j)
    for i, x in enumerate(model):
        k = key(x)
        if buckets.get(k):
            m.pairs.append((i, buckets[k].pop(0)))
        else:
            m.unmatched_model.append(i)
    matched_o = {j for _, j in m.pairs}
    m.unmatched_oracle = [j for j in range(len(oracle)) if j not in matched_o]
    return m


# ---------------------------------------------------------------------------
# variables


def variable_key(var: Mapping[str, Any], *, dim_key: str = "dimension", cand_key: str = "candidates") -> tuple:
    """Identity of a variable = (normalised dimension, set of normalised candidates)."""
    return (norm_str(var.get(dim_key)), norm_set(var.get(cand_key)))


def match_variables(
    model_vars: Sequence[Mapping[str, Any]],
    oracle_vars: Sequence[Mapping[str, Any]],
    *,
    dim_key: str = "dimension",
    cand_key: str = "candidates",
) -> Matching:
    """Match variables by dimension + candidate-set equality (ids are ignored)."""
    return _match_by_key(
        model_vars, oracle_vars, lambda v: variable_key(v, dim_key=dim_key, cand_key=cand_key)
    )


def variable_id_map(
    model_vars: Sequence[Mapping[str, Any]],
    oracle_vars: Sequence[Mapping[str, Any]],
    matching: Matching,
    *,
    id_key: str = "id",
) -> dict[str, str]:
    """model variable id -> oracle variable id for matched pairs."""
    return {
        str(model_vars[i].get(id_key)): str(oracle_vars[j].get(id_key)) for i, j in matching.pairs
    }


# ---------------------------------------------------------------------------
# hypotheses (assignment vectors)


def assignment_key(
    hyp: Mapping[str, Any],
    *,
    var_map: Mapping[str, str] | None = None,
    assign_key: str = "assignments",
) -> frozenset[tuple[str, str]]:
    """Identity of a hypothesis = set of (variable id, chosen candidate), both normalised.
    `var_map` renames model variable ids into oracle ids first; unmapped ids pass through."""
    assignments = hyp.get(assign_key) or {}
    pairs: list[tuple[Any, Any]]
    if isinstance(assignments, Mapping):
        pairs = list(assignments.items())
    else:  # list of {variable, value} / {var, candidate}
        pairs = []
        for a in assignments:
            if isinstance(a, Mapping):
                v = a.get("variable", a.get("var", a.get("id")))
                val = a.get("value", a.get("candidate"))
                pairs.append((v, val))
    out = set()
    for v, val in pairs:
        vid = str(v)
        if var_map and vid in var_map:
            vid = var_map[vid]
        out.add((norm_str(vid), norm_str(val)))
    return frozenset(out)


def match_hypotheses(
    model_hyps: Sequence[Mapping[str, Any]],
    oracle_hyps: Sequence[Mapping[str, Any]],
    *,
    var_map: Mapping[str, str] | None = None,
    assign_key: str = "assignments",
) -> Matching:
    """Match hypotheses by equal assignment vectors (after mapping model var ids to oracle ids)."""
    return _match_by_key(
        model_hyps,
        oracle_hyps,
        lambda h: assignment_key(h, var_map=var_map, assign_key=assign_key),
    )


# ---------------------------------------------------------------------------
# partitions


def _blocks(partition: Iterable[Iterable[Any]]) -> list[frozenset[str]]:
    return [frozenset(norm_str(x) for x in block) for block in partition]


def partition_signature(partition: Iterable[Iterable[Any]]) -> tuple[int, ...]:
    """Sorted block sizes — the label-free shape of a partition."""
    return tuple(sorted((len(b) for b in _blocks(partition)), reverse=True))


def partition_isomorphic(p1: Iterable[Iterable[Any]], p2: Iterable[Iterable[Any]]) -> bool:
    """True iff the partitions have the same shape (same multiset of block sizes):
    a relabelling of blocks/elements could make them equal. Use `partitions_equal`
    when both sides partition the *same* universe (e.g. turn indices)."""
    return partition_signature(p1) == partition_signature(p2)


def partitions_equal(p1: Iterable[Iterable[Any]], p2: Iterable[Iterable[Any]]) -> bool:
    """Same universe, same blocks (block order and element order irrelevant)."""
    return frozenset(_blocks(p1)) == frozenset(_blocks(p2))


def partition_from_assignment(assignment: Mapping[Any, Any]) -> list[frozenset[str]]:
    """{element: label} -> list of blocks (elements grouped by label)."""
    groups: dict[str, set[str]] = {}
    for el, label in assignment.items():
        groups.setdefault(norm_str(label), set()).add(norm_str(el))
    return [frozenset(g) for g in groups.values()]


# ---------------------------------------------------------------------------
# propositions {pred, args, value}


def proposition_key(prop: Mapping[str, Any]) -> tuple:
    pred = norm_str(prop.get("pred", prop.get("predicate")))
    args = prop.get("args", prop.get("arguments", []))
    if isinstance(args, Mapping):
        args_key: tuple = tuple(sorted((norm_str(k), norm_str(v)) for k, v in args.items()))
    elif isinstance(args, (list, tuple)):
        args_key = tuple(norm_str(a) for a in args)
    else:
        args_key = (norm_str(args),)
    value = prop.get("value", True)
    if isinstance(value, str):
        value_key: Any = norm_str(value)
    else:
        value_key = value
    return (pred, args_key, value_key)


def proposition_equal(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Structured equality: same predicate, same args (order-sensitive for lists,
    order-free for mappings), same value (default True). Strings compare normalised."""
    return proposition_key(a) == proposition_key(b)


def match_propositions(
    model_props: Sequence[Mapping[str, Any]], oracle_props: Sequence[Mapping[str, Any]]
) -> Matching:
    return _match_by_key(model_props, oracle_props, proposition_key)


__all__ = [
    "Matching",
    "assignment_key",
    "match_hypotheses",
    "match_propositions",
    "match_variables",
    "norm_set",
    "norm_str",
    "partition_from_assignment",
    "partition_isomorphic",
    "partition_signature",
    "partitions_equal",
    "proposition_equal",
    "proposition_key",
    "variable_id_map",
    "variable_key",
]
