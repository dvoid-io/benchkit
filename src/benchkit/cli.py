"""benchkit CLI (contract §4). Exit codes: 0 ok · 1 contract · 2 usage · 3 environment."""

from __future__ import annotations

import sys
import warnings
from datetime import datetime
from functools import wraps
from pathlib import Path

import click

from . import __version__
from .errors import EXIT_CONTRACT, BenchkitError, UsageError
from .manifest import load_manifest


def _handle(fn):
    """Map BenchkitError subclasses to exit codes; keep click's own usage errors (2)."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except UsageError as e:
            click.echo(f"benchkit: {e}", err=True)
            sys.exit(e.exit_code)
        except BenchkitError as e:
            click.echo(f"benchkit: {e}", err=True)
            sys.exit(e.exit_code)

    return wrapper


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="benchkit", message="%(prog)s %(version)s")
def main() -> None:
    """Manifest-driven LLM benchmark toolkit: cases -> Langfuse datasets/experiments."""
    warnings.simplefilter("default")
    warnings.showwarning = lambda message, *a, **k: click.echo(f"benchkit: note: {message}", err=True)


# ---------------------------------------------------------------------------


@main.command()
@click.option("--benchmark", "-b", default=None, help="Benchmark name (default: all).")
@_handle
def validate(benchmark: str | None) -> None:
    """Schema + invariants + render dry-run + leak guard. Exit 1 on any problem."""
    from .validate import validate_benchmark

    m = load_manifest()
    failed = False
    for b in m.select_benchmarks(benchmark):
        rep = validate_benchmark(b)
        click.echo(rep.format())
        failed = failed or not rep.ok
    if failed:
        sys.exit(EXIT_CONTRACT)


@main.command()
@click.option("--benchmark", "-b", default=None)
@click.option("--dataset", "-d", default=None, help="Logical dataset (default: every case).")
@click.option("--out", "-o", default=None, type=click.Path(file_okay=False), help="Output dir (default: .benchkit/render/<benchmark>/<dataset|all>).")
@_handle
def render(benchmark: str | None, dataset: str | None, out: str | None) -> None:
    """Write <id>.json per selected item — exactly what sync would send."""
    from .render import render_items, write_items

    m = load_manifest()
    for b in m.select_benchmarks(benchmark):
        ds = b.dataset(dataset) if dataset else None
        items = render_items(b, ds)
        target = Path(out) if out else m.dir / ".benchkit" / "render" / b.name / (dataset or "all")
        if out and len(m.select_benchmarks(benchmark)) > 1:
            target = target / b.name
        written = write_items(items, target)
        click.echo(f"{b.name}: wrote {len(written)} item(s) to {target}")


@main.command()
@click.option("--benchmark", "-b", default=None)
@click.option("--dataset", "-d", required=True)
@click.option("--dry-run", is_flag=True, help="Render and report; no network.")
@click.option("--archive-stale", is_flag=True, help="Archive Langfuse items not in the selection.")
@_handle
def sync(benchmark: str | None, dataset: str, dry_run: bool, archive_stale: bool) -> None:
    """Create/update the Langfuse dataset and upsert the selected items by id."""
    from .sync import sync_dataset

    m = load_manifest()
    b = m.benchmark(benchmark)
    plan = sync_dataset(b, dataset, dry_run=dry_run, archive_stale=archive_stale)
    if dry_run:
        click.echo(f"[dry-run] {b.name}/{dataset} -> dataset {plan.dataset!r}: would send {len(plan.new)} item(s)")
        for i in plan.new:
            click.echo(f"  {i}")
    else:
        click.echo(f"{b.name}/{dataset} -> " + plan.format())
        if plan.stale and not archive_stale:
            click.echo(f"  stale (not archived; pass --archive-stale): {', '.join(plan.stale)}")


@main.command()
@click.option("--benchmark", "-b", default=None)
@click.option("--dataset", "-d", required=True)
@click.option("--prompt", "-p", required=True, help="Logical prompt name from bench.yaml.")
@click.option("--model", "-m", default=None)
@click.option("--run-name", default=None)
@click.option("--version", "version_", default=None, help="Dataset snapshot timestamp (ISO 8601).")
@click.option("--max-concurrency", default=4, show_default=True, type=int)
@click.option("--limit", default=None, type=int, help="Only the first N items.")
@click.option("--response-format", "response_format", default=None, type=click.Choice(["json_schema", "none"]), help="Structured-output request mode (default: $BENCHKIT_RESPONSE_FORMAT, then manifest, then json_schema).")
@click.option("--strict/--no-strict", "strict", default=None, help="json_schema.strict (default: $BENCHKIT_STRICT, then manifest, then false).")
@_handle
def experiment(benchmark, dataset, prompt, model, run_name, version_, max_concurrency, limit, response_format, strict) -> None:
    """Run the prompt over the dataset through the OpenAI-compatible gateway; scores land in Langfuse."""
    from .experiment import run_experiment

    m = load_manifest()
    b = m.benchmark(benchmark)
    version = None
    if version_:
        try:
            version = datetime.fromisoformat(version_.replace("Z", "+00:00"))
        except ValueError:
            raise UsageError(f"--version must be ISO 8601, got {version_!r}") from None
    result = run_experiment(
        b,
        dataset_logical=dataset,
        prompt_logical=prompt,
        model=model,
        run_name=run_name,
        version=version,
        max_concurrency=max_concurrency,
        limit=limit,
        response_format=response_format,
        strict=strict,
    )
    fmt = getattr(result, "format", None)
    click.echo(fmt() if callable(fmt) else str(result))


@main.command()
@click.option("--benchmark", "-b", default=None)
@click.option("--dataset", "-d", default=None, help="Logical dataset (default: search all of the benchmark's).")
@click.option("--run-name", required=True)
@click.option("--out", "-o", default=None, type=click.Path(dir_okay=False), help="JSONL file (default: stdout).")
@click.option("--where", default=None, help="Filter expression over the row, e.g. \"protocol_pass == false\".")
@click.option("--no-outputs", is_flag=True, help="Skip fetching each trace's output (faster).")
@_handle
def export(benchmark, dataset, run_name, out, where, no_outputs) -> None:
    """Fetch a run's items + scores from Langfuse and write JSONL."""
    from .export import export_run, write_jsonl

    m = load_manifest()
    b = m.benchmark(benchmark)
    rows = export_run(b, run_name=run_name, dataset_logical=dataset, where=where, with_outputs=not no_outputs)
    text = write_jsonl(rows, out)
    if out:
        click.echo(f"wrote {len(rows)} row(s) to {out}")
    else:
        click.echo(text, nl=False)


