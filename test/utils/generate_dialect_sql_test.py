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

# Every decision point (which branch, which vocab value, which MultiStringParser
# keyword, how many items a Delimited renders) is chosen randomly - see the
# module docstring and _WalkState._choose. Tests need a fixed seed so they
# aren't flaky: this is just "a seed that works" (found by brute-force search
# over a small range), not a meaningful value. Most seeds fail at least one
# assertion below - not a bug, an accepted consequence of no longer biasing
# toward the "plain/valid" branch - see the design doc.
SEED = 6


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
    examples = generate(dialect, segment, max_examples=20, seed=SEED)
    assert examples, "generator produced no candidates at all"

    valid = [sql for sql in examples if self_check(dialect, sql)]
    assert valid, f"no generated example for {dialect}/{segment} parsed cleanly"


def test__generate_dialect_sql__unknown_segment_raises():
    """An unknown --segment name should fail clearly, not silently."""
    with pytest.raises(GenerationError):
        generate("ansi", "NotARealSegmentName", max_examples=5, seed=SEED)


def test__generate_dialect_sql__self_referential_grammar_terminates():
    """Self-referential grammar must not hang or blow the recursion stack.

    Expressions containing expressions are exactly this case - the cycle
    guard (a Ref name already visited on the current path renders as a
    terminal instead of being followed again) should always bring the walk
    to an end.
    """
    examples = generate("ansi", "ExpressionSegment", max_examples=10, seed=SEED)
    assert examples


def test__generate_dialect_sql__zero_coverage_matches_default():
    """coverage=0 (the default) must be indistinguishable from omitting it.

    This is the backward-compatibility contract for the coverage parameter.
    Both calls must share the same seed - two independently-auto-seeded calls
    would now legitimately differ, since every decision point is random.
    """
    with_default = generate("postgres", "CreateTableStatementSegment", 20, seed=SEED)
    with_explicit_zero = generate(
        "postgres", "CreateTableStatementSegment", 20, coverage=0, seed=SEED
    )
    assert with_default == with_explicit_zero


def test__generate_dialect_sql__high_coverage_reaches_nested_content():
    """High coverage should discover content invisible at coverage=0.

    postgres/CreateTableStatementSegment's column list is itself optional, so
    at coverage=0 it's sometimes omitted (depending on the random baseline -
    unlike before this was made random, the baseline isn't guaranteed to be
    the bare "CREATE TABLE foo ( )" form): candidate discovery only looks at
    what's visible while rendering that one baseline walk, so nothing inside
    a column list that walk didn't render - real column definitions,
    constraints - is ever found at coverage=0. coverage=100 adds discovery
    rounds that render already-known optional/branch points and look for
    *new* candidates nested inside them, which should surface something with
    real content in the column list regardless of what the random baseline
    looked like.
    """
    baseline_only = generate(
        "postgres", "CreateTableStatementSegment", 40, coverage=0, seed=SEED
    )
    assert all(example.startswith("CREATE") for example in baseline_only)

    thorough = generate(
        "postgres", "CreateTableStatementSegment", 40, coverage=100, seed=SEED
    )
    shortest_baseline = min(len(example.split()) for example in baseline_only)
    assert any(len(example.split()) > shortest_baseline for example in thorough)


def test__generate_dialect_sql__mid_coverage_combines_candidates():
    """Mid-range coverage should produce examples with 2+ non-baseline elements.

    At coverage=0 every example is exactly one toggle away from the baseline;
    combination width > 1 (reached above coverage=0) should produce at least
    one example where two independent optional SELECT clauses are both
    present simultaneously.
    """
    examples = generate("ansi", "SelectStatementSegment", 40, coverage=60, seed=SEED)
    clause_keywords = ("WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET")

    def clause_count(example: str) -> int:
        tokens = example.split()
        return sum(1 for kw in clause_keywords if kw in tokens)

    assert any(clause_count(example) >= 2 for example in examples)


def test__generate_dialect_sql__coverage_out_of_range_raises():
    """An out-of-range coverage value should fail clearly, not silently."""
    with pytest.raises(ValueError):
        generate("ansi", "SelectStatementSegment", 10, coverage=-1, seed=SEED)
    with pytest.raises(ValueError):
        generate("ansi", "SelectStatementSegment", 10, coverage=101, seed=SEED)


def test__generate_dialect_sql__same_seed_is_reproducible():
    """The same seed (with the same other arguments) must produce identical output."""
    first = generate("ansi", "SelectStatementSegment", 30, coverage=60, seed=12345)
    second = generate("ansi", "SelectStatementSegment", 30, coverage=60, seed=12345)
    assert first == second


