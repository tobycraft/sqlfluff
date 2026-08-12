"""Tests for utils/generate_dialect_sql.py.

`utils/` is repo-root dev tooling, not part of the `sqlfluff` package, so it's
imported here by adding it to sys.path rather than as a normal import.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "utils"))

from generate_dialect_sql import (  # noqa: E402
    GenerationError,
    generate,
    main,
    self_check,
)


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
    examples = generate(dialect, segment, max_examples=20)
    assert examples, "generator produced no candidates at all"

    valid = [sql for sql in examples if self_check(dialect, sql)]
    assert valid, f"no generated example for {dialect}/{segment} parsed cleanly"


def test__generate_dialect_sql__unknown_segment_raises():
    """An unknown --segment name should fail clearly, not silently."""
    with pytest.raises(GenerationError):
        generate("ansi", "NotARealSegmentName", max_examples=5)


def test__generate_dialect_sql__self_referential_grammar_terminates():
    """Self-referential grammar must not hang or blow the recursion stack.

    Expressions containing expressions are exactly this case - the cycle
    guard (a Ref name already visited on the current path renders as a
    terminal instead of being followed again) should always bring the walk
    to an end.
    """
    examples = generate("ansi", "ExpressionSegment", max_examples=10)
    assert examples


def test__generate_dialect_sql__zero_coverage_matches_default():
    """coverage=0 (the default) must be indistinguishable from omitting it.

    This is the backward-compatibility contract for the coverage parameter.
    """
    with_default = generate("postgres", "CreateTableStatementSegment", 20)
    with_explicit_zero = generate(
        "postgres", "CreateTableStatementSegment", 20, coverage=0
    )
    assert with_default == with_explicit_zero


def test__generate_dialect_sql__high_coverage_reaches_nested_content():
    """High coverage should discover content invisible at coverage=0.

    postgres/CreateTableStatementSegment's column list is itself optional, so
    at coverage=0 it's always omitted (baseline renders as bare
    "CREATE TABLE foo ( )"): candidate discovery only looks at what's visible
    while rendering the minimal baseline, so nothing inside that omitted
    column list - real column definitions, constraints - is ever found.
    coverage=100 adds discovery rounds that render already-known optional/
    branch points and look for *new* candidates nested inside them, which
    should surface something with real content in the column list.
    """
    baseline_only = generate("postgres", "CreateTableStatementSegment", 40, coverage=0)
    assert baseline_only[0] == "CREATE TABLE foo ( )"

    thorough = generate("postgres", "CreateTableStatementSegment", 40, coverage=100)
    assert any(
        len(example.split()) > len(baseline_only[0].split()) for example in thorough
    )


def test__generate_dialect_sql__mid_coverage_combines_candidates():
    """Mid-range coverage should produce examples with 2+ non-baseline elements.

    At coverage=0 every example is exactly one toggle away from the minimal
    baseline; combination width > 1 (reached above coverage=0) should produce
    at least one example where two independent optional SELECT clauses are
    both present simultaneously.
    """
    examples = generate("ansi", "SelectStatementSegment", 40, coverage=60)
    clause_keywords = ("WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET")

    def clause_count(example: str) -> int:
        tokens = example.split()
        return sum(1 for kw in clause_keywords if kw in tokens)

    assert any(clause_count(example) >= 2 for example in examples)


def test__generate_dialect_sql__coverage_out_of_range_raises():
    """An out-of-range coverage value should fail clearly, not silently."""
    with pytest.raises(ValueError):
        generate("ansi", "SelectStatementSegment", 10, coverage=-1)
    with pytest.raises(ValueError):
        generate("ansi", "SelectStatementSegment", 10, coverage=101)


def test__main__coverage_out_of_range_rejected_by_cli():
    """An out-of-range --coverage should fail argparse validation."""
    with pytest.raises(SystemExit):
        main(
            [
                "--dialect",
                "ansi",
                "--segment",
                "SelectStatementSegment",
                "--coverage",
                "101",
            ]
        )


def test__main__vocab_warnings_print_once_per_segment(capsys):
    """A given "no vocab entry for" name should print at most once per run.

    Not once per occurrence - at high coverage the same gap can otherwise be
    hit hundreds of times in one run (confirmed: this exact scenario went
    from 6679 stderr lines to 134 once deduped).
    """
    main(
        [
            "--dialect",
            "postgres",
            "--segment",
            "SelectStatementSegment",
            "--coverage",
            "100",
            "--max-examples",
            "40",
            "--skip-self-check",
        ]
    )
    warning_lines = [
        line
        for line in capsys.readouterr().err.splitlines()
        if "no vocab entry for" in line
    ]
    assert warning_lines, "expected at least one vocab-gap warning in this scenario"
    assert len(warning_lines) == len(set(warning_lines))


def test__generate_dialect_sql__delimited_produces_multi_item_example():
    """A Delimited list should be exercised with more than one item.

    Regression test for the repetition-count gap: previously every Delimited
    node (comma-separated column lists, SELECT target lists, ...) always
    rendered exactly one item, so multi-item lists were structurally
    unreachable output. ansi's SelectClauseSegment wraps its column list in
    a Delimited(..., allow_trailing=True), so this also exercises the
    trailing-comma variant.
    """
    examples = generate("ansi", "SelectStatementSegment", max_examples=40)
    assert any(", *" in example for example in examples), "no multi-item example"
    assert any(example.rstrip().endswith(",") for example in examples), (
        "no trailing-comma example"
    )


def test__generate_dialect_sql__multi_string_parser_reaches_alternates():
    """A MultiStringParser site should produce more than one distinct keyword.

    Regression test: previously always rendered sorted(templates)[0] and
    never any other member of the closed keyword set.
    """
    examples = generate("snowflake", "DatetimeUnitSegment", max_examples=40)
    assert len({example.strip() for example in examples}) > 1


def test__generate_dialect_sql__terminal_vocab_reaches_alternates():
    """A terminal vocab category should produce more than one distinct value.

    Regression test: TERMINAL_VOCAB/SUFFIX_VOCAB previously mapped every
    name to a single fixed value, so e.g. every generated identifier was
    always literally "foo".
    """
    examples = generate("ansi", "CreateTableStatementSegment", max_examples=40)
    assert any("bar_1" in example for example in examples)


@pytest.mark.parametrize("dialect", ["ansi", "mysql", "mariadb", "sqlite"])
def test__generate_dialect_sql__delete_statement_produces_valid_examples(dialect):
    """Regression test: these produced zero valid examples at all.

    Two compounding bugs, both in grammar shared across many dialects: (1)
    `TableExpressionSegment`'s branch-score tie between `BareFunctionSegment`
    (a bare no-parens function like CURRENT_DATE) and `TableReferenceSegment`
    was silently broken by declaration order, picking the function over a
    real table reference; (2) `Conditional` grammar objects (reflow-only
    Indent/Dedent markers) weren't recognized by the renderer's dispatch, so
    they fell through to the terminal fallback and rendered as a stray "1",
    corrupting otherwise-valid output (e.g. "FROM DUAL 1 1" instead of
    "FROM DUAL"). Confirmed both fixed: this used to generate 0 valid examples
    for all four of these dialects.
    """
    examples = generate(dialect, "DeleteStatementSegment", max_examples=20)
    valid = [sql for sql in examples if self_check(dialect, sql)]
    assert valid, f"no valid DELETE example generated for {dialect}"
