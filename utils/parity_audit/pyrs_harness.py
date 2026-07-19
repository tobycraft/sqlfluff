# ruff: noqa: D101,D102,D103,E402
"""Python-Parser vs RustParser strict differential harness.

Compares, per (sql, dialect): tree tuple WITH positions, stringify bytes,
raw round-trip, and exception type+message+attributes. Node accounting is
excluded here (engines count differently by design; see accounting probe).
"""

import re
import sys
import traceback
from pathlib import Path

from sqlfluff.core import FluffConfig
from sqlfluff.core.parser import Lexer, Parser
from sqlfluff.core.parser.rust_parser import RustParser

assert RustParser is not None

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "test" / "fixtures" / "dialects"

_BLOCK_UUID_RE = re.compile(r"Block: '[0-9a-f]+'")


def _norm(s):
    return _BLOCK_UUID_RE.sub("Block: '<uuid>'", s) if isinstance(s, str) else s


def parse_one(sql, dialect, use_rust, overrides=None):
    ov = {"dialect": dialect}
    if overrides:
        ov.update(overrides)
    config = FluffConfig(overrides=ov)
    segments, _ = Lexer(config=config).lex(sql)
    cls = RustParser if use_rust else Parser
    try:
        tree = cls(config=config).parse(segments, fname="t.sql")
        if tree is None:
            return {"kind": "none"}
        return {
            "kind": "tree",
            "tuple": tree.to_tuple(
                code_only=False,
                show_raw=True,
                include_meta=True,
                include_position=True,
            ),
            "stringify": _norm(tree.stringify()),
            "raw": tree.raw,
        }
    except BaseException as err:
        return {
            "kind": "exc",
            "type": type(err).__name__,
            "str": str(err),
            "line_no": getattr(err, "line_no", None),
            "line_pos": getattr(err, "line_pos", None),
            "fatal": getattr(err, "fatal", None),
            "ignore": getattr(err, "ignore", None),
            "warning": getattr(err, "warning", None),
        }


def diff_result(a, b):
    diffs = {}
    for k in set(a) | set(b):
        if a.get(k) != b.get(k):
            diffs[k] = (a.get(k), b.get(k))
    return diffs


def compare(sql, dialect, overrides=None):
    py = parse_one(sql, dialect, use_rust=False, overrides=overrides)
    rs = parse_one(sql, dialect, use_rust=True, overrides=overrides)
    return diff_result(py, rs), py, rs


def first_str_diff(a, b):
    """Locate first differing line between two stringify outputs."""
    la, lb = (a or "").splitlines(), (b or "").splitlines()
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<missing>"
        y = lb[i] if i < len(lb) else "<missing>"
        if x != y:
            return f"line {i}: py={x[:110]!r} rs={y[:110]!r}"
    return None


def run_fixtures(limit=None, offset=0):
    files = sorted(FIXTURE_DIR.glob("*/*.sql"))[offset:]
    if limit:
        files = files[:limit]
    fails = 0
    for i, f in enumerate(files):
        dialect = f.parent.name
        sql = f.read_text(encoding="utf-8")
        try:
            diffs, py, rs = compare(sql, dialect)
        except BaseException:
            print(f"[HARNESS] {f}")
            traceback.print_exc()
            fails += 1
            continue
        if diffs:
            fails += 1
            detail = ""
            if "stringify" in diffs:
                detail = first_str_diff(py.get("stringify"), rs.get("stringify"))
            elif "kind" in diffs:
                detail = f"kind {py['kind']} vs {rs['kind']}: {str(py)[:80]} | {str(rs)[:80]}"
            elif "str" in diffs:
                detail = f"exc str: py={str(py.get('str'))[:100]!r} rs={str(rs.get('str'))[:100]!r}"
            print(f"[DIVERGENCE] {f.relative_to(FIXTURE_DIR)}: fields={sorted(diffs)}")
            if detail:
                print(f"    {detail}")
        if i % 100 == 0:
            print(f"...{i}/{len(files)}", file=sys.stderr)
    print(f"py-vs-rs strict fixtures: {len(files)} files, {fails} divergences")
    return fails


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    offset = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    run_fixtures(limit, offset)
