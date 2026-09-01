# Proposal: prompts as code

**Status**: proposal — not implemented. **Target**: contract 0.5 (0.4 shipped without it).

benchkit versions cases, schemas, evaluators and runs in git, and fetches the *prompt* from a
platform by name and label at run time. That asymmetry is the gap this proposes to close, in a
way that generalises past one platform.

## Verdict: build capture, not sync

The prior art is mostly a graveyard, and it dies in a consistent place.

| project | what it shipped | state (2026-08) |
|---|---|---|
| Latitude | `latitude-lock.json` + `pull` / `push` / `checkout` | prompt product removed; no `prompt` path in the live API |
| Humanloop | `.prompt` files (YAML frontmatter + role tags); docs promised *"two-way synchronization is coming soon"* | never shipped it; company gone |
| Pezzo | version id = `sha256(content + Date.now())` | dead |
| Agenta | had a CLI | CLI deleted 2025-02-27 |
| PromptHub | "git-style versioning" | no repo sync in either direction; no endpoint writes a version |
| Vellum | `push` / `pull` | workflows only — no prompts |
| Braintrust | `bt functions push/pull` | **the only shipped, supported round-trip** — but its artifact is generated source code, not hashable prompt text |

What keeps dying is **bidirectional sync**. Two teams built approximately this design and neither
survived; the one with the best on-disk format promised two-way and never delivered it.

That is not an argument against the feature. It is an argument about its shape:

> **`pull` and `verify` are first-class. `push` is append-only and refuses to write when nothing
> changed. Moving a deployment pointer is a separate, human-invoked verb. There is no command
> called `sync`.**

### Why it earns its place

The platform already versions prompts and gives non-engineers a UI, so the value is *not*
versioning. It is four things a platform cannot provide:

1. **Atomicity with what the prompt is judged by** — a prompt change reviewed in the same commit
   as the cases and evaluators it moves.
2. **Offline work** — authoring, validation and replay continue when the platform is unreachable.
3. **Provenance** — the committed bytes hash to what was actually sent, checkable in CI.
4. **Portability** — see the table above for why single-vendor coupling is a real risk.

Those only hold if git is authoritative for prompt *content* while the platform stays
authoritative for *deployment*. Blur that and you get the sync problem that killed the others.

## What benchkit gets wrong today

Two current behaviours are the reason this is worth doing, independent of any new command:

- **The prompt is resolved at run time.** `fetch_prompt()` passes `label` to the API, so a run's
  identity depends on who last moved `production`. Two runs of "the same" experiment a week apart
  are not comparable, and nothing in the run record explains why.
- **Rendering is delegated to the platform SDK.** `build_messages()` calls
  `PromptClient.compile()`. Langfuse's Python client is a hand-rolled `{{`/`}}` scan — not
  mustache: no sections, no partials, no escaping, and unknown variables are left in the output
  verbatim. Its own JS client uses real mustache. **The same stored prompt renders differently
  depending on which SDK reads it**, and nothing in the stored prompt says which dialect it was
  authored against.

Recording `system_prompt_sha256` per run (contract 0.2) mitigates the first after the fact. Neither
is fixed by it.

## Constraints (each from an observed failure)

1. **Identity is `(logical slug, sha256 of the body verbatim)`** — never the platform's version id.
   Those are ints (Langfuse, PromptLayer), base64 Relay GIDs (Phoenix), 64-hex commit hashes
   (LangSmith) and 16-hex transaction ids (Braintrust); they belong in the lockfile only.
   **No timestamp in the hash** — that mistake makes the id unable to answer "is this unchanged?",
   which is the one question the tool exists to answer.
2. **`template_format` is required in the file**, and it records the *authored* dialect. `{{var}}`
   means four incompatible things across platforms; two of the six store no dialect at all. The
   portable subset is flat `{{variable}}`: no sections, no dot-notation, no filters.
   **No Jinja2** — templating engines in prompt playgrounds have produced environment-variable
   disclosure and remote code execution.
3. **benchkit owns rendering.** A `PromptDoc` plus its declared dialect renders through one
   benchkit renderer, so a case renders identically no matter which platform the prompt came from.
