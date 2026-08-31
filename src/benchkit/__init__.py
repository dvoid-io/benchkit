"""benchkit — proves a benchmark is honest before it runs.

Ground-truth isolation, case validation, projection into an eval platform, and
offline re-scoring of a recorded run.

Public surface: the `benchkit` CLI (see CONTRACT.md §4) and `Evaluation` (re-exported
from the Langfuse SDK so spec repos can `from benchkit import Evaluation`).
"""

from __future__ import annotations

from langfuse import Evaluation

__all__ = ["Evaluation", "__version__"]


def _detect_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("benchkit")
        except PackageNotFoundError:
            pass
    except Exception:  # pragma: no cover - importlib.metadata always present on 3.11+
        pass
    try:
        from ._version import __version__ as v  # written by hatch-vcs at build time

        return str(v)
    except Exception:
        return "0.0.0"


__version__ = _detect_version()
