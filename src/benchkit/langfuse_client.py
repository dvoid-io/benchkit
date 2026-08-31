"""Thin Langfuse SDK v4 wrapper: env handling + the few dataset calls benchkit needs.

Everything that touches the network lives here (and in experiment/export via the
client object), so tests can substitute a fake client.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .errors import EnvError

ENV_BASE_URL = "LANGFUSE_BASE_URL"
ENV_HOST_FALLBACK = "LANGFUSE_HOST"
ENV_PUBLIC_KEY = "LANGFUSE_PUBLIC_KEY"
ENV_SECRET_KEY = "LANGFUSE_SECRET_KEY"

ENV_OPENAI_BASE_URL = ("OPENAI_BASE_URL", "LITELLM_BASE_URL")
ENV_OPENAI_API_KEY = ("OPENAI_API_KEY", "LITELLM_VIRTUAL_KEY")


def langfuse_env() -> dict[str, str | None]:
    """Resolved Langfuse env (applies the LANGFUSE_HOST fallback). Values may be None."""
    base = os.environ.get(ENV_BASE_URL) or os.environ.get(ENV_HOST_FALLBACK)
    return {
        ENV_BASE_URL: base,
        ENV_PUBLIC_KEY: os.environ.get(ENV_PUBLIC_KEY),
        ENV_SECRET_KEY: os.environ.get(ENV_SECRET_KEY),
    }


def missing_langfuse_env() -> list[str]:
    return [k for k, v in langfuse_env().items() if not v]


def first_env(names: tuple[str, ...]) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def gateway_env() -> tuple[str | None, str | None]:
    """(OPENAI_BASE_URL, OPENAI_API_KEY) with the LITELLM_* fallbacks."""
    return first_env(ENV_OPENAI_BASE_URL), first_env(ENV_OPENAI_API_KEY)


def get_client(**kwargs: Any):
    """Construct a `langfuse.Langfuse` from the environment; EnvError if incomplete."""
    missing = missing_langfuse_env()
    if missing:
        raise EnvError(
            "missing environment: "
            + ", ".join(missing)
            + f" (LANGFUSE_BASE_URL falls back to {ENV_HOST_FALLBACK}); "
            "export them, or run under your secret manager's exec wrapper"
        )
    env = langfuse_env()
    os.environ.setdefault(ENV_BASE_URL, env[ENV_BASE_URL] or "")
    from langfuse import Langfuse

    return Langfuse(
        public_key=env[ENV_PUBLIC_KEY],
        secret_key=env[ENV_SECRET_KEY],
        base_url=env[ENV_BASE_URL],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# datasets


def _is_not_found(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return status == 404 or type(exc).__name__ == "NotFoundError"


def get_dataset_meta(client, name: str):
    """The Dataset record (schemas etc.) or None if it does not exist."""
    try:
        return client.api.datasets.get(name)
    except Exception as e:
        if _is_not_found(e):
            return None
        raise


def ensure_dataset(
    client,
    name: str,
    *,
    input_schema: dict | None = None,
    expected_output_schema: dict | None = None,
    description: str | None = None,
    metadata: dict | None = None,
):
    """Create the dataset if missing, or re-upsert it when the schemas differ.
    POST /datasets upserts by name in Langfuse, so this is idempotent."""
    existing = get_dataset_meta(client, name)
    if existing is not None:
        same = (getattr(existing, "input_schema", None) or None) == (input_schema or None) and (
            getattr(existing, "expected_output_schema", None) or None
        ) == (expected_output_schema or None)
        if same:
            return existing, False
        description = description or getattr(existing, "description", None)
        metadata = metadata or getattr(existing, "metadata", None)
    created = client.create_dataset(
        name=name,
        description=description,
        metadata=metadata,
        input_schema=input_schema,
        expected_output_schema=expected_output_schema,
    )
    return created, True


def list_items(client, dataset_name: str, *, page_size: int = 100) -> list:
    """All DatasetItem records of a dataset (paginated)."""
    out: list = []
    page = 1
    while True:
        resp = client.api.dataset_items.list(dataset_name=dataset_name, page=page, limit=page_size)
        data = list(getattr(resp, "data", []) or [])
        out.extend(data)
        meta = getattr(resp, "meta", None)
        total_pages = getattr(meta, "total_pages", None) if meta is not None else None
        if not data or total_pages is None or page >= total_pages:
            break
        page += 1
    return out


@dataclass
class SyncPlan:
    dataset: str
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)  # in Langfuse (ACTIVE) but not selected
    archived: list[str] = field(default_factory=list)  # stale items actually archived

    def format(self) -> str:
        return (
            f"dataset {self.dataset!r}: new={len(self.new)} changed={len(self.changed)} "
            f"unchanged={len(self.unchanged)} stale={len(self.stale)} archived={len(self.archived)}"
        )


def _same(a: Any, b: Any) -> bool:
    return (a if a not in ({}, []) else None) == (b if b not in ({}, []) else None)


def plan_items(items: list, existing: list) -> SyncPlan:
    """Diff rendered items against what Langfuse holds; pure."""
    plan = SyncPlan(dataset="")
    by_id = {e.id: e for e in existing}
    wanted = set()
    for it in items:
        wanted.add(it.id)
        e = by_id.get(it.id)
        if e is None:
            plan.new.append(it.id)
        elif (
            _same(e.input, it.input)
            and _same(e.expected_output, it.expected_output)
            and _same(e.metadata, it.metadata)
            and str(getattr(e, "status", "ACTIVE") or "ACTIVE").upper() == "ACTIVE"
        ):
            plan.unchanged.append(it.id)
        else:
            plan.changed.append(it.id)
    for e in existing:
        if e.id not in wanted and str(getattr(e, "status", "ACTIVE") or "ACTIVE").upper() == "ACTIVE":
            plan.stale.append(e.id)
    return plan


def upsert_items(client, dataset_name: str, items: list) -> int:
    """Upsert by id (Langfuse upserts dataset items on id) and force status ACTIVE."""
    n = 0
    for it in items:
        client.create_dataset_item(
            dataset_name=dataset_name,
            id=it.id,
            input=it.input,
            expected_output=it.expected_output,
            metadata=it.metadata,
            status="ACTIVE",
        )
        n += 1
    return n


def archive_items(client, dataset_name: str, existing: list, ids: list[str]) -> list[str]:
    """Set status ARCHIVED on the given items, preserving their payload."""
    by_id = {e.id: e for e in existing}
    done = []
    for item_id in ids:
        e = by_id.get(item_id)
        if e is None:
            continue
        client.create_dataset_item(
            dataset_name=dataset_name,
            id=item_id,
            input=e.input,
            expected_output=e.expected_output,
            metadata=e.metadata,
            status="ARCHIVED",
        )
        done.append(item_id)
    return done
