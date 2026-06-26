"""TPC-H and TPC-DS lex/parse benchmarks for pytest-codspeed.

Run locally (wall-time measurement via pytest-benchmark):
    pytest test/benchmarks/test_tpc.py -v

Run under CodSpeed instrumentation (instruction count):
    pytest test/benchmarks/test_tpc.py --codspeed -v
"""

from sqlfluff.core.linter import Linter
from sqlfluff.core.parser import Lexer


def test_lex_tpch(benchmark, ansi_lexer: Lexer, tpch_sqls: list[str]):
    """Lex all 22 TPC-H queries (Q1–Q22) in one pass."""
    benchmark(lambda: [ansi_lexer.lex(sql) for sql in tpch_sqls])


def test_parse_tpch(benchmark, ansi_linter: Linter, tpch_sqls: list[str]):
    """Lex and parse all 22 TPC-H queries (Q1–Q22) in one pass."""
    benchmark(lambda: [ansi_linter.parse_string(sql) for sql in tpch_sqls])


def test_lex_tpcds(benchmark, ansi_lexer: Lexer, tpcds_sqls: list[str]):
    """Lex all 99 TPC-DS queries (Q1–Q99) in one pass."""
    benchmark(lambda: [ansi_lexer.lex(sql) for sql in tpcds_sqls])


def test_parse_tpcds(benchmark, ansi_linter: Linter, tpcds_sqls: list[str]):
    """Lex and parse all 99 TPC-DS queries (Q1–Q99) in one pass."""
    benchmark(lambda: [ansi_linter.parse_string(sql) for sql in tpcds_sqls])
