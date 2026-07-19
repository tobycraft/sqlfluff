"""Parity tests that need Python-side instrumentation.

Most parity coverage is data-driven (see ``cases_test.py`` and
``test/fixtures/parity/``). The tests here can't be expressed as a plain
SQL-plus-config case because they instrument the Python side: log capture,
parse-node accounting hooks, shared-instance state, subprocess hash-seed
probes. They still use the shared capture machinery from ``compare.py``
wherever a comparison is made.
"""

import pytest

from .compare import (
    _exception_capture,
    _tree_capture,
    build_config,
    parse_capture,
    requires_rust_parser,
)

try:
    from sqlfluff.core.parser.rust_parser import RustParser
except ImportError:  # pragma: no cover
    RustParser = None


def _native_and_legacy(sql, dialect="ansi", configs=None):
    """Capture the same lexed SQL through both RustParser build paths."""
    from sqlfluff.core.parser import Lexer

    config = build_config(dialect=dialect, configs=configs)
    segments, _ = Lexer(config=config).lex(sql)
    return (
        parse_capture("rust-native", config, segments),
        parse_capture("rust", config, segments),
    )


@requires_rust_parser
def test__parity__native_ast_recursion_depth_parity():
    """Both tree-building paths tolerate the same bracket-nesting depth.

    Regression test: _convert_rs_match_result (the native_ast=False tree
    builder) used to recurse through an extra generator-expression stack
    frame per nesting level that _apply_rs_match_result (the fused
    native_ast=True builder) doesn't have, so the legacy path blew the
    Python call stack roughly twice as early as the fused path for the same
    deeply-nested input: with the depth guard raised out of the way
    (max_parse_depth=2000; at the default of 600 the depth guard fires
    first on both paths identically, masking the divergence), 70 levels of
    bracket nesting built a tree on the fused path but raised
    RecursionError on the legacy path. The converter now builds child
    matches with an explicit loop (one interpreter frame per level, like
    MatchResult.apply and the fused builder), so both paths succeed here
    and keep failing in lockstep (both RecursionError) at much deeper
    nesting, past the shared stack budget.
    """
    sql = "SELECT " + "(" * 70 + "1" + ")" * 70
    native_capture, legacy_capture = _native_and_legacy(
        sql, configs={"max_parse_depth": 2000}
    )
    assert native_capture == legacy_capture
    # Depth 70 is comfortably within the (now shared) stack budget: the
    # parity above must come from both paths building the tree, not from
    # both failing.
    assert legacy_capture[0] == "tree"


@requires_rust_parser
def test__parity__native_ast_alternating_modes_shared_instance():
    """Alternating build paths on ONE parser instance stays byte-stable.

    Both paths share per-instance state (the _get_segment_class_by_name
    lru_cache, the RsParser handle) and module-global state (the native_ast
    flag). Parsing the same lexed segments repeatedly while flipping the flag
    between parses must give the same result every time - no cross-mode cache
    pollution or state leakage.
    """
    from sqlfluff.core.parser import Lexer
    from sqlfluff.core.parser.rust_parser import set_native_ast

    config = build_config()
    parser = RustParser(config=config)
    lexer = Lexer(config=config)
    for sql in (
        "SELECT a FROM t",
        "SELECT CASE",
        "WITH x AS (SELECT 1) SELECT * FROM x",
    ):
        segments, _ = lexer.lex(sql)
        results = []
        for native in (False, True, False, True):
            set_native_ast(native)
            try:
                results.append(_tree_capture(parser.parse(segments, fname="t.sql")))
            except BaseException as err:
                results.append(_exception_capture(err))
            finally:
                set_native_ast(False)
        assert all(r == results[0] for r in results), sql


