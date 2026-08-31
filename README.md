# benchkit

Manifest-driven LLM benchmark toolkit: git-managed cases → Langfuse datasets/experiments,
evaluator harness, authoring skills.

**The contract is the product**: a spec repo holds `bench.yaml` + cases + a `render()` + Langfuse-style
evaluators; benchkit validates, projects, syncs, runs and exports. Both sides code against
[`CONTRACT.md`](CONTRACT.md) and nothing else.

## Install (in a spec repo)

```toml
# pyproject.toml of the spec repo (virtual uv project)
[project]
dependencies = ["benchkit @ git+https://github.com/dvoid-io/benchkit@v0.3.0"]
[tool.uv]
package = false
```

```sh
uv sync
uv run benchkit init --name mybench --skills   # scaffold bench.yaml, benchmarks/mybench/…, ./.claude/skills/benchkit-*
```

## Use

```sh
uv run benchkit validate                                   # schema + invariants + render dry-run + leak guard
uv run benchkit render --benchmark X --dataset gold --out build/render
uv run benchkit sync --benchmark X --dataset gold --dry-run
uv run benchkit doctor
uv run benchkit sync --benchmark X --dataset gold [--archive-stale]
uv run benchkit experiment --benchmark X --dataset gold --prompt structured --model M
uv run benchkit export --benchmark X --run-name R --out run.jsonl --where "protocol_pass == false"
uv run benchkit replay --benchmark X --run runs/<name>/ --only-changed   # offline: re-score a recorded run
uv run benchkit --version
```
Exit codes: 0 ok · 1 validation/contract failure · 2 usage · 3 environment.

Env (never files, never argv): `LANGFUSE_BASE_URL` (fallback `LANGFUSE_HOST`), `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`; for `experiment`: `OPENAI_BASE_URL`/`OPENAI_API_KEY` (fallbacks
`LITELLM_BASE_URL`/`LITELLM_VIRTUAL_KEY`), optional `BENCHKIT_MODEL`, `BENCHKIT_RESPONSE_FORMAT`
(`json_schema`|`none`) and `BENCHKIT_STRICT` — endpoints differ on structured output (Anthropic's
OpenAI-compatible endpoint needs `strict:true`; LiteLLM/OpenAI accept false); CLI flags
`--response-format` / `--strict|--no-strict` override env, which overrides `prompts.<p>.response_format|strict`.
Model output handling (§3.2): unparseable → `{"_raw","_error":"parse: …"}`; schema-invalid → parsed object +
`_error`/`_schema_errors` (evaluators still run on the fields); `contract_valid` is False in both cases.

## Library

- `from benchkit import Evaluation` — the Langfuse SDK v4 evaluation type, re-exported.
- `benchkit.align` — pure oracle↔output alignment helpers (`match_variables`, `match_hypotheses`,
  `partition_isomorphic`, `proposition_equal`, …) for spec-repo evaluators.

## Develop

```sh
uv sync && uv run pytest && uv run ruff check
cd tests/fixtures/toybench && uv run benchkit validate     # synthetic spec repo
```
Releases: push a `v*` tag → CI builds the wheel, smoke-runs it, creates the GitHub release.
Unreleased dev builds skip the manifest's `benchkit:` version check (with a note).
