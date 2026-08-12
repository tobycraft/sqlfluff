"""Tests for utils/realengine_check.py.

`utils/` is repo-root dev tooling, not part of the `sqlfluff` package, so it's
imported here by adding it to sys.path rather than as a normal import.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "utils"))

import realengine_check as rec  # noqa: E402


def test__check_postgres__accepts_valid_sql():
    """Valid Postgres syntax should report no divergence."""
    assert rec.check_postgres("SELECT * FROM foo") is None


def test__check_postgres__rejects_invalid_sql():
    """Invalid Postgres syntax should report a parse error message."""
    error = rec.check_postgres("SELECT * FROM WHERE")
    assert error is not None
    assert "syntax error" in error


def test__check_duckdb__accepts_valid_sql():
    """Valid DuckDB syntax should report no divergence."""
    assert rec.check_duckdb("SELECT * FROM foo") is None


def test__check_duckdb__rejects_invalid_sql():
    """Invalid DuckDB syntax should report a parse error message."""
    error = rec.check_duckdb("SELECT * FROM WHERE")
    assert error is not None
    assert "syntax error" in error


def test__check_duckdb__does_not_flag_semantic_only_errors():
    """A missing-table error is not a syntax divergence and must not be flagged.

    This is the behavior that justifies catching duckdb.ParserException
    specifically rather than the broader duckdb.Error - DuckDB has no
    pure-parse API for non-SELECT statements, so this checker executes for
    real, and must filter out non-syntax failures rather than flagging them.
    """
    assert rec.check_duckdb("SELECT * FROM nonexistent_table_xyz") is None


def test__check_sparksql__accepts_valid_sql():
    """Valid Spark SQL should report no divergence."""
    assert rec.check_sparksql("SELECT * FROM foo") is None


def test__check_sparksql__rejects_invalid_sql():
    """Invalid Spark SQL syntax should report a parse error message.

    (Not `SELECT * FROM WHERE` here, unlike the postgres/duckdb tests - Spark
    parses that as querying a table literally named WHERE, which is a
    semantic error, not a syntax one. `SELEC 1` is unambiguously a syntax
    error in every engine.)
    """
    error = rec.check_sparksql("SELEC 1")
    assert error is not None
    assert "Syntax error" in error


def test__check_sparksql__does_not_flag_semantic_only_errors():
    """A missing-table error is not a syntax divergence and must not be flagged.

    This is the behavior that justifies catching pyspark.errors.ParseException
    specifically (and ahead of the broader AnalysisException it subclasses,
    plus any other Spark-internal exception) rather than something broader.
    """
    assert rec.check_sparksql("SELECT * FROM nonexistent_table_xyz") is None


def test__check_clickhouse__accepts_valid_sql():
    """Valid ClickHouse SQL should report no divergence."""
    assert rec.check_clickhouse("SELECT * FROM foo") is None


def test__check_clickhouse__rejects_invalid_sql():
    """Invalid ClickHouse syntax should report a parse error message."""
    error = rec.check_clickhouse("SELEC 1")
    assert error is not None
    assert "SYNTAX_ERROR" in error


def test__check_clickhouse__does_not_flag_semantic_only_errors():
    """A missing-table error is not a syntax divergence and must not be flagged.

    chdb doesn't expose typed exceptions the way duckdb/pyspark do - every
    error is a plain RuntimeError - so check_clickhouse filters by message
    content instead (only "(SYNTAX_ERROR)" counts). This is the case that
    justifies that: a missing-table error is also a RuntimeError, but its
    message ends in "(UNKNOWN_TABLE)", not "(SYNTAX_ERROR)".
    """
    assert rec.check_clickhouse("SELECT * FROM nonexistent_table_xyz") is None


def test__run__skiplisted_divergence_is_skipped_not_failed(monkeypatch, capsys):
    """A divergence with a matching skiplist entry.

    Should be reported but not fail the run.
    """
    monkeypatch.setattr(rec, "CHECKERS", {"postgres": lambda sql: "always fails"})
    monkeypatch.setattr(
        "generate_dialect_sql.generate", lambda *a, **k: ["SELECT * FROM foo"]
    )
    monkeypatch.setattr("generate_dialect_sql.self_check", lambda *a, **k: True)

    skiplist = {("postgres", "SELECT * FROM foo"): "known false positive"}
    ok = rec.run("postgres", "SelectStatementSegment", 8, 10, skiplist)

    assert ok is True
    out = capsys.readouterr().out
    assert "[skipped]" in out
    assert "known false positive" in out


def test__run__unresolved_divergence_fails_the_run(monkeypatch, capsys):
    """A divergence with no matching skiplist entry should fail the run."""
    monkeypatch.setattr(rec, "CHECKERS", {"postgres": lambda sql: "always fails"})
    monkeypatch.setattr(
        "generate_dialect_sql.generate", lambda *a, **k: ["SELECT * FROM foo"]
    )
    monkeypatch.setattr("generate_dialect_sql.self_check", lambda *a, **k: True)

    ok = rec.run("postgres", "SelectStatementSegment", 8, 10, skiplist={})

    assert ok is False
    out = capsys.readouterr().out
    assert "[DIVERGENCE]" in out


def test__run__end_to_end_smoke():
    """A real run against a real segment should complete and return a bool.

    Not asserting on the exact divergence count/content here - that's live
    grammar data that can legitimately change as the dialect evolves. This is
    a smoke test that the whole pipeline (generate -> self_check -> pglast)
    runs without raising.
    """
    ok = rec.run("postgres", "CreateTableStatementSegment", 8, 20, skiplist={})
    assert isinstance(ok, bool)


def test__run__end_to_end_smoke_duckdb():
    """Same smoke test as above, against the DuckDB checker."""
    ok = rec.run("duckdb", "CreateTableStatementSegment", 8, 20, skiplist={})
    assert isinstance(ok, bool)


def test__run__end_to_end_smoke_sparksql():
    """Same smoke test as above, against the Spark checker.

    The shared SparkSession is created lazily on first use and cached at
    module level (see rec._get_spark_session), so this test pays the ~7s
    session-startup cost only if no earlier test in this process already
    triggered it - it isn't repeated per test.
    """
    ok = rec.run("sparksql", "CreateTableStatementSegment", 8, 20, skiplist={})
    assert isinstance(ok, bool)


def test__run__end_to_end_smoke_clickhouse():
    """Same smoke test as above, against the ClickHouse checker."""
    ok = rec.run("clickhouse", "CreateTableStatementSegment", 8, 20, skiplist={})
    assert isinstance(ok, bool)


def test__main__unknown_dialect_rejected_by_cli():
    """An unsupported --dialect value should fail argparse validation."""
    with pytest.raises(SystemExit):
        rec.main(["--dialect", "mysql", "--segment", "SelectStatementSegment"])