@requires_rust_parser
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Not yet fixed: the fused native-AST builder skips the legacy path's "
        "parser_logger.info('Root Match:...') diagnostic entirely. Fixed by "
        "the upcoming RustParser wrapper parity commit."
    ),
)
def test__parity__native_ast_root_match_logging_parity(caplog):
    r"""Both build paths emit byte-identical parser INFO diagnostics.

    Regression test: the fused native-AST builder used to skip the legacy
    path's ``parser_logger.info("Root Match:\\n%s", match)`` diagnostic
    entirely, so parsing the same SQL with parser logging enabled (e.g.
    ``sqlfluff parse -vvvv``) produced different diagnostic bytes depending
    on the native_ast flag. The native path now emits the identical record,
    building the intermediate MatchResult it needs for the message only when
    INFO logging is actually enabled (so the fused path stays conversion-free
    in normal operation - see
    test__rust_parser__native_ast_profile_has_no_convert_stage).
    """
    import logging

    from sqlfluff.core.parser import Lexer
    from sqlfluff.core.parser.rust_parser import set_native_ast

    config = build_config()
    segments, _ = Lexer(config=config).lex("SELECT a, b FROM t WHERE a = 1")

    def parser_log_messages(native):
        caplog.clear()
        set_native_ast(native)
        try:
            with caplog.at_level(logging.INFO, logger="sqlfluff.parser"):
                RustParser(config=config).parse(segments, fname="t.sql")
        finally:
            set_native_ast(False)
        return [rec.getMessage() for rec in caplog.records]

    legacy_messages = parser_log_messages(native=False)
    native_messages = parser_log_messages(native=True)

    # The legacy path logs the root match; the native path must too.
    assert any(msg.startswith("Root Match:") for msg in legacy_messages)
    assert native_messages == legacy_messages


@requires_rust_parser
def test__parity__native_ast_parse_node_accounting_parity(monkeypatch):
    """Both build paths make identical parse-node accounting increments.

    The max_parse_nodes budget (a DoS guard) is charged as the BaseSegment
    tree is built - in MatchResult.apply on the legacy path and in
    _apply_rs_match_result on the fused path. If the two paths counted
    differently, the same SQL with the same max_parse_nodes limit could
    parse under one flag and raise SQLParseError under the other. Assert
    the final consumed budget is identical, and that behaviour at the exact
    budget boundary (smallest passing limit, and one below it) is
    byte-identical - the boundary probe is driven purely through SQL plus
    config, exactly how a user would hit it.
    """
    from sqlfluff.core.parser.context import ParseContext

    captured = []
    orig_from_config = ParseContext.from_config.__func__

    def _capturing_from_config(cls, config):
        ctx = orig_from_config(cls, config)
        captured.append(ctx)
        return ctx

    monkeypatch.setattr(
        ParseContext, "from_config", classmethod(_capturing_from_config)
    )

    sql = "SELECT a, b FROM t WHERE x = 1"

    captured.clear()
    _native_and_legacy(sql)
    # _native_and_legacy parses native-first: one context per parse.
    assert len(captured) == 2
    native_count, legacy_count = (ctx.current_parse_nodes for ctx in captured)
    assert native_count == legacy_count

    # Boundary behaviour: find the smallest max_parse_nodes that lets the
    # legacy path build the tree (the effective floor may be enforced by
    # either the shared Rust core or the Python-side ParseContext budget),
    # then require the native path to agree byte-for-byte both at that
    # limit and just below it (where both must raise the same SQLParseError).
    lo, hi = 1, 4000
    while lo < hi:
        mid = (lo + hi) // 2
        _, legacy_capture = _native_and_legacy(sql, configs={"max_parse_nodes": mid})
        if legacy_capture[0] == "tree":
            hi = mid
        else:
            lo = mid + 1
    for limit in (lo, lo - 1):
        native_capture, legacy_capture = _native_and_legacy(
            sql, configs={"max_parse_nodes": limit}
        )
        assert native_capture == legacy_capture
    # And the boundary is real: passing at the floor, SQLParseError below.
    assert _native_and_legacy(sql, configs={"max_parse_nodes": lo})[1][0] == "tree"
    below = _native_and_legacy(sql, configs={"max_parse_nodes": lo - 1})[1]
    assert below[0] == "exc" and below[1] == "SQLParseError"


