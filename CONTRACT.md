# benchkit contract v0.3 — what a spec repo and benchkit agree on

This is the **only** coupling between `benchkit` (toolkit) and a spec repo — the repo that
holds `bench.yaml`, the cases and the evaluators. benchkit knows nothing about any particular
benchmark; a spec repo imports nothing from benchkit internals. Both sides code against this file.
Contract version: `0.3` (manifest `benchkit: ">=0.3,<0.4"`).

## 1. Spec-repo layout (what benchkit expects to find)

```
<repo>/
  bench.yaml                      # the manifest (§2) — benchkit walks UP from cwd to find it
  pyproject.toml                  # virtual uv project: [tool.uv] package=false; depends on benchkit
  benchmarks/<name>/…             # anything; referenced from the manifest by path / module:attr
```
benchkit inserts the **manifest's directory** at the front of `sys.path` before
resolving any `module:attr`, so `benchmarks.<name>.render:render` imports as a plain
package path (needs `__init__.py` files, no build/install).

## 2. `bench.yaml`

```yaml
benchkit: ">=0.1,<0.2"            # required; PEP 440 specifier benchkit checks itself against
langfuse:                         # optional, informational (keys come from env)
  project: my-project
benchmarks:                       # required, ≥1
  <name>:
    path: benchmarks/<name>       # dir, relative to manifest; all relative paths below are relative to it
    cases: cases/**/*.yaml        # glob of case files (YAML or JSON); each file = one case (a mapping)
    oracle_schema: schema/oracle.schema.json      # JSON Schema (draft 2020-12) every case must satisfy
    output_schema: schema/output.schema.json      # JSON Schema the model's structured output must satisfy; also sent to Langfuse as structured-output schema
    render: benchmarks.<name>.render:render       # module:attr  (§3.1)
    evaluators: benchmarks.<name>.evaluators:EVALUATORS   # module:attr → list (§3.2)
    invariants: benchmarks.<name>.validate:INVARIANTS     # optional module:attr → list of callables(case) -> list[str] (errors)
    datasets:                     # logical name → Langfuse dataset
      gold:  { name: <langfuse dataset name>, select: "status == 'gold'" }    # select: Python expression over the case mapping (restricted eval: only names from the case + builtins `len`,`any`,`all`,`str`,`int`,`set`)
      draft: { name: <...>, select: "status in ('reviewed','gold')" }
    prompts:                      # logical name → Langfuse prompt ref
      protocol:   { name: "<prompt name>", label: production }      # text OR chat prompt
      structured: { name: "<prompt name>", label: production, output_schema: true }   # output_schema:true ⇒ request JSON matching output_schema
      # optional per prompt: response_format: json_schema|none (default json_schema; only with output_schema:true) and strict: bool (default false) — endpoints differ: Anthropic's OpenAI-compatible endpoint needs strict:true, LiteLLM/OpenAI accept false; `none` sends no response_format (fenced JSON still parsed)
    models:                       # optional defaults for `experiment`
      default: primary
```

Resolution rules: `select` decides membership; the item **id** is `case["id"]`
(required, stable, unique within the repo). Dataset item ids in Langfuse are
project-global, so benchkit writes them as `<dataset-logical-name>:<case id>`
*only if* `datasets.<x>.prefix_ids: true`; default is the bare case id (one dataset
per benchmark is the norm).

## 3. Python extension points (spec repo provides; benchkit calls)

### 3.1 render
```python
def render(case: dict) -> dict:
    """Return a DatasetItemSpec:
    {
      "id": str,                      # == case["id"]
      "input": dict,                  # JSON object; for UI prompt experiments its top-level keys are the prompt's {{vars}}
      "expected_output": dict | list | str | None,
      "metadata": dict | None,
    }
    MUST be deterministic and pure. MUST NOT leak anything the case marks hidden
    (that is the spec repo's responsibility; benchkit only checks `input` is JSON
    and, if the case has `world_truth`, that none of its leaf string values appear
    verbatim inside json.dumps(input) — a cheap leak guard, overridable per case with
    `leak_guard: false`)."""
```

