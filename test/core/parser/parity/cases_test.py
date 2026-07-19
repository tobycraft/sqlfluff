"""Drivers for the parity case corpus in ``test/fixtures/parity/``.

Each YAML case is pure data - SQL (inline or a dialect-corpus fixture),
dialect, config and metadata. The capture/compare machinery lives in
``compare.py`` and is shared by every driver, so all cases are guarded at
the same (maximal) strictness; known divergences are declared per-case,
per-leg in the fixtures as strict xfails.
"""

import pytest

from .compare import (
    PARSER_LEGS,
    build_config,
    check_expectations,
    lex_capture,
    linted_parse_capture,
    load_case_params,
    parse_capture,
    raw_match_violations,
    requires_rust_parser,
    resolve_case_sql,
)


@requires_rust_parser
@pytest.mark.parametrize("case,leg", load_case_params("parser", legs=list(PARSER_LEGS)))
def test__parity__parser_case(case, leg):
    """Both engines of the leg produce byte-identical parse results."""
    sql = resolve_case_sql(case)
    config = build_config(case.get("dialect", "ansi"), case.get("configs"))

    if case.get("templater"):
        left_engine, right_engine = PARSER_LEGS[leg]
        left = linted_parse_capture(
            left_engine,
            sql,
            dialect=case.get("dialect", "ansi"),
            configs=case.get("configs"),
            context=case.get("context"),
        )
        right = linted_parse_capture(
            right_engine,
            sql,
            dialect=case.get("dialect", "ansi"),
            configs=case.get("configs"),
            context=case.get("context"),
        )
        assert left == right
        return

    from sqlfluff.core.parser import Lexer

    # Both engines parse the SAME lexed segments: this isolates parser
    # parity from lexer parity (which has its own leg).
    segments, _ = Lexer(config=config).lex(sql)
    left_engine, right_engine = PARSER_LEGS[leg]
    left = parse_capture(left_engine, config, segments)
    right = parse_capture(right_engine, config, segments)
    assert left == right
    if leg == "python_vs_rust":
        check_expectations(case, right)


@requires_rust_parser
@pytest.mark.parametrize("case,leg", load_case_params("lexer", legs=["lexer"]))
def test__parity__lexer_case(case, leg):
    """PyRsLexer's token stream and errors match PyLexer's byte-for-byte."""
    sql = resolve_case_sql(case)
    config = build_config(case.get("dialect", "ansi"), case.get("configs"))
    assert lex_capture("rust", config, sql) == lex_capture("python", config, sql)


@requires_rust_parser
@pytest.mark.parametrize(
    "case,leg", load_case_params("invariants", legs=["invariants"])
)
def test__parity__match_result_invariants(case, leg):
    """Returned RsMatchResults must be structurally well-formed."""
    sql = resolve_case_sql(case)
    assert raw_match_violations(sql, case.get("dialect", "ansi")) == []