@requires_rust_parser
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known gap: the max_parse_nodes budget is enforced twice with "
        "different counting semantics - the Rust core counts its internal "
        "match-tree nodes and raises before Python-side building, while the "
        "pure-Python parser counts materialized parse nodes. For the same "
        "SQL the minimal passing limit differs (e.g. 46 vs 50 for a simple "
        "SELECT), so limits in that window parse under one engine and raise "
        "SQLParseError under the other."
    ),
)
def test__parity__vs_python_max_parse_nodes_threshold():
    """The same max_parse_nodes limit should behave identically on both engines."""
    from sqlfluff.core.parser import Lexer

    sql = "SELECT a, b FROM t WHERE x = 1"

    def outcome(engine, limit):
        config = build_config(configs={"max_parse_nodes": limit})
        segments, _ = Lexer(config=config).lex(sql)
        return parse_capture(engine, config, segments)[:2]

    # Find Python's minimal passing limit, then require Rust to agree at
    # that limit and one below it.
    lo, hi = 1, 4000
    while lo < hi:
        mid = (lo + hi) // 2
        if outcome("python", mid)[0] == "tree":
            hi = mid
        else:
            lo = mid + 1
    assert outcome("rust", lo) == outcome("python", lo)
    assert outcome("rust", lo - 1) == outcome("python", lo - 1)


@requires_rust_parser
def test__parity__int_typed_indentation_config_stays_int():
    """The config layer stores bool-ish indentation values as int, not bool.

    The pinned parity case ``config_parity.yml:int_typed_indentation_config``
    exists because RustParser used to drop int-typed (non-bool) indentation
    settings. This companion guard asserts the config layer still produces
    the int typing that makes that case exercise what it was written for.
    """
    config = build_config()
    config.set_value(["indentation", "indented_joins"], True)
    stored = config.get_section("indentation")["indented_joins"]
    assert stored == 1 and not isinstance(stored, bool)


def test__parity__codegen_lexer_patterns_hash_seed_stable():
    """Dialect lexer regexes are byte-identical across interpreter hash seeds.

    Regression test for the tsql money-literal lexer pattern, which was
    assembled from an unordered set of currency symbols: its bytes changed
    with the interpreter's hash seed, so the generated Rust lexer file
    (snapshotted by utils/rustify.py) could never reproduce and the codegen
    freshness check failed spuriously. The full matcher list is compared so
    any future set-ordered pattern in ANY tsql matcher is caught too.
    """
    import os
    import subprocess
    import sys

    script = (
        "from sqlfluff.core.dialects import dialect_selector\n"
        "d = dialect_selector('tsql')\n"
        "for m in d.lexer_matchers:\n"
        "    print(m.name, repr(getattr(m, 'template', None)))\n"
    )
    outputs = set()
    for seed in ("0", "1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            env=env,
            check=True,
            timeout=120,
        )
        outputs.add(proc.stdout)
    assert len(outputs) == 1, "lexer matcher patterns vary with the hash seed"


@requires_rust_parser
@pytest.mark.parametrize(
    "method,is_datatype_method",
    [
        ("value", True),  # T-SQL data-type methods are case-SENSITIVE (lowercase)
        ("query", True),
        ("VALUE", False),  # upper/mixed case is NOT a data-type method
        ("Value", False),
        ("QUERY", False),
    ],
)
def test__parity__tsql_datatype_method_case_sensitive(method, is_datatype_method):
    """Rust parser honors ``ignore_case=False``.

    ``col.value(...)`` is a data-type method (case-sensitive, lowercase only);
    ``col.VALUE(...)`` / ``col.Value(...)`` are not. The Rust parser must match
    Python here, and the semantic direction is asserted too (not just parity).
    """
    from sqlfluff.core import FluffConfig, Linter

    src = f"SELECT col.{method}('/x', 'y') FROM t;\n"

    def method_ids(rust):
        cfg = FluffConfig(
            overrides={
                "dialect": "tsql",
                "use_rust_parser": rust,
                "use_rust_engine": False,
            }
        )
        tree = Linter(config=cfg).parse_string(src).tree
        return [s.raw for s in tree.recursive_crawl("datatype_method_name_identifier")]

    rust_ids = method_ids(True)
    python_ids = method_ids(False)
    assert rust_ids == python_ids
    assert (method in rust_ids) is is_datatype_method