@main.command()
@click.option("--benchmark", "-b", default=None)
@click.option("--dataset", "-d", default=None, help="Logical dataset whose cases to re-render (default: every case).")
@click.option("--run", "run_", required=True, type=click.Path(exists=True), help="Run export: a JSONL file, or a directory holding items.jsonl.")
@click.option("--out", "-o", default=None, type=click.Path(dir_okay=False), help="Write the replayed rows as JSONL.")
@click.option("--only-changed", is_flag=True, help="Keep only items whose scores moved (or that errored).")
@click.option("--quiet", "-q", is_flag=True, help="Suppress the summary; useful with --out.")
@_handle
def replay(benchmark, dataset, run_, out, only_changed, quiet) -> None:
    """Re-run the evaluators over a recorded run's outputs and diff against its scores.

    No model call and no network: the outputs come from the export, the items are
    re-rendered from the case files. This is how an evaluator or oracle change is measured
    against outputs that were already collected."""
    from .replay import replay_run, write_jsonl

    m = load_manifest()
    b = m.benchmark(benchmark)
    report = replay_run(b, run_, dataset_logical=dataset, only_changed=only_changed)
    if out:
        write_jsonl(report, out)
        click.echo(f"wrote {len(report.rows)} row(s) to {out}")
    if not quiet:
        click.echo(report.format())


@main.command()
@click.option("--benchmark", "-b", default=None)
@_handle
def doctor(benchmark: str | None) -> None:
    """Env present? Langfuse reachable? Prompts exist? Prompt {{vars}} ⊆ rendered input keys?"""
    from .doctor import run_doctor

    m = load_manifest()
    rep = run_doctor(m.select_benchmarks(benchmark))
    click.echo(rep.format())
    if rep.exit_code:
        sys.exit(rep.exit_code)


@main.command()
@click.option("--name", "-n", default="example", show_default=True, help="Benchmark name to scaffold.")
@click.option("--skills", is_flag=True, help="Also copy the /benchkit:* skills into ./.claude/skills/.")
@click.option("--dir", "target", default=".", show_default=True, type=click.Path(file_okay=False))
@click.option("--force", is_flag=True, help="Overwrite existing scaffold files.")
@_handle
def init(name: str, skills: bool, target: str, force: bool) -> None:
    """Scaffold bench.yaml + benchmarks/<name>/… (and optionally the skills)."""
    from .init import scaffold

    written = scaffold(target, name, skills=skills, force=force)
    for p in written:
        click.echo(f"  {p}")
    click.echo(f"scaffolded {len(written)} path(s) under {Path(target).resolve()}")


if __name__ == "__main__":  # pragma: no cover
    main()
