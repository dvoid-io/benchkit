# benchkit

**Proves a benchmark is honest before it runs.**

Your cases live in git. benchkit fails the build when a hidden answer is reachable from the input
the model actually sees — mechanising item **II.5** of the [Agentic Benchmark Checklist](https://arxiv.org/abs/2507.02825),
*"the agent is completely isolated from any ground truth information."* Then it gets out of the
way: run the cases on whatever platform you already use.

```sh
uv run benchkit validate     # schema + invariants + render dry-run + ground-truth isolation
```

## Why

A benchmark whose answer is reachable from its own prompt measures reading, not reasoning — and
that failure is common, published, and expensive:

- **SWE-bench**: **32.67%** of top-of-leaderboard successful patches were *leaked solutions* sitting
  in the issue text. Filtering them dropped the headline resolution rate **12.47% → 3.97%**
  ([arXiv 2410.06992](https://arxiv.org/abs/2410.06992)).
- **SWE-Lancer** (OpenAI) fails the same check — agents could **score 100% without solving tasks**
  ([arXiv 2507.02825](https://arxiv.org/abs/2507.02825)).
- **HellaSwag**: replace the question with *Lorem ipsum* and **over 65% of predictions are unchanged**
  ([arXiv 2504.07825](https://arxiv.org/abs/2504.07825)).
- Surface matching is not enough: a 13B model reached GPT-4-level MMLU/GSM8k/HumanEval scores purely
  through *rephrased* overlap ([arXiv 2311.04850](https://arxiv.org/abs/2311.04850)).

The concept is not ours — it is published and named. What is missing is enforcement. The definitive
paper on the problem ships a **Markdown checklist**; teams hand-roll the check per benchmark. This
turns it into a build failure.

## What it does

- **Ground-truth isolation.** No leaf value of a case's hidden truth may appear in what the model
  sees. Cases where a hidden value legitimately appears must opt out explicitly, and prove it
  against the case's own licensed vocabulary rather than silently suppressing the check.
- **Structural isolation** — the stronger guarantee. A canary planted in the hidden field must not
  survive rendering, which proves the render path *cannot read* the hidden field at all. That is
  absence of a code path, not absence of a string.
- **Case validation.** JSON Schema for the case shape, plus your own invariants as Python callables.
- **Dataset maintenance between git and a platform.** Project cases into dataset items, sync them,
  reconcile what has gone stale.
- **Offline re-scoring.** `replay` re-runs your evaluators over a recorded run's outputs — no model
  calls, no network — re-deriving the oracle from the *current* case files, so editing a gold answer
  shows exactly which items change verdict.

## What it is not

Not an eval framework. It has no solvers, no agent loop, no scorer DSL, and no opinion about how you
run a model. [Inspect AI](https://inspect.aisi.org.uk) and [promptfoo](https://www.promptfoo.dev)
own that and are better at it. Not a prompt-management platform: git is the store, the platform gets
a projection. Bring your own runner.

## Related work

Honest placement — benchkit overlaps all of these and replaces none.

| Project | What it is | Overlap |
|---|---|---|
| [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) | The most complete OSS eval framework: tasks, solvers, scorers, log format, viewer | Its `score --action append` is a better-engineered `replay`. Use it as your runner |
| [promptfoo](https://www.promptfoo.dev) | YAML-config eval + red-team CLI | Closest by shape. Pulls prompts from platforms; no ground-truth isolation |
| [pydantic-evals](https://pydantic.dev/docs/ai/evals/evals/) | Typed `Dataset`/`Case` with YAML + **generated** JSON Schema | Better at the case/schema layer than we are — it generates the schema instead of hand-maintaining it |
| [Langfuse](https://langfuse.com) / [Phoenix](https://arize.com/docs/phoenix) / [Braintrust](https://www.braintrust.dev) / [Opik](https://github.com/comet-ml/opik) | Platforms: datasets, experiments, scores, UI | The projection target. They own the traces and the dashboards |
| [DeepEval](https://deepeval.com) / [Ragas](https://github.com/explodinggradients/ragas) | Metric libraries | Their metrics drop into our evaluator contract |
| [Evidently](https://github.com/evidentlyai/evidently) | Data/LLM monitoring | Its `ItemNoMatch(columns=[...])` is the closest off-the-shelf primitive to the leak check — generic, not purpose-built |

Every adjacent feature in this space — `ContextualRecall`, `ContextRecall`, `context_recall` — measures
whether the context **contains** the answer and treats high overlap as *success*. benchkit measures the
same quantity with inverted polarity, one lifecycle stage earlier, before a model is ever called.

## The contract

A spec repo owns `bench.yaml`, its cases, a `render()` that projects a case into what the model sees,
and evaluators. benchkit owns validation, projection and replay. Neither imports the other's
internals — [`CONTRACT.md`](https://github.com/dvoid-io/benchkit/blob/main/CONTRACT.md) is the whole
coupling. Scaffold one with `benchkit init`.

## Credentials

benchkit reads platform credentials from the process environment only — `LANGFUSE_BASE_URL`,
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` — and never from a file. There is no `.env` loader and
no config-file credential path, so whatever populates the environment works unchanged: direnv, CI
secrets, a secret manager, or your organisation's own tooling. `benchkit doctor` reports which
variables are missing by name and never prints a value.

## Status

`0.x`, Apache-2.0, built against one real benchmark and generalised on a hypothesis. The CLI and the
contract are versioned; a spec repo declares which contract it was written against and benchkit
refuses a mismatch.
