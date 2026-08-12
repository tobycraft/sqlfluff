"""Tests for utils/generate_dialect_sql.py.

`utils/` is repo-root dev tooling, not part of the `sqlfluff` package, so it's
imported here by adding it to sys.path rather than as a normal import.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "utils"))

from generate_dialect_sql import GenerationError, generate, self_check  # noqa: E402


@pytest.mark.parametrize(
    "dialect,segment",
    [
        ("ansi", "SelectStatementSegment"),
        ("ansi", "CreateTableStatementSegment"),
        ("postgres", "CreateTableStatementSegment"),
        ("bigquery", "SelectStatementSegment"),
        ("snowflake", "CreateTableStatementSegment"),
    ],
)
def test__generate_dialect_sql__produces_valid_examples(dialect, segment):
    """Generation should produce at least one example that parses cleanly.

    Not every generated candidate is expected to be valid SQL (that's what
    the self-check filtering in the real CLI is for), but the tool is only
    useful if *some* of what it generates for a real statement type survives
    that filtering.
    """
    examples = generate(dialect, segment, max_depth=8, max_examples=20)
    assert examples, "generator produced no candidates at all"

    valid = [sql for sql in examples if self_check(dialect, sql)]
    assert valid, f"no generated example for {dialect}/{segment} parsed cleanly"


def test__generate_dialect_sql__unknown_segment_raises():
    """An unknown --segment name should fail clearly, not silently."""
    with pytest.raises(GenerationError):
        generate("ansi", "NotARealSegmentName", max_depth=8, max_examples=5)


def test__generate_dialect_sql__self_referential_grammar_terminates():
    """Self-referential grammar must not hang or blow the recursion stack.

    Expressions containing expressions are exactly this case - the depth
    limit and cycle guard should always bring the walk to an end.
    """
    examples = generate("ansi", "ExpressionSegment", max_depth=6, max_examples=10)
    assert examples
