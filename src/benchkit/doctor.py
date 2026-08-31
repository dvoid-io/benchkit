"""`benchkit doctor`: env presence, Langfuse reachability, prompts exist, prompt vars ⊆ input keys."""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import EXIT_CONTRACT, EXIT_ENV, EXIT_OK, ContractError
from .langfuse_client import (
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_BASE_URL,
    first_env,
    get_client,
    missing_langfuse_env,
)
from .manifest import Benchmark
from .render import render_items


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    severity: str = "error"  # "error" | "warn"

    def format(self) -> str:
        mark = "ok " if self.ok else ("WARN" if self.severity == "warn" else "FAIL")
        return f"[{mark}] {self.name}" + (f" — {self.detail}" if self.detail else "")


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    exit_code: int = EXIT_OK

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def format(self) -> str:
        return "\n".join(c.format() for c in self.checks)


def run_doctor(benchmarks: list[Benchmark], *, client_factory=get_client, network: bool = True) -> DoctorReport:
    rep = DoctorReport()
    missing = missing_langfuse_env()
    rep.add(
        Check(
            "langfuse env (LANGFUSE_BASE_URL|LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY)",
            not missing,
            "missing: " + ", ".join(missing) if missing else "present",
        )
    )
    gw_base, gw_key = first_env(ENV_OPENAI_BASE_URL), first_env(ENV_OPENAI_API_KEY)
    rep.add(
        Check(
            "gateway env (OPENAI_BASE_URL|LITELLM_BASE_URL, OPENAI_API_KEY|LITELLM_VIRTUAL_KEY)",
            bool(gw_base and gw_key),
            "present" if (gw_base and gw_key) else "missing — only `experiment` needs these",
            severity="warn",
        )
    )

    # Local (offline) checks: render every benchmark once; collect input keys per benchmark.
    input_keys: dict[str, set[str]] = {}
    for b in benchmarks:
        try:
            items = render_items(b)
        except ContractError as e:
            rep.add(Check(f"{b.name}: render", False, str(e).splitlines()[0]))
            continue
        keys: set[str] | None = None
        for it in items:
            keys = set(it.input) if keys is None else keys & set(it.input)
        input_keys[b.name] = keys or set()
        rep.add(Check(f"{b.name}: render", True, f"{len(items)} item(s); common input keys: {sorted(input_keys[b.name])}"))

    if missing:
        rep.exit_code = EXIT_ENV
        return rep
    if not network:
        return rep

    try:
        client = client_factory()
        ok = bool(client.auth_check())
    except Exception as e:
        rep.add(Check("langfuse reachable", False, f"{type(e).__name__}: {e}"))
        rep.exit_code = EXIT_ENV
        return rep
    rep.add(Check("langfuse reachable", ok, "auth ok" if ok else "auth_check() returned False"))
    if not ok:
        rep.exit_code = EXIT_ENV
        return rep

    from .experiment import fetch_prompt

    for b in benchmarks:
        for logical, p in b.prompts.items():
            try:
                cp = fetch_prompt(client, p)
            except Exception as e:
                rep.add(Check(f"{b.name}: prompt {logical} ({p.name})", False, f"{type(e).__name__}: {e}"))
                rep.exit_code = max(rep.exit_code, EXIT_CONTRACT)
                continue
            rep.add(Check(f"{b.name}: prompt {logical} ({p.name})", True, f"v{cp.version} {cp.type}, vars={cp.variables}"))
            have = input_keys.get(b.name)
            if have is None:
                continue
            missing_vars = [v for v in cp.variables if v not in have]
            rep.add(
                Check(
                    f"{b.name}: prompt {logical} vars ⊆ input keys",
                    not missing_vars,
                    "missing from rendered input: " + ", ".join(missing_vars) if missing_vars else "ok",
                )
            )
            if missing_vars:
                rep.exit_code = max(rep.exit_code, EXIT_CONTRACT)
    return rep
