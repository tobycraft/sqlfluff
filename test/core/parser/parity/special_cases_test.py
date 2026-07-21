"""Parity tests that need Python-side instrumentation.

Most parity coverage is data-driven (see ``cases_test.py`` and
``test/fixtures/parity/``). The tests here can't be expressed as a plain
SQL-plus-config case because they instrument the Python side: codegen
object-lifetime probes, subprocess hash-seed probes.
"""

import gc
import os
import subprocess
import sys
import weakref
from pathlib import Path

import pytest

from .compare import _HAS_RUST_PARSER

_REPO_ROOT = Path(__file__).resolve().parents[4]


def test__parity__codegen_grammar_cache_pins_against_gc():
    """A grammar cached by TableBuilder must not be collectible.

    utils/build_parsers.py's TableBuilder deduplicates flattened grammars in
    ``grammar_to_id``, keyed by Python's id() - the object's raw memory
    address. That's only a valid cache key for as long as the object stays
    alive: once it's garbage-collected, CPython is free to hand that same
    address to an unrelated later grammar, which then silently inherits the
    stale cache entry - wiring a semantically wrong subgrammar into the
    generated Rust dialect tables (seen for real in oracle's
    ``Ref("AttributeIndicatorSegment")`` picking up a dead
    ``Ref("ModuloSegment")`` entry). The builder must keep every grammar it
    caches alive for its own lifetime so no id() in ``grammar_to_id`` can
    ever be freed and reused.
    """
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from sqlfluff.core.dialects import dialect_selector
    from sqlfluff.core.parser.grammar.base import Ref
    from utils.build_parsers import DummyParseContext, TableBuilder

    dialect = dialect_selector("ansi")
    ctx = DummyParseContext(dialect=dialect, uuid=0)
    builder = TableBuilder()

    grammar = Ref("SelectStatementSegment")
    alive = weakref.ref(grammar)
    builder.flatten_grammar(grammar, ctx)

    del grammar
    gc.collect()

    assert alive() is not None, (
        "TableBuilder let a cached grammar get garbage-collected - its "
        "id() can now be reused by an unrelated grammar, silently aliasing "
        "it to this stale GrammarId."
    )


def test__parity__codegen_lexer_patterns_hash_seed_stable():
    """Dialect lexer regexes are byte-identical across interpreter hash seeds.

    tsql's money-literal lexer pattern is assembled from an unordered set of
    currency symbols via ``"".join(...)`` with no sorting, so its bytes
    shuffle with the interpreter's hash seed - unlike every other
    ``dialect.sets(...)``-derived pattern in the codebase, which already
    sorts first. That makes the generated Rust lexer file (produced by
    utils/rustify.py) unreproducible: rebuilding with a different hash seed
    silently changes the checked-in output's byte content, even though the
    character class matches exactly the same input either way.
    """
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


@pytest.mark.skipif(not _HAS_RUST_PARSER, reason="Rust parser not available")
@pytest.mark.xfail(
    strict=True,
    reason="parse_capture only forces the native-AST flag ON for 'rust-native'; "
    "the 'rust' leg inherits the ambient SQLFLUFF_RS_NATIVE_AST global, so with "
    "it set the legacy leg silently runs the native builder.",
)
def test__parity__rust_leg_forces_legacy_native_ast():
    """The ``rust`` leg must parse with the native-AST builder OFF regardless of
    the ambient ``SQLFLUFF_RS_NATIVE_AST`` global.

    Otherwise ``native_vs_legacy`` compares the native builder against itself
    (vacuous, and every strict xfail pinned to that leg reports XPASS) and
    ``python_vs_rust`` silently tests python-vs-native while claiming to test
    the legacy convert+apply path. The flag the ``rust`` leg parses under must
    be pinned to ``False`` by ``parse_capture`` itself, not left to the
    environment.
    """
    from sqlfluff.core.parser import Lexer
    from sqlfluff.core.parser.rust_parser import get_native_ast, set_native_ast

    from . import compare

    config = compare.build_config("ansi", None)
    segments, _ = Lexer(config=config).lex("SELECT 1 FROM t")

    seen = {}
    real_parser_cls = compare.RustParser

    class _SpyParser(real_parser_cls):
        def parse(self, *args, **kwargs):
            seen["native_at_parse"] = get_native_ast()
            return super().parse(*args, **kwargs)

    previous = get_native_ast()
    set_native_ast(True)  # simulate SQLFLUFF_RS_NATIVE_AST=1
    try:
        compare.RustParser = _SpyParser
        compare.parse_capture("rust", config, segments)
    finally:
        compare.RustParser = real_parser_cls
        set_native_ast(previous)

    assert seen["native_at_parse"] is False, (
        "the 'rust' (legacy) leg parsed with the native-AST builder enabled - "
        "parse_capture leaked the ambient native flag instead of forcing it off"
    )
