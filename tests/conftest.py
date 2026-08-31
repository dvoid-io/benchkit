from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from benchkit.manifest import load_manifest

FIXTURES = Path(__file__).parent / "fixtures"
TOYBENCH = FIXTURES / "toybench"

OWN = "0.1.0"  # pretend released version so the manifest specifier check is exercised
# What a freshly scaffolded repo declares. It must satisfy the specifier `benchkit init`
# writes — test_init.py::test_scaffold_targets_the_running_benchkit pins that to __version__.
SCAFFOLD_OWN = "0.4.0"


@pytest.fixture
def toybench() -> Path:
    return TOYBENCH


@pytest.fixture
def manifest():
    return load_manifest(TOYBENCH / "bench.yaml", own_version=OWN)


@pytest.fixture
def toy(manifest):
    return manifest.benchmarks["toy"]


@pytest.fixture
def in_toybench(monkeypatch):
    monkeypatch.chdir(TOYBENCH)
    yield TOYBENCH


@pytest.fixture
def clean_env(monkeypatch):
    for k in (
        "LANGFUSE_BASE_URL",
        "LANGFUSE_HOST",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "LITELLM_BASE_URL",
        "LITELLM_VIRTUAL_KEY",
        "BENCHKIT_MODEL",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def langfuse_env(clean_env, monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://langfuse.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")


@pytest.fixture
def bench_copy(tmp_path):
    """A writable copy of toybench (so tests can break things). Each copy gets a unique
    package name so importlib caching between tests does not bleed."""
    counter = {"n": 0}

    def make(name: str = "toy"):
        counter["n"] += 1
        root = tmp_path / f"bench{counter['n']}"
        shutil.copytree(TOYBENCH, root)
        # Uniquify the python package so cached modules from other copies are never reused.
        pkg = f"benchmarks_{os.getpid()}_{abs(hash(str(root))) % 10_000_000}_{counter['n']}"
        (root / pkg).mkdir()
        shutil.move(str(root / "benchmarks" / "toy"), str(root / pkg / "toy"))
        shutil.rmtree(root / "benchmarks")
        (root / pkg / "__init__.py").write_text("")
        y = (root / "bench.yaml").read_text().replace("benchmarks.toy.", f"{pkg}.toy.")
        y = y.replace("path: benchmarks/toy", f"path: {pkg}/toy")
        (root / "bench.yaml").write_text(y)
        return root

    yield make
    # drop any modules imported from the temp copies
    for m in [m for m in sys.modules if m.startswith("benchmarks_")]:
        sys.modules.pop(m, None)


def load(root: Path, own_version: str = OWN):
    return load_manifest(root / "bench.yaml", own_version=own_version)