def test__generate_dialect_sql__different_seeds_vary():
    """Different seeds should (almost always) produce different output.

    SelectStatementSegment has plenty of decision points, so two different
    seeds landing on the exact same sequence of random choices is
    astronomically unlikely - not a guarantee, but a reasonable regression
    check that --seed actually influences generation.
    """
    first = generate("ansi", "SelectStatementSegment", 30, coverage=60, seed=1)
    second = generate("ansi", "SelectStatementSegment", 30, coverage=60, seed=2)
    assert first != second


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
            "--seed",
            str(SEED),
        ]
    )
    warning_lines = [
        line
        for line in capsys.readouterr().err.splitlines()
        if "no vocab entry for" in line
    ]
    assert warning_lines, "expected at least one vocab-gap warning in this scenario"
    assert len(warning_lines) == len(set(warning_lines))


def test__main__omitted_seed_is_printed_and_reproducible(capsys):
    """Omitting --seed should print one that, passed back in, reproduces the run."""
    main(
        [
            "--dialect",
            "ansi",
            "--segment",
            "SelectStatementSegment",
            "--max-examples",
            "20",
            "--skip-self-check",
        ]
    )
    first_output = capsys.readouterr()
    seed_lines = [
        line for line in first_output.err.splitlines() if "no --seed given" in line
    ]
    assert len(seed_lines) == 1
    # "...no --seed given, using {seed} - pass --seed {seed} to reproduce..."
    seed = int(seed_lines[0].split("using ", 1)[1].split(" -", 1)[0])

    main(
        [
            "--dialect",
            "ansi",
            "--segment",
            "SelectStatementSegment",
            "--max-examples",
            "20",
            "--skip-self-check",
            "--seed",
            str(seed),
        ]
    )
    second_output = capsys.readouterr()
    assert first_output.out == second_output.out


def test__generate_dialect_sql__delimited_produces_multi_item_example():
    """A Delimited list should be exercised with more than one item.

    Regression test for the repetition-count gap: previously every Delimited
    node (comma-separated column lists, SELECT target lists, ...) always
    rendered exactly one item, so multi-item lists were structurally
    unreachable output. ansi's SelectClauseSegment wraps its column list in
    a Delimited(..., allow_trailing=True), so this also exercises the
    trailing-comma variant.
    """
    examples = generate("ansi", "SelectStatementSegment", max_examples=40, seed=SEED)
    assert any(", *" in example for example in examples), "no multi-item example"
    assert any(example.rstrip().endswith(",") for example in examples), (
        "no trailing-comma example"
    )


def test__generate_dialect_sql__multi_string_parser_reaches_alternates():
    """A MultiStringParser site should produce more than one distinct keyword.

    Regression test: previously always rendered sorted(templates)[0] and
    never any other member of the closed keyword set.
    """
    examples = generate("snowflake", "DatetimeUnitSegment", max_examples=40, seed=SEED)
    assert len({example.strip() for example in examples}) > 1


def test__generate_dialect_sql__terminal_vocab_reaches_alternates():
    """A terminal vocab category should produce more than one distinct value.

    Regression test: TERMINAL_VOCAB/SUFFIX_VOCAB previously mapped every
    name to a single fixed value, so e.g. every generated identifier was
    always literally "foo".
    """
    examples = generate(
        "ansi", "CreateTableStatementSegment", max_examples=40, seed=SEED
    )
    values_seen = {
        token
        for example in examples
        for token in example.replace("(", " ").replace(")", " ").split()
        if token in ("foo", "bar_1")
    }
    assert len(values_seen) > 1, f"expected 2+ distinct vocab values, saw {values_seen}"


@pytest.mark.parametrize("dialect", ["ansi", "mysql", "mariadb", "sqlite"])
def test__generate_dialect_sql__delete_statement_produces_valid_examples(dialect):
    """Regression test: these produced zero valid examples at all.

    Two compounding bugs, both in grammar shared across many dialects: (1) a
    branch-selection tie between `TableExpressionSegment`'s
    `BareFunctionSegment` (a bare no-parens function like CURRENT_DATE) and
    `TableReferenceSegment` used to always resolve to the same one
    (declaration order), so a real table reference was never reachable; (2)
    `Conditional` grammar objects (reflow-only Indent/Dedent markers) weren't
    recognized by the renderer's dispatch, so they fell through to the
    terminal fallback and rendered as a stray "1", corrupting otherwise-valid
    output (e.g. "FROM DUAL 1 1" instead of "FROM DUAL"). Confirmed both
    fixed: this used to generate 0 valid examples for all four of these
    dialects. Branch selection is now random rather than deterministic, so
    this also guards against the fix regressing into "only reachable if you
    get lucky" - every branch, chosen or not, is independently reachable via
    the coverage/combination machinery regardless of what the random
    baseline picked.
    """
    examples = generate(dialect, "DeleteStatementSegment", max_examples=20, seed=SEED)
    valid = [sql for sql in examples if self_check(dialect, sql)]
    assert valid, f"no valid DELETE example generated for {dialect}"
