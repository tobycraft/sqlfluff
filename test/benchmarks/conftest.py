"""Shared fixtures for the benchmark suite.

TPC-H and TPC-DS SQL files must be pre-fetched into .cache/tpc-fixtures/
before running (the CI workflow does this via the fetch-tpc-fixtures job;
locally run: cargo build -p sqlfluffrs_benchmarks --features fetch).
"""

from pathlib import Path
from typing import Generator

import pytest

from sqlfluff.core import FluffConfig
from sqlfluff.core.linter import Linter
from sqlfluff.core.parser import Lexer

_TPCH_N = 22
_TPCDS_N = 99

_CACHE_DIR = Path(__file__).parents[2] / ".cache" / "tpc-fixtures"


def _load_tpch() -> list[str]:
    tpch_dir = _CACHE_DIR / "tpc-h"
    return [(tpch_dir / f"{n}.sql").read_text() for n in range(1, _TPCH_N + 1)]


def _load_tpcds() -> list[str]:
    tpcds_dir = _CACHE_DIR / "tpc-ds"
    return [(tpcds_dir / f"{n}.sql").read_text() for n in range(1, _TPCDS_N + 1)]


@pytest.fixture(scope="session")
def tpch_sqls() -> list[str]:
    """All 22 TPC-H query strings, pre-fetched into .cache/tpc-fixtures/."""
    if not (_CACHE_DIR / "tpc-h").exists():
        pytest.skip("TPC-H fixtures not found; run fetch-tpc-fixtures first")
    return _load_tpch()


@pytest.fixture(scope="session")
def tpcds_sqls() -> list[str]:
    """All 99 TPC-DS query strings, pre-fetched into .cache/tpc-fixtures/."""
    if not (_CACHE_DIR / "tpc-ds").exists():
        pytest.skip("TPC-DS fixtures not found; run fetch-tpc-fixtures first")
    return _load_tpcds()


@pytest.fixture(scope="session")
def ansi_lexer() -> Generator[Lexer, None, None]:
    """ANSI dialect Lexer instance, shared across the session."""
    yield Lexer(config=FluffConfig(overrides={"dialect": "ansi"}))


@pytest.fixture(scope="session")
def ansi_linter() -> Generator[Linter, None, None]:
    """ANSI dialect Linter instance, shared across the session."""
    yield Linter(config=FluffConfig(overrides={"dialect": "ansi"}))
