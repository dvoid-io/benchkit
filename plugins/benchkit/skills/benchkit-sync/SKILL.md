---
name: benchkit-sync
description: Use when a repo has a bench.yaml (a benchkit spec repo) and the task is to validate cases, render/diff dataset items, sync them to Langfuse, run an experiment against a prompt, or export run results. Runs `uv run benchkit …` with Langfuse credentials in the environment.
---

# benchkit: validate → render → sync → experiment → export

A spec repo is any directory (or parent) holding `bench.yaml` — the contract lives in
benchkit's `CONTRACT.md`. Everything below is `uv run benchkit <cmd>` from inside the repo.
Read `bench.yaml` first: it names the benchmarks, their logical **datasets** (with a
`select` expression over each case) and **prompts** (Langfuse prompt name + label).

## 1. Offline (no secrets needed)

```sh
uv run benchkit validate                       # schema + invariants + render dry-run + leak guard; exit 1 on any problem
uv run benchkit render --benchmark X --dataset gold --out build/render   # <id>.json per item = what sync would send
uv run benchkit sync --benchmark X --dataset gold --dry-run              # lists what would be sent; no network
```
Fix every `validate` problem before syncing — it prints `<file>: <message>` per case.
`git diff` on the rendered dir is the review surface for render changes.

## 2. With Langfuse (credentials from the environment, never in files or argv)

```sh
uv run benchkit doctor                # env present? Langfuse reachable? prompts exist? {{vars}} ⊆ input keys?
uv run benchkit sync --benchmark X --dataset gold [--archive-stale]
uv run benchkit experiment --benchmark X --dataset gold --prompt structured [--model M] [--limit 3]
uv run benchkit export --benchmark X --run-name <run> --out build/run.jsonl [--where "protocol_pass == false"]
```
Env the commands read: `LANGFUSE_BASE_URL` (or `LANGFUSE_HOST`), `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`; for `experiment` also `OPENAI_BASE_URL`/`OPENAI_API_KEY`
(fallbacks `LITELLM_BASE_URL`/`LITELLM_VIRTUAL_KEY`) and optionally `BENCHKIT_MODEL`.
Export them however you manage secrets — a `.env` loader, `op run`, `vault exec`, or by hand;
benchkit only ever reads them from the process environment.

## Exit codes
0 ok · 1 validation/contract failure · 2 usage · 3 environment (missing env / unreachable).
Run `doctor` first whenever a networked command fails with exit 3.

## Do / don't
- Do `--dry-run` / `--limit 3` before a full sync/experiment.
- Do pass `--archive-stale` only when the selection is intentionally smaller than before.
- Don't edit items in the Langfuse UI — cases in git are the source of truth; re-sync.
- Don't print or paste secret values; `doctor` only reports presence.
