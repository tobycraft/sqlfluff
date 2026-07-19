# ruff: noqa: D101,D102,D103,E402
"""Step 1: attribute-level deep comparison of trees from the two build paths.

Goes beyond to_tuple/stringify: parent wiring, per-node internal attributes,
cache-derived properties, traversal orders, deepcopy round-trips.
"""

import copy
import random
import sys
from pathlib import Path

from sqlfluff.core import FluffConfig
from sqlfluff.core.parser import Lexer
from sqlfluff.core.parser.rust_parser import RustParser, set_native_ast

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "test" / "fixtures" / "dialects"

assert RustParser is not None


def node_fingerprint(seg, parent_path):
    """Everything observable about one node except random uuids/objects."""
    pm = seg.pos_marker
    fp = {
        "cls": type(seg).__name__,
        "type": seg.get_type(),
        "raw": seg.raw,
        "is_code": seg.is_code,
        "is_meta": seg.is_meta,
        "is_whitespace": seg.is_whitespace,
        "class_types": tuple(sorted(seg.class_types)),
        "instance_types": tuple(getattr(seg, "_instance_types", ()) or ()),
        "trim_chars": getattr(seg, "trim_chars", None),
        "casefold": getattr(seg, "casefold", None),
        "quoted_value": getattr(seg, "quoted_value", None),
        "escape_replacements": getattr(seg, "escape_replacements", None),
        "normalized_raw": getattr(seg, "raw_normalized", lambda: None)()
        if callable(getattr(seg, "raw_normalized", None))
        else None,
        "pos": None,
        "descendants": tuple(sorted(seg.descendant_type_set)),
        "direct_descendants": tuple(sorted(seg.direct_descendant_type_set)),
        "n_children": len(seg.segments),
        "parent_path": parent_path,
    }
    if pm:
        fp["pos"] = (
            pm.source_slice.start,
            pm.source_slice.stop,
            pm.templated_slice.start,
            pm.templated_slice.stop,
            pm.working_line_no,
            pm.working_line_pos,
        )
    # Parent link consistency (weakref wiring).
    gp = seg.get_parent()
    fp["parent_link"] = None if gp is None else (type(gp[0]).__name__, gp[1])
    # Source fixes / templated flags where present.
    fp["is_templated"] = getattr(seg, "is_templated", None)
    sf = getattr(seg, "source_fixes", None)
    fp["n_source_fixes"] = len(sf) if sf else 0
    return fp


def tree_profile(tree):
    """Ordered attribute-level profile of the whole tree + derived views."""
    nodes = []

    def walk(seg, path):
        nodes.append(node_fingerprint(seg, path))
        for i, child in enumerate(seg.segments):
            walk(child, path + (i,))

    walk(tree, ())
    profile = {
        "nodes": nodes,
        "raw": tree.raw,
        "raw_segments": [
            (type(s).__name__, s.raw, s.get_type()) for s in tree.raw_segments
        ],
        "crawl_all_types": [s.get_type() for s in tree.recursive_crawl_all()],
        "start_loc": tree.get_start_loc(),
        "end_loc": tree.get_end_loc(),
        # path_to for a few interesting leaves (first/last/mid raw).
        "path_to": [],
    }
    raws = tree.raw_segments
    for pick in {0, len(raws) // 2, len(raws) - 1} if raws else set():
        steps = tree.path_to(raws[pick])
        profile["path_to"].append([(type(st.segment).__name__, st.idx) for st in steps])
    # deepcopy round-trip must preserve the serialized form.
    try:
        cp = copy.deepcopy(tree)
        profile["deepcopy_tuple"] = cp.to_tuple(
            code_only=False, show_raw=True, include_meta=True, include_position=True
        )
        profile["copy_tuple"] = tree.copy().to_tuple(
            code_only=False, show_raw=True, include_meta=True, include_position=True
        )
    except BaseException as e:
        profile["deepcopy_tuple"] = ("exc", type(e).__name__, str(e))
    return profile


def build(sql, dialect, native):
    config = FluffConfig(overrides={"dialect": dialect})
    segments, _ = Lexer(config=config).lex(sql)
    set_native_ast(native)
    try:
        tree = RustParser(config=config).parse(segments, fname="t.sql")
        if tree is None:
            return {"kind": "none"}
        return {"kind": "tree", "profile": tree_profile(tree)}
    except BaseException as err:
        return {"kind": "exc", "type": type(err).__name__, "str": str(err)}
    finally:
        set_native_ast(False)


def first_diff(a, b, path="$"):
    if type(a) is not type(b):
        return f"{path}: type {type(a).__name__} != {type(b).__name__}"
    if isinstance(a, dict):
        for k in set(a) | set(b):
            if k not in a or k not in b:
                return f"{path}.{k}: missing on one side"
            d = first_diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return f"{path}: len {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    if a != b:
        return f"{path}: {a!r} != {b!r}"
    return None


CASES = [
    ("ansi", "SELECT a, b FROM t WHERE x = 1"),
    ("ansi", "SELECT CASE"),
    ("ansi", "SELECT 1) FROM t"),
    ("ansi", "SELECT a FROM t WHERE a IN (1, )"),
    ("ansi", "SELECT 1; !!!! ; SELECT 2"),
    ("ansi", "-- just a comment\n"),
    ("ansi", "SELECT rank() OVER (PARTITION BY a ORDER BY b) FROM t"),
    ("ansi", "WITH x AS (SELECT 1) SELECT * FROM x JOIN y ON x.a = y.a"),
    ("snowflake", "SELECT OBJECT_CONSTRUCT('a', 1), $1 FROM @stage"),
    ("tsql", "SELECT 1\nGO\nSELECT 2\nGO"),
    ("bigquery", "SELECT STRUCT<a INT64>(1)"),
    ("postgres", "SELECT ROW(1,2), ARRAY[1,2]"),
]


def run_cases():
    fails = 0
    for dialect, sql in CASES:
        legacy = build(sql, dialect, native=False)
        native = build(sql, dialect, native=True)
        d = first_diff(legacy, native)
        if d:
            fails += 1
            print(f"[DIVERGENCE] {dialect} {sql[:50]!r}\n    {d}")
        else:
            print(f"[ok] {dialect} {sql[:50]!r}")
    return fails


def run_fixture_sample(n=150, seed=5):
    rng = random.Random(seed)
    files = sorted(FIXTURE_DIR.glob("*/*.sql"))
    sample = rng.sample(files, min(n, len(files)))
    fails = 0
    for i, f in enumerate(sample):
        sql = f.read_text(encoding="utf-8")
        legacy = build(sql, f.parent.name, native=False)
        native = build(sql, f.parent.name, native=True)
        d = first_diff(legacy, native)
        if d:
            fails += 1
            print(f"[DIVERGENCE] {f.relative_to(FIXTURE_DIR)}\n    {d}")
        if i % 25 == 0:
            print(f"...{i}/{len(sample)}", file=sys.stderr)
    print(f"fixture sample: {len(sample)} files, {fails} divergences")
    return fails


if __name__ == "__main__":
    fails = run_cases()
    if len(sys.argv) > 1 and sys.argv[1] == "fixtures":
        fails += run_fixture_sample()
    print(f"TOTAL: {fails}")
