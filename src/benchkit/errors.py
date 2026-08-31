"""Exception hierarchy mapped to CLI exit codes (contract §4)."""

from __future__ import annotations

EXIT_OK = 0
EXIT_CONTRACT = 1
EXIT_USAGE = 2
EXIT_ENV = 3


class BenchkitError(Exception):
    """Base class; `exit_code` is what the CLI exits with."""

    exit_code = EXIT_CONTRACT


class ContractError(BenchkitError):
    """Validation / contract failure (bad manifest, bad case, bad render, version refusal)."""

    exit_code = EXIT_CONTRACT


class UsageError(BenchkitError):
    """Bad invocation (unknown benchmark/dataset/prompt, missing required option)."""

    exit_code = EXIT_USAGE


class EnvError(BenchkitError):
    """Missing environment or unreachable Langfuse/gateway."""

    exit_code = EXIT_ENV
