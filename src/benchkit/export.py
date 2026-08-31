"""`benchkit export`: one JSONL row per dataset-run item with its scores."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .errors import ContractError, UsageError
from .langfuse_client import get_client, langfuse_env, list_items
from .manifest import Benchmark, compile_select


@dataclass(frozen=True)
class ScoreRow:
    """Normalised score (from raw REST JSON or an SDK object)."""

    name: str
    value: Any
    string_value: str | None
    data_type: str
    comment: str | None
    trace_id: str | None
    observation_id: str | None


def normalize_score(raw: Any) -> ScoreRow:
    if isinstance(raw, dict):
        g = lambda camel, snake: raw.get(camel, raw.get(snake))  # noqa: E731
    else:
        g = lambda camel, snake: getattr(raw, snake, None)  # noqa: E731
    return ScoreRow(
        name=str(g("name", "name")),
        value=g("value", "value"),
        string_value=g("stringValue", "string_value"),
        data_type=str(g("dataType", "data_type") or "NUMERIC").upper(),
        comment=g("comment", "comment"),
        trace_id=g("traceId", "trace_id"),
        observation_id=g("observationId", "observation_id"),
    )


def score_value(s: ScoreRow) -> Any:
    """BOOLEAN -> bool from numeric 0/1; CATEGORICAL -> stringValue; NUMERIC -> float."""
    if s.data_type == "BOOLEAN":
        if s.value is None:
            return None
        try:
            return bool(float(s.value))
        except (TypeError, ValueError):
            return str(s.value).strip().lower() in ("true", "1")
    if s.data_type == "CATEGORICAL":
        return s.string_value if s.string_value is not None else s.value
    if s.value is None:
        return None
    try:
        return float(s.value)
    except (TypeError, ValueError):
        return s.value


def _paginate(call, **kwargs) -> list:
    out: list = []
    page = 1
    while True:
        resp = call(page=page, limit=100, **kwargs)
        data = list(getattr(resp, "data", []) or [])
        out.extend(data)
        meta = getattr(resp, "meta", None)
        total_pages = getattr(meta, "total_pages", None) if meta is not None else None
        if not data or total_pages is None or page >= total_pages:
            break
        page += 1
    return out


def _log(msg: str) -> None:
    print(f"benchkit: export: {msg}", file=sys.stderr)


def _rest_scores(http: httpx.Client, base_url: str, path: str, trace_id: str) -> list[dict]:
    """GET {base}/api/public/<path>/scores?traceId=&page=&limit=100, paginated on meta.totalPages."""
    out: list[dict] = []
    page = 1
    while True:
        resp = http.get(
            f"{base_url.rstrip('/')}/api/public/{path}/scores",
            params={"traceId": trace_id, "page": page, "limit": 100},
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") or []
        out.extend(d for d in data if isinstance(d, dict))
        meta = body.get("meta") or {}
        total_pages = meta.get("totalPages")
        if not data or total_pages is None or page >= int(total_pages):
            break
        page += 1
    return out


def fetch_run_scores(
    client,
    run_id: str,
    trace_ids: list[str],
    *,
    http: httpx.Client | None = None,
    base_url: str | None = None,
    auth: tuple[str, str] | None = None,
) -> list[ScoreRow]:
    """Scores for each trace of a run. Sources in order: raw REST v2, raw REST v3, SDK
    `client.api.scores.get_many(trace_id=)` (SDK v4 targets the v4 API and returns nothing
    against a 3.x server). A source that fails is reported once on stderr and skipped."""
    env = langfuse_env()
    base_url = base_url or env["LANGFUSE_BASE_URL"] or ""
    auth = auth or ((env["LANGFUSE_PUBLIC_KEY"] or "", env["LANGFUSE_SECRET_KEY"] or ""))
    own_http = http is None
    http = http or httpx.Client(timeout=60.0, auth=auth)
    dead: set[str] = set()
    scores: list[ScoreRow] = []

    def sources(tid: str):
        yield "rest v2 (/api/public/v2/scores)", lambda: _rest_scores(http, base_url, "v2", tid)
        yield "rest v3 (/api/public/v3/scores)", lambda: _rest_scores(http, base_url, "v3", tid)
        yield "sdk (api.scores.get_many)", lambda: _paginate(client.api.scores.get_many, trace_id=tid)

    try:
        for tid in trace_ids:
            got = None
            for label, call in sources(tid):
                if label in dead:
                    continue
                try:
                    got = [normalize_score(r) for r in call()]
                    break
                except Exception as e:  # report once per source, then fall through
                    dead.add(label)
                    _log(f"scores via {label} failed ({type(e).__name__}: {e}); trying next source")
            if got is None:
                _log(f"no score source succeeded for trace {tid}")
                continue
            scores.extend(got)
    finally:
        if own_http:
            http.close()
    return scores


def fetch_trace_output(client, trace_id: str) -> Any:
    try:
        trace = client.api.trace.get(trace_id)
    except Exception:
        return None
    return getattr(trace, "output", None)


def shape_rows(run, items_by_id: dict, scores: list, outputs: dict[str, Any] | None = None) -> list[dict]:
    """Pure: join run items, dataset items, scores (by trace_id) and outputs into rows."""
    by_trace: dict[str, list[ScoreRow]] = {}
    for raw in scores:
        s = raw if isinstance(raw, ScoreRow) else normalize_score(raw)
        if s.trace_id:
            by_trace.setdefault(s.trace_id, []).append(s)
    rows: list[dict] = []
    for ri in getattr(run, "dataset_run_items", []) or []:
        item = items_by_id.get(ri.dataset_item_id)
        tid = ri.trace_id
        sc: dict[str, Any] = {}
        comments: dict[str, str] = {}
        for s in by_trace.get(tid, []):
            sc[s.name] = score_value(s)
            if s.comment:
                comments[s.name] = s.comment
        rows.append(
            {
                "id": ri.dataset_item_id,
                "run_name": getattr(run, "name", None),
                "trace_id": tid,
                "input": getattr(item, "input", None) if item is not None else None,
                "expected_output": getattr(item, "expected_output", None) if item is not None else None,
                "metadata": getattr(item, "metadata", None) if item is not None else None,
                "output": (outputs or {}).get(tid),
                "scores": sc,
                "comments": comments,
            }
        )
    rows.sort(key=lambda r: str(r["id"]))
    return rows


class _RowNamespace(dict):
    def __missing__(self, key):
        return None


def filter_rows(rows: list[dict], where: str | None) -> list[dict]:
    """Restricted expression over the row: score names are top-level names, plus
    id/scores/metadata/input/expected_output/output and true/false/null literals."""
    if not where:
        return rows
    code = compile_select(where)
    out = []
    for r in rows:
        ns = _RowNamespace({"true": True, "false": False, "null": None, "len": len, "any": any, "all": all, "str": str, "int": int, "set": set})
        ns.update(r.get("scores") or {})
        ns.update({k: r.get(k) for k in ("id", "scores", "metadata", "input", "expected_output", "output", "trace_id", "comments")})
        try:
            keep = bool(eval(code, {"__builtins__": {}}, ns))  # noqa: S307 - whitelisted AST
        except Exception as e:
            raise ContractError(f"--where {where!r} failed on row {r.get('id')!r}: {e}") from None
        if keep:
            out.append(r)
    return out


def write_jsonl(rows: list[dict], out: Path | str | None) -> str:
    text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return text


def export_run(
    bench: Benchmark,
    *,
    run_name: str,
    dataset_logical: str | None = None,
    where: str | None = None,
    with_outputs: bool = True,
    client=None,
    http: httpx.Client | None = None,
    base_url: str | None = None,
) -> list[dict]:
    client = client or get_client()
    candidates = [bench.dataset(dataset_logical)] if dataset_logical else list(bench.datasets.values())
    run = None
    ds = None
    last_err: Exception | None = None
    for ds in candidates:
        try:
            run = client.api.datasets.get_run(ds.name, run_name)
            break
        except Exception as e:
            last_err = e
            run = None
    if run is None or ds is None:
        raise UsageError(
            f"run {run_name!r} not found in dataset(s) {[d.name for d in candidates]}"
            + (f": {last_err}" if last_err else "")
        )
    items_by_id = {i.id: i for i in list_items(client, ds.name)}
    trace_ids = [ri.trace_id for ri in (run.dataset_run_items or []) if ri.trace_id]
    scores = fetch_run_scores(client, run.id, trace_ids, http=http, base_url=base_url)
    outputs = {tid: fetch_trace_output(client, tid) for tid in trace_ids} if with_outputs else {}
    rows = shape_rows(run, items_by_id, scores, outputs)
    return filter_rows(rows, where)