### 3.2 evaluators — exactly the Langfuse SDK v4 evaluator shape
```python
from langfuse import Evaluation   # benchkit re-exports: from benchkit import Evaluation

def <name>(*, input, output, expected_output, metadata, **kwargs) -> Evaluation | list[Evaluation]:
    ...
EVALUATORS: list[callable]
```
- `output` is **whatever the task returned**: for `output_schema: true` prompts it is
  (a) `{"_raw": str, "_error": "parse: …"}` when the text is not JSON; (b) the **parsed object
  plus** `"_error": "schema: …"` and `"_schema_errors": ["<json path>: <message>", …]` when it
  parsed but violates `output_schema` — evaluators still see every field, so a single-field
  breach keeps its diagnostic signal; (c) the parsed object unchanged when valid. For plain
  prompts it is the text. Evaluators treat `"_error" in output` as "contract broken" and must
  tolerate malformed/missing fields in case (b).
- Return `Evaluation(name=<snake_case ≤35 chars>, value=<bool|float|str>, data_type="BOOLEAN"|"NUMERIC"|"CATEGORICAL", comment=<str|None>, metadata=<dict|None>)`.
- benchkit adds two evaluations itself: `contract_valid` (BOOLEAN — output parsed and validated against `output_schema`) and, if `expected_output.rubric.critical` exists, `protocol_pass` = AND of the named critical evaluations, plus `diagnostic_score` = passed/applicable over all BOOLEAN evaluations.
- Evaluators MUST be pure and stdlib-only (so they can also be pasted into Langfuse code evaluators later); they MUST NOT raise on malformed `output` — return a failing Evaluation with a comment instead.

### 3.3 invariants (optional)
`INVARIANTS: list[Callable[[dict], list[str]]]` — each returns error strings for a case; benchkit runs them in `validate` after schema validation.

## 4. CLI (stable)

```
benchkit validate   [--benchmark X]                      # schema + invariants + render dry-run + leak guard; exit 1 on any error
benchkit render     [--benchmark X] [--dataset D] [--out DIR]   # writes <id>.json per selected item (what sync would send)
benchkit sync       --benchmark X --dataset D [--dry-run] [--archive-stale]
benchkit experiment --benchmark X --dataset D --prompt P [--model M] [--run-name N] [--version ISO] [--max-concurrency N] [--limit N]
benchkit export     --benchmark X --run-name N [--out FILE.jsonl] [--where EXPR]
benchkit replay     --benchmark X --run PATH [--dataset D] [--out FILE.jsonl] [--only-changed] [--quiet]   # (0.3) offline: re-score a recorded run
benchkit doctor     [--benchmark X]                      # env vars present, Langfuse reachable, prompt exists, prompt {{vars}} ⊆ rendered input keys
benchkit init       [--name N] [--skills]                # scaffold bench.yaml + benchmarks/<N>/…; --skills copies /benchkit:* skills into ./.claude/skills/
benchkit --version
```
Exit codes: 0 ok · 1 validation/contract failure · 2 usage · 3 environment (missing env / unreachable Langfuse).

## 5. Environment (never files, never argv)

| var | used by | note |
|---|---|---|
| `LANGFUSE_BASE_URL` (fallback `LANGFUSE_HOST`) | sync/experiment/export/doctor | e.g. `https://cloud.langfuse.com`, or your self-hosted Langfuse |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | same | project keys |
| `OPENAI_BASE_URL` (fallback `LITELLM_BASE_URL`), `OPENAI_API_KEY` (fallback `LITELLM_VIRTUAL_KEY`) | experiment | any OpenAI-compatible chat endpoint (OpenAI, LiteLLM, the Anthropic/Gemini compatibility endpoints) |
| `BENCHKIT_MODEL` | experiment | default model if `--model` and manifest omit it |
| `BENCHKIT_RESPONSE_FORMAT`, `BENCHKIT_STRICT` | experiment | `json_schema`\|`none` and a boolean; CLI > env > manifest > default (§6) |

