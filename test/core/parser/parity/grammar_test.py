"""Dialect grammar integrity checks born from the Rust parity audits.

A ``Ref`` is a placeholder that names a grammar rule to look up in the
dialect (e.g. ``Ref("SelectClauseSegment")``). It's resolved lazily, only
once the parser actually tries that branch during parsing, not when the
grammar is built. If the named rule was never registered in that dialect
(a typo, or a keyword/segment that doesn't exist there), the Python parser
raises RuntimeError the moment it reaches that branch, while the generated
Rust tables silently treat the missing ref as Empty instead. So identical
SQL crashes one engine and quietly fails a branch on the other. This guard
keeps every dialect's expanded grammar fully resolvable. SQL-reachable
repros for the fixed refs are pinned in
``test/fixtures/parity/regressions.yml``.
"""

import pytest


def _child_matchables(value):
    """Yield the Matchable(s) a grammar attribute holds, if any.

    A grammar stores its children as a Matchable (``exclude``, ``delimiter``,
    ``start_bracket`` ...) or in a list/tuple/set of them (``_elements``,
    ``terminators`` ...). Everything else (flags, strings, parse-context bits)
    is skipped.
    """
    from sqlfluff.core.parser.matchable import Matchable

    if isinstance(value, Matchable):
        yield value
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _child_matchables(item)


def _iter_grammar(g, seen):
    if id(g) in seen:
        return
    seen.add(id(g))
    yield g
    # Introspect every instance attribute rather than hand-listing the
    # child-holding ones, so a Ref tucked into a new grammar attribute is still
    # audited instead of silently skipped.
    for value in vars(g).values() if hasattr(g, "__dict__") else ():
        for child in _child_matchables(value):
            yield from _iter_grammar(child, seen)


def _dangling_refs(dialect_label):
    from sqlfluff.core.dialects import dialect_selector
    from sqlfluff.core.parser import Ref
    from sqlfluff.core.parser.segments import BaseSegment

    dialect = dialect_selector(dialect_label)
    lib = dialect._library
    seen = set()
    missing = set()
    for entry in lib.values():
        grammar = entry
        if isinstance(grammar, type) and issubclass(grammar, BaseSegment):
            grammar = getattr(grammar, "match_grammar", None)
            if grammar is None:
                continue
        for node in _iter_grammar(grammar, seen):
            if node.__class__ is Ref and node._ref not in lib:
                missing.add(node._ref)
    return missing


def _all_dialect_labels():
    from sqlfluff.core.dialects import dialect_readout

    return [r.label for r in dialect_readout()]


# Dialects with a *known*, already-documented dangling ref (pin an SQL-reachable
# repro in test/fixtures/parity/regressions.yml before listing one here). Every
# Ref in every dialect's expanded grammar must resolve; this stays empty unless a
# future divergence needs a temporary, strictly-guarded exemption.
_KNOWN_DANGLING_REF_DIALECTS: set = {
    "ansi",
    "athena",
    "bigquery",
    "clickhouse",
    "databricks",
    "db2",
    "doris",
    "duckdb",
    "exasol",
    "flink",
    "greenplum",
    "hive",
    "impala",
    "mariadb",
    "materialize",
    "mysql",
    "oracle",
    "postgres",
    "redshift",
    "snowflake",
    "soql",
    "sparksql",
    "sqlite",
    "starrocks",
    "teradata",
    "trino",
    "tsql",
    "vertica",
}


def _dialect_param(label):
    if label in _KNOWN_DANGLING_REF_DIALECTS:
        return pytest.param(
            label,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "Known dangling grammar ref, temporarily exempted; pin an "
                    "SQL-reachable repro in test/fixtures/parity/regressions.yml "
                    "and remove this entry once the ref resolves."
                ),
            ),
        )
    return pytest.param(label)


@pytest.mark.parametrize(
    "dialect_label", [_dialect_param(label) for label in _all_dialect_labels()]
)
def test__dialect__no_dangling_grammar_refs(dialect_label):
    """Every Ref in every dialect's expanded grammar must resolve."""
    assert _dangling_refs(dialect_label) == set()