4. **`push` with no change is a no-op** — enforced by the abstraction, not hoped for from the API.
   Five of six platforms happily create a duplicate version on a re-run, and on three of them that
   history can never be pruned.
5. **`pull` never deletes.** Version files are immutable *records*; a version that vanishes
   server-side is a loud failure, not a cleanup task.
6. **Labels resolve to pinned versions at plan/pull time.** Experiments consume a pinned version.
   "What is live" is always a server read, never inferred from our own last push.
7. **Ownership is partitioned.** Git owns body, declared variables and model parameters; the
   platform owns labels, tags and scores. Crossing that line requires an explicit flag.

## Portable model

The common core across Langfuse, Phoenix, LangSmith, Braintrust, Opik and PromptLayer is small and
real: a named prompt holding an append-only chain of immutable versions; three read selectors
(version, pointer, latest); a movable pointer to exactly one version; new-version as the only
content write; text-vs-chat as the single structural axis; a free-form JSON blob; a per-version
human note.

```yaml
# prompts/<slug>.prompt.md — YAML frontmatter, body below
logical: interlocutor-structured      # ^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$ — the intersection of
kind: text                            #   every platform's name grammar
template_format: mustache-flat        # REQUIRED: the authored dialect
variables: []                         # declared, validated against the body — not inferred
model: {provider: anthropic, name: claude-haiku-4-5, params: {}}   # optional; some require it
config: {}                            # the universal home for everything else
message: "…"                          # <=500 chars, survives every platform
extra:                                # per-provider escape hatch, never portable
  phoenix: {response_format: {...}}
---
<the prompt body, verbatim>
```

Names are a logical slug mapped per provider (`remote_name(logical)`), because the grammars do not
intersect usefully — one platform allows spaces, unicode and `/` folders; another allows none of
them and scopes names **globally across its whole database**, so two benchmarks on one instance
would collide.

## Provider interface

```python
class PromptProvider(Protocol):
    caps: Capabilities                  # scope, dedupes_identical_content, can_unset_label,
                                        # formats, requires_model, name/label patterns …
    def remote_name(self, logical: str) -> str: ...        # pure, injective, no network
    def validate(self, doc) -> list[Diagnostic]: ...       # pure + OFFLINE: catch every 4xx first
    def fetch(self, logical, *, version=None, label=None) -> RemotePrompt: ...
    def diff(self, doc, remote) -> Change: ...             # NONE | CONTENT | METADATA | ABSENT
    def push(self, doc, *, labels=(), expect_version=None, dry_run=False) -> PushResult: ...
    def set_label(self, logical, label, version) -> None: ...   # move-or-create, read-verified
```

Two guarantees belong to the abstraction rather than any API: **`push` is a no-op when `diff` is
`NONE`**, and **`dry_run` is honoured locally** — no platform offers one.

`validate()` being pure and offline is what makes the whole thing usable while disconnected, and it
is where the asymmetries land: one platform requires `model_provider` from a closed enum plus
`model_name` on every version; another requires nothing. A document missing what a provider needs
fails before any network call.

## Phasing

1. **`prompts pull` + lockfile + `prompts verify`** — capture what exists, prove the committed
   bytes match the platform and the run record. Read-only, useful immediately, no way to do harm.
2. **Pin at plan time** — `experiment` consumes a pinned version; label resolution moves to pull.
   Fixes the run-identity gap above and needs no new write path.
3. **benchkit-owned rendering** keyed on `template_format`, replacing `PromptClient.compile()`.
4. **`prompts push`** — append-only, sha-gated, `--dry-run` first. Then `prompts promote` as a
   separate verb for pointer moves.
5. **A second provider** — the interface is unproven until something other than the first platform
   implements it. Note that at least one candidate's own CLI is currently read-only.

Steps 1–3 are worth doing even if `push` is never built.

## Out of scope

Prompt composition/include syntax (resolve composition at build time in git, where it is reviewable
and hashable, rather than at render time in a dialect-dependent engine); forking and public prompt
hubs; A/B or percentage-split pointers; distributing prompts as OCI artifacts.
