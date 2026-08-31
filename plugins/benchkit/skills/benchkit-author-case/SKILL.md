---
name: benchkit-author-case
description: Use when writing or editing a benchmark case file in a benchkit spec repo (a repo with bench.yaml) — how to satisfy the benchmark's oracle_schema, keep hidden truth out of the rendered input, and get `benchkit validate` to pass.
---

# Authoring a case that passes `benchkit validate`

benchkit is benchmark-agnostic: the **spec repo** defines what a case is. Before writing
anything, read in this order:

1. `bench.yaml` → the benchmark's `path`, `cases` glob, `oracle_schema`, `render`, `invariants`.
2. `<path>/<oracle_schema>` — the JSON Schema every case file must satisfy (required keys, enums, id pattern).
3. `<path>/render.py` (the `render` target) — which case fields become the dataset item's `input`
   (what the model sees) vs `expected_output` (what evaluators see). Anything only the
   evaluators may know must stay out of the fields render puts into `input`.
4. The spec repo's own authoring guide / reference cases (typically `docs/` or a
   `.claude/skills/*` it ships) — it owns the content rules; this skill only covers mechanics.
5. The `invariants` module, if declared — extra checks beyond the schema, each with a message.

## Mechanics
- One case per file, a mapping, with a unique stable `id`; YAML (`.yaml`) or JSON. Put it where
  the `cases` glob finds it (e.g. `cases/gold_017.yaml`). Never reuse an id.
- Membership in a Langfuse dataset is decided by each dataset's `select` expression in
  `bench.yaml` (e.g. `status == 'gold'`) — set the status/tag fields the selects read.
- **Leak guard**: if the case has `world_truth`, none of its leaf string values may appear
  verbatim in the rendered `input` (checked on `json.dumps(input)`). Paraphrase hidden truth,
  or — only when the overlap is legitimate — set `leak_guard: false` on the case and say why
  in a comment.
- `expected_output.rubric.critical: [evaluator_name, …]` (if the benchmark uses rubrics) names
  the evaluations whose AND becomes `protocol_pass`. Use evaluator names that exist in the
  benchmark's `EVALUATORS`.
- Keep cases deterministic and self-contained; no references to files outside the repo.
- Evaluators receive `output` with `_error` set when the model broke the contract: `parse:` (no fields at
  all, only `_raw`) or `schema:` (the parsed fields are present, plus `_schema_errors`) — write them to
  tolerate missing/malformed fields rather than failing wholesale.

## Loop
```sh
uv run benchkit validate --benchmark <name>     # fix every "<file>: <message>" line
uv run benchkit render --benchmark <name> --out build/render && git diff --no-index /dev/null build/render/<id>.json
```
`validate` exits 1 until the schema, invariants, render dry-run and leak guard all pass.
