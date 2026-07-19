# ruff: noqa: D101,D102,D103,E402
"""Approach #3: PyLexer vs PyRsLexer differential.

Compares full token streams (raw, type, class, positions, normalization
kwargs, code/whitespace/comment flags) and lexing errors (type, message,
position) between the pure-Python lexer and the Rust-backed lexer.
"""

import sys
import traceback
from pathlib import Path

from sqlfluff.core import FluffConfig
from sqlfluff.core.parser.lexer import PyLexer, PyRsLexer

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "test" / "fixtures" / "dialects"


def token_fp(seg):
    pm = seg.pos_marker
    return {
        "cls": type(seg).__name__,
        "raw": seg.raw,
        "type": seg.get_type(),
        "is_code": seg.is_code,
        "is_whitespace": seg.is_whitespace,
        "is_comment": getattr(seg, "is_comment", None),
        "is_meta": seg.is_meta,
        "instance_types": tuple(getattr(seg, "_instance_types", ()) or ()),
        "trim_chars": getattr(seg, "trim_chars", None),
        "trim_start": getattr(seg, "trim_start", None),
        "quoted_value": getattr(seg, "quoted_value", None),
        "escape_replacements": getattr(seg, "escape_replacements", None),
        "casefold": getattr(seg, "casefold", None),
        "pos": (
            pm.source_slice.start,
            pm.source_slice.stop,
            pm.templated_slice.start,
            pm.templated_slice.stop,
            pm.working_line_no,
            pm.working_line_pos,
        )
        if pm
        else None,
    }


def err_fp(e):
    return (
        type(e).__name__,
        str(e),
        getattr(e, "line_no", None),
        getattr(e, "line_pos", None),
    )


def lex_both(sql, dialect):
    out = {}
    for name, cls in (("py", PyLexer), ("rs", PyRsLexer)):
        config = FluffConfig(overrides={"dialect": dialect})
        try:
            segs, errs = cls(config=config).lex(sql)
            out[name] = {
                "tokens": [token_fp(s) for s in segs],
                "errors": [err_fp(e) for e in errs],
            }
        except BaseException as e:
            out[name] = {"exc": (type(e).__name__, str(e)[:200])}
    return out["py"], out["rs"]


def first_diff(a, b, path="$"):
    if type(a) is not type(b):
        return f"{path}: type {type(a).__name__} != {type(b).__name__} ({a!r:.60} vs {b!r:.60})"
    if isinstance(a, dict):
        for k in list(a.keys()) + [k for k in b if k not in a]:
            if k not in a or k not in b:
                return f"{path}.{k}: missing on one side"
            d = first_diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, (list, tuple)):
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        if len(a) != len(b):
            extra = a[len(b) :] if len(a) > len(b) else b[len(a) :]
            side = "py" if len(a) > len(b) else "rs"
            return f"{path}: len {len(a)} != {len(b)} (extra on {side}: {str(extra)[:120]})"
        return None
    if a != b:
        return f"{path}: {a!r} != {b!r}"
    return None


