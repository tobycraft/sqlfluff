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


def test__main__unknown_dialect_rejected_by_cli():
    """An unsupported --dialect value should fail argparse validation."""
    with pytest.raises(SystemExit):
        rec.main(["--dialect", "mysql", "--segment", "SelectStatementSegment"])