## 6. Experiment semantics
- `task(item)`: fetch prompt `P` from Langfuse (label/version from manifest), compile with `item.input` (text prompt → `{{var}}` substitution → single user message with the protocol as system? **No**: text prompt = system message; `item.input["transcript"]`/`["instruction"]` etc. are substituted into it; if the prompt declares a placeholder named `messages` and the input has `messages: [...]`, they are appended as chat turns); call the gateway with `response_format={"type":"json_schema","json_schema":{"name":"output","strict":<strict>,"schema":output_schema}}` when `output_schema: true` and the resolved response_format is `json_schema` (`none` ⇒ no `response_format` in the request); return the output per §3.2 (unparseable ⇒ `{"_raw","_error":"parse: …"}`; schema-invalid ⇒ parsed object + `_error`/`_schema_errors`; valid ⇒ parsed object). Resolution of `response_format`/`strict`: `--response-format`/`--strict|--no-strict` > `BENCHKIT_RESPONSE_FORMAT`/`BENCHKIT_STRICT` > manifest `prompts.<p>.response_format`/`.strict` > defaults (`json_schema`, `false`).
- One Langfuse experiment/run per invocation: `run_name` default `<prompt>@<version>-<model>-<git sha[:7]>` (git sha of the spec repo); `metadata` = {benchmark, dataset, prompt, model, benchkit_version, spec_repo_sha}.
- Uses `langfuse.run_experiment(...)` so runs/items/scores appear natively; `--version` pins the dataset snapshot.
- (0.2) Each gateway call is logged inside the item trace as a Langfuse `generation` observation named `chat`: requested model (updated to the model the gateway reports), input messages, output text, `model_parameters.response_format`, and `usage_details` normalized to `input`/`output`/`total` (+ `cache_read_input_tokens`/`cache_creation_input_tokens`/`reasoning_tokens`) from any of the OpenAI usage shape (OpenAI, LiteLLM, and the Anthropic/Gemini OpenAI-compatible endpoints: `prompt_tokens`/`completion_tokens`/`total_tokens` + `*_tokens_details`), the native Anthropic shape (`input_tokens`/`output_tokens` + cache fields), or the native Gemini shape (`usageMetadata`) — so Langfuse can price runs. A gateway error closes the observation with `level=ERROR`.
- benchkit never prints secret values; `doctor` prints only presence.

## 6.1 Replay semantics (0.3)
`replay` re-derives a recorded run's scores from its outputs **with no model call and no
Langfuse** — the only benchkit command besides `validate`/`render` that needs no network at all.

- `--run` takes a JSONL file, or a directory holding `items.jsonl`. Each row needs an `id` and
  an `output`; `scores` (as `benchkit export` writes them) is optional and is what the replay
  diffs against.
- Items are re-rendered from the case files (§3.1) and matched to rows by id, so replay measures
  the *current* cases and evaluators against outputs collected earlier. A row whose id no longer
  renders is reported (`missing_case`), never silently dropped — this is how a retired or renamed
  case shows up.
- Every evaluator (§3.2) plus benchkit's own `contract_valid`/`protocol_pass`/`diagnostic_score`
  is re-run. An evaluator that raises is reported per item and the remaining ones still run:
  replay is a development loop, and one broken evaluator must not hide the other items' results.
- Diff semantics: `changed` covers only names present in both the recorded run and this replay;
  a name we now emit that was not recorded is `added`, one no longer emitted is `removed`.
  An absent recorded score means "never measured", never "measured as null". Numeric scores
  compare within 1e-9.
- Exit code is 0 whether or not anything moved: replay reports, it does not judge.

## 7. Versioning of this contract
Additive changes bump the minor (0.3); breaking changes bump the major. Spec repos declare what they were written against; benchkit refuses (exit 1) a manifest whose specifier excludes its own version.