ADVERSARIAL = [
    # Unterminated / malformed quoting
    ("ansi", "SELECT 'unterminated"),
    ("ansi", 'SELECT "unterminated'),
    ("ansi", "SELECT 'esc\\'aped'"),
    ("ansi", "SELECT ''"),
    ("ansi", "SELECT ''''"),
    ("postgres", "SELECT $tag$body$tag$"),
    ("postgres", "SELECT $tag$unclosed"),
    ("postgres", "SELECT $$empty$$"),
    ("postgres", "SELECT E'\\n\\t'"),
    ("postgres", "SELECT U&'d\\0061t'"),
    ("bigquery", "SELECT r'raw\\string'"),
    ("bigquery", "SELECT b'bytes'"),
    ("bigquery", 'SELECT """triple quoted"""'),
    ("bigquery", "SELECT '''also triple'''"),
    ("mysql", "SELECT `backtick``escaped`"),
    ("mysql", "SELECT x'ABCD', 0xAB"),
    ("mysql", "# hash comment\nSELECT 1"),
    ("tsql", "SELECT [bracket]]escaped]"),
    ("tsql", "SELECT N'unicode str'"),
    ("tsql", "SELECT $123.45, ￥99"),
    ("snowflake", "SELECT $$dollar$$, @stage/path"),
    ("duckdb", "SELECT {'a': 1}"),
    # Comments
    ("ansi", "/* unterminated block"),
    ("ansi", "/* nested /* comment */ end */"),
    ("mysql", "SELECT 1 -- no space needed?"),
    ("mysql", "SELECT 1 --needs space in mysql"),
    ("ansi", "--\n---\n----"),
    # Numbers and dots
    ("ansi", "SELECT 1..2"),
    ("ansi", "SELECT 1.2.3"),
    ("ansi", "SELECT .5, 5., 5.e2, .5e-2, 1e999"),
    ("ansi", "SELECT 0x, 1a2b"),
    # Whitespace / newlines / BOM / control
    ("ansi", "﻿SELECT 1"),
    ("ansi", "SELECT\r\n1\r"),
    ("ansi", "SELECT\v1\f2"),
    ("ansi", "SELECT nbsp"),
    ("ansi", "SELECT ​zwsp"),
    ("ansi", "a\tb\t\tc"),
    # Unlexable runs
    ("ansi", "SELECT \xa1\xa2\xa3 FROM t"),
    ("ansi", "\xa1"),
    ("ansi", "SELECT ¡¡¡¡¡¡¡¡¡¡¡¡¡ FROM t"),
    # Unicode identifiers
    ("ansi", 'SELECT "héllo", 日本語 FROM t'),
    ("bigquery", "SELECT `日本語` FROM t"),
    ("ansi", "SELECT 🔥 FROM t"),
    # Empty / degenerate
    ("ansi", ""),
    ("ansi", " "),
    ("ansi", "\n"),
    ("ansi", ";"),
    # Operators / edge symbols
    ("ansi", "SELECT a::b, a->b, a->>b, a=>b, a||b, a&b, a|b, a^b"),
    ("postgres", "SELECT a @> b, a <@ b, a ?| b, a ?& b, a #>> b"),
    ("clickhouse", "SELECT a ? b : c"),
    ("oracle", "SELECT a(+) FROM t"),
]


def run_adversarial():
    fails = []
    for dialect, sql in ADVERSARIAL:
        try:
            py, rs = lex_both(sql, dialect)
        except BaseException:
            traceback.print_exc()
            fails.append((dialect, sql, "HARNESS"))
            continue
        d = first_diff(py, rs)
        if d:
            fails.append((dialect, sql, d))
            print(f"[DIVERGENCE] {dialect}: {sql[:45]!r}\n    {d}")
        else:
            print(f"[ok] {dialect}: {sql[:45]!r}")
    print(f"adversarial: {len(ADVERSARIAL)} cases, {len(fails)} divergences")
    return fails


def run_fixtures(limit=None):
    files = sorted(FIXTURE_DIR.glob("*/*.sql"))
    if limit:
        files = files[:limit]
    fails = []
    for i, f in enumerate(files):
        sql = f.read_text(encoding="utf-8")
        try:
            py, rs = lex_both(sql, f.parent.name)
        except BaseException:
            fails.append((str(f), "HARNESS"))
            continue
        d = first_diff(py, rs)
        if d:
            fails.append((str(f.relative_to(FIXTURE_DIR)), d))
            print(f"[DIVERGENCE] {f.relative_to(FIXTURE_DIR)}\n    {d}")
        if i % 300 == 0:
            print(f"...{i}/{len(files)}", file=sys.stderr)
    print(f"fixtures: {len(files)} files, {len(fails)} divergences")
    return fails


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "adversarial"):
        run_adversarial()
    if which in ("all", "fixtures"):
        run_fixtures()
