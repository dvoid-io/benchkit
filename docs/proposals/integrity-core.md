# Proposal: the integrity core

**Status**: proposal. **Supersedes** the framing that benchkit is a "manifest-driven LLM benchmark
toolkit" — it is narrower, and the narrow version is the defensible one.

## The question this answers

Should benchkit exist, given that Inspect AI, promptfoo, pydantic-evals and every observability
platform already run evals better than it does — and given that a person can drive the platform's UI
directly without it?

**Yes, for one reason: nothing proves a benchmark is honest.** Everything else benchkit does is
commodity and should be delegated or deleted.

## The evidence

### The problem is named and published

| Name | Source |
|---|---|
| "Agent is completely isolated from any ground truth information" (check **II.5**) | [Agentic Benchmark Checklist, arXiv 2507.02825](https://arxiv.org/abs/2507.02825) |
| **"solution leakage"** | [SWE-Bench+, arXiv 2410.06992](https://arxiv.org/abs/2410.06992) |
| "Explicit Answer Leakage" | [arXiv 2606.05241](https://arxiv.org/abs/2606.05241) |
| target/label leakage (8-type taxonomy, 329 affected papers) | [Kapoor & Narayanan, arXiv 2207.07048](https://arxiv.org/abs/2207.07048) |

Do **not** call it "prompt leakage" — that already means system-prompt extraction (OWASP LLM07). Do
not conflate it with training-data contamination or the BIG-bench canary GUID, which are
*cross-corpus* problems. This one is *within-case*.

### It breaks real benchmarks

- SWE-bench: **32.67%** of successful patches leaked; **12.47% → 3.97%** after filtering.
- SWE-Lancer (OpenAI): agents can **score 100% without solving tasks**.
- KernelBench: "failed to remove ground truth answers from GPU memory."
- HellaSwag: *Lorem ipsum* substitution leaves **>65%** of predictions unchanged.

### Nothing automates it

~25 frameworks and data-quality tools audited at source level — Inspect AI, promptfoo,
pydantic-evals, DeepEval, Ragas, lm-evaluation-harness, HELM, OpenAI Evals, LangSmith, Braintrust,
Langfuse, Phoenix, Opik, Weave, Giskard, Humanloop, Vellum, Great Expectations (all 340 core+contrib
expectations), Pandera, deepchecks, cleanlab, TFDV, Soda, dbt-expectations — **none ships an
authoring-time check of a case's input against its own hidden answer.**

Two near-misses to acknowledge rather than hide from: **Evidently `ItemNoMatch`** is a real shipped
cross-column "A must not contain B" primitive with a CI gate, and **Giskard** can express it via
`Not(StringMatching(...))`. Both are generic primitives nobody frames this way.

**Six independent hand-rolled reimplementations** were found in the wild (including Sourcegraph's
`prompt_hygiene.py`, with a hardcoded allowlist). Six ten-line reinventions is the strongest evidence
that no canonical library exists.

### The polarity insight

Every adjacent feature in the field — DeepEval `ContextualRecall`, Ragas `ContextRecall`, Braintrust
`ContextRecall`, the pydantic-evals `context_recall` rubric — measures **context ⊇ answer** and treats
high overlap as **success**. benchkit measures the same quantity with **inverted polarity at an
earlier lifecycle stage**. The mechanism is everywhere; the polarity and the stage are empty.

## What to build

**Tier 0 — structural isolation (already the strongest asset).** A canary planted in the hidden field
must not survive rendering. This proves the render path *cannot read* the hidden field — absence of a
code path, not absence of a string. Cheap, deterministic, zero false positives. Lead with it.

**Tier 1 — normalised matching.** Unicode NFKC, case/whitespace/punctuation folding, word-boundary
matching (so "cat" ⊄ "concatenate"), lemmatisation, numeric equivalence (`0.5` / `50%` / `1/2` /
`half`), date and unit normalisation, and per-case entity aliasing.

**Tier 2 — semantic.** Embedding similarity over sentence windows, then NLI entailment: does any span
of the visible input *entail* the hidden proposition? Entailment is the semantically correct
predicate — leakage is entailment, not substring. Justified by the decontaminator result: n-gram
matching is defeated by trivial rephrasing.

**Tier 3 — behavioural, the gold standard.** The **partial-input baseline**: render the case with the
answer-bearing region ablated and run a cheap model. If it still scores, the case leaks regardless of
surface form ([Feng et al., arXiv 1905.05778](https://arxiv.org/abs/1905.05778) — including its
caveat that passing does not prove absence of artifacts). A cheap deterministic proxy already exists:
Inspect AI ships `target_perplexity()`, and anomalously low target perplexity is a leak signal with
no gate attached to it.

### False positives are where this is won or lost

1. **Legitimately visible handles** — the opt-out must *prove itself* against the case's licensed
   vocabulary, never merely suppress.
2. **Option enumeration** — "reply BUY or SKIP" false-positives constantly; multiple-choice and
   categorical cases need per-type rules.
3. **Presupposition carriers** — a span may appear as a speaker's *utterance*, carrying a
   presupposition rather than asserting the truth. **Position and speaker attribution matter.**
   Span-anchored checking (leak iff the string appears *outside* the licensed spans) beats a
   document-level check.
4. **Structurally-required answers** — some scenarios legitimately put the reference in the input.
   Opt-out must be per case, never global.
5. **Allowlist rot** — allowlists must be *derived* from the case, not hand-maintained.

### One cautionary precedent

lm-evaluation-harness' decontamination module is orphaned: `get_train_overlap()` has zero callers,
the filter is a `pass` stub, no functional commit since 2022. **A validation feature that is not
wired into CI rots inside one major version.** Build-failure framing, or don't build it.

## What to delete or delegate

| Today | Action |
|---|---|
| `align.py` (252 lines) | **Delete.** Zero importers; its function names are domain concepts, contradicting the contract's first paragraph |
| hand-rolled `Gateway` + usage normalisation | Delegate to the OpenAI SDK with `base_url` |
| `export.py` join + 3-source fallback ladder (262 lines) | Collapse onto the platform SDK's experiment-items API |
| `from langfuse import Evaluation` re-export | **Own the dataclass.** Today every downstream evaluator imports a vendor type |
| owning the experiment loop via a private SDK parameter | Invert it: write a local run record, then project it |

Not adopted, with reasons: **inspect_ai** is 81 packages (interop target, never a dependency);
**litellm** is 187 MB and refetches its cost map at import, so two runs on different days can report
different costs; **pydantic-evals** hard-pins the pydantic-ai core and serialises evaluators by bare
class name, which breaks the git-is-source-of-truth premise — adopt its *pattern* of generating the
schema, not the dependency; **simpleeval** buys nothing here, because `Pow` and `LShift` are already
outside our node allowlist and `Attribute` is absent entirely (tested, not assumed).

## Naming

**"Ground-truth isolation"** for the concept — reviewers place it instantly against ABC II.5.
**"Leak guard"** for the artefact — unclaimed in this space.

And do not claim invention. *"We turn ABC II.5 into a build failure"* is defensible and verifiable.
*"We invented leak guarding"* is false, and a reviewer finds that out in one search.
