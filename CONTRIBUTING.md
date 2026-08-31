# Contributing

- Behaviour lives in `CONTRACT.md`. Change the contract first (additive → minor bump, breaking → major), then the code, then tests.
- `uv sync && uv run pytest && uv run ruff check` must pass; tests run fully offline (mock Langfuse/HTTP). Never add real benchmark data from a downstream spec repo — `tests/fixtures/toybench` is synthetic.
- Python ≥3.11, `src/` layout, runtime deps stay minimal (`langfuse`, `jsonschema`, `pyyaml`, `click`, `packaging`, `httpx`).
- Commits: imperative subject, no attribution trailers. Releases only via `v*` tag push (CI).
