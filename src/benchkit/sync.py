"""`benchkit sync`: dataset ensure + item upsert + optional archive of stale items."""

from __future__ import annotations

from . import __version__
from .langfuse_client import (
    SyncPlan,
    archive_items,
    ensure_dataset,
    get_client,
    list_items,
    plan_items,
    upsert_items,
)
from .manifest import Benchmark
from .render import render_items


def sync_dataset(
    bench: Benchmark,
    dataset_logical: str,
    *,
    dry_run: bool = False,
    archive_stale: bool = False,
    client=None,
) -> SyncPlan:
    ds = bench.dataset(dataset_logical)
    items = render_items(bench, ds)
    if dry_run:
        # No network: everything would be sent.
        plan = SyncPlan(dataset=ds.name, new=[i.id for i in items])
        return plan

    client = client or get_client()
    output_schema = bench.load_output_schema()
    ensure_dataset(
        client,
        ds.name,
        expected_output_schema=None,  # expected_output is benchmark-shaped; only the model output has a schema
        input_schema=None,
        description=ds.description or f"benchkit {bench.name}/{ds.logical} (select: {ds.select})",
        metadata={
            "benchkit": __version__,
            "benchmark": bench.name,
            "dataset": ds.logical,
            "output_schema": output_schema.get("$id") or output_schema.get("title"),
        },
    )
    existing = list_items(client, ds.name)
    plan = plan_items(items, existing)
    plan.dataset = ds.name
    to_send = [i for i in items if i.id in set(plan.new) | set(plan.changed)]
    upsert_items(client, ds.name, to_send)
    if archive_stale and plan.stale:
        plan.archived = archive_items(client, ds.name, existing, plan.stale)
    flush = getattr(client, "flush", None)
    if callable(flush):
        flush()
    return plan
