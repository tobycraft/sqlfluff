"""TPC-H and TPC-DS lex/parse benchmarks for pytest-codspeed.

Run locally (wall-time measurement via pytest-benchmark):
    pytest test/benchmarks/tpc_test.py -v

Run under CodSpeed instrumentation (instruction count):
    pytest test/benchmarks/tpc_test.py --codspeed -v

Each benchmark measures a single randomly-selected query so that the
Valgrind callgrind output stays within the CodSpeed runner's memory
limits.  A single-query benchmark captures the same regression signal
as the full suite: a 5% regression shows as 5% more instructions
regardless of how many queries are benchmarked.

Queries are chosen with a fixed seed so the selection is stable across
runs (reproducible baselines) but is not always Q1.
"""

import os
import random

from sqlfluff.core.linter import Linter
from sqlfluff.core.parser import Lexer

# Query indices (0-based) — injectable via env vars for ad-hoc testing.
# TPC-H default: random.Random(0).randrange(22) → 12  (Q13)
# TPC-DS default: Q1 (index 0) — one of the shortest queries; avoids
#   CodSpeed runner OOM segfault that Q50 triggers on the parse benchmark.
_TPCH_IDX = int(os.environ.get("TPCH_QUERY_IDX", random.Random(0).randrange(22)))
_TPCDS_IDX = int(os.environ.get("TPCDS_QUERY_IDX", 0))


def test_lex_tpch(benchmark, ansi_lexer: Lexer, tpch_sqls: list[str]):
    """Lex a single representative TPC-H query."""
    sql = tpch_sqls[_TPCH_IDX]
    benchmark(lambda: ansi_lexer.lex(sql))


def test_parse_tpch(benchmark, ansi_linter: Linter, tpch_sqls: list[str]):
    """Parse a single representative TPC-H query."""
    sql = tpch_sqls[_TPCH_IDX]
    benchmark(lambda: ansi_linter.parse_string(sql))


def test_lex_tpcds(benchmark, ansi_lexer: Lexer, tpcds_sqls: list[str]):
    """Lex a single representative TPC-DS query."""
    sql = tpcds_sqls[_TPCDS_IDX]
    benchmark(lambda: ansi_lexer.lex(sql))


def test_parse_tpcds(benchmark, ansi_linter: Linter, tpcds_sqls: list[str]):
    """Parse a single representative TPC-DS query."""
    sql = tpcds_sqls[_TPCDS_IDX]
    benchmark(lambda: ansi_linter.parse_string(sql))
