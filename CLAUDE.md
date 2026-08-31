# benchkit

Python toolkit turning a spec repo's git-managed cases into Langfuse datasets and experiments,
plus an evaluator harness and authoring skills. **`CONTRACT.md` is the only coupling** between
benchkit and any spec repo — change behaviour there first, then the code. benchkit knows nothing
about any particular benchmark, and no downstream benchmark data belongs in this repo.

- Layout: `src/benchkit/` (cli, manifest, cases, validate, render, langfuse_client, sync,
  experiment, export, replay, doctor, init, schemas/), `plugins/benchkit/` (skills,
  packaged into the wheel as `benchkit/_plugin`), `tests/` (offline; `tests/fixtures/toybench`
  is a synthetic spec repo — keep it synthetic).
- Dev loop: `uv sync && uv run pytest && uv run ruff check` — tests must stay fully offline
  (mock Langfuse/HTTP). Try the CLI from `tests/fixtures/toybench`.
- Versioning: hatch-vcs from `v*` tags; releases are cut by CI on tag push, never hand-built.
  Contract versioning is §7 of CONTRACT.md (additive → minor, breaking → major).
- Secrets only from the environment; never files, never argv; never print values.
