# ruff: noqa: D101,D102,D103,E402
"""Adversarial fuzzer for native-AST vs legacy build-path parity.

Generates hostile inputs the fixture corpus never contains:
- mutated fixtures (truncate / drop / duplicate tokens, stray brackets/commas)
- cross-dialect parsing (fixture from dialect A parsed under dialect B)
- unicode / newline torture cases
Everything is compared strictly via diff_harness.compare.
"""

import random
import sys
import traceback
from pathlib import Path

from diff_harness import compare

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "test" / "fixtures" / "dialects"

STRAYS = [")", "(", "]", ",", ";", "END", "SELECT", "'", '"', "{-", "-}", "``"]


def mutations(sql: str, rng: random.Random):
    """Yield (label, mutated_sql) variants of one source file."""
    toks = sql.split(" ")
    # Truncate at a random point (mid-token truncation included).
    cut = rng.randrange(1, max(2, len(sql)))
    yield "truncate", sql[:cut]
    # Drop a random word.
    if len(toks) > 2:
        i = rng.randrange(len(toks))
        yield "dropword", " ".join(toks[:i] + toks[i + 1 :])
    # Duplicate a random word.
    if len(toks) > 1:
        i = rng.randrange(len(toks))
        yield "dupword", " ".join(toks[:i] + [toks[i]] + toks[i:])
    # Insert a stray token at a random word boundary.
    i = rng.randrange(len(toks) + 1)
    stray = rng.choice(STRAYS)
    yield f"stray({stray})", " ".join(toks[:i] + [stray] + toks[i:])
    # Swap two adjacent words.
    if len(toks) > 3:
        i = rng.randrange(len(toks) - 1)
        toks2 = list(toks)
        toks2[i], toks2[i + 1] = toks2[i + 1], toks2[i]
        yield "swap", " ".join(toks2)


def run_mutation_fuzz(per_dialect=8, per_file_muts=None, seed=1234):
    rng = random.Random(seed)
    failures = []
    n = 0
    dialects = sorted(d for d in FIXTURE_DIR.iterdir() if d.is_dir())
    for ddir in dialects:
        files = sorted(ddir.glob("*.sql"))
        if not files:
            continue
        sample = rng.sample(files, min(per_dialect, len(files)))
        for f in sample:
            sql = f.read_text(encoding="utf-8")
            if len(sql) > 4000:
                sql = sql[:4000]
            for label, mut in mutations(sql, rng):
                n += 1
                try:
                    diffs, legacy, native = compare(mut, ddir.name)
                except BaseException as e:
                    failures.append((ddir.name, f.name, label, f"HARNESS: {e!r}"))
                    traceback.print_exc()
                    continue
                if diffs:
                    failures.append((ddir.name, f.name, label, list(diffs)))
                    print(f"[DIVERGENCE] {ddir.name}/{f.name} {label}: {list(diffs)}")
                    for k, (lv, nv) in diffs.items():
                        print(f"    {k}: legacy={str(lv)[:200]}")
                        print(f"    {k}: native={str(nv)[:200]}")
        print(f"... {ddir.name} done ({n} cases so far)", file=sys.stderr)
    print(f"mutation fuzz: {n} cases, {len(failures)} divergences")
    return failures


def run_cross_dialect(n_pairs=120, seed=99):
    rng = random.Random(seed)
    dialects = sorted(d.name for d in FIXTURE_DIR.iterdir() if d.is_dir())
    failures = []
    for i in range(n_pairs):
        src = rng.choice(dialects)
        dst = rng.choice([d for d in dialects if d != src])
        files = sorted((FIXTURE_DIR / src).glob("*.sql"))
        if not files:
            continue
        f = rng.choice(files)
        sql = f.read_text(encoding="utf-8")[:3000]
        try:
            diffs, legacy, native = compare(sql, dst)
        except BaseException as e:
            failures.append((src, f.name, dst, f"HARNESS: {e!r}"))
            traceback.print_exc()
            continue
        if diffs:
            failures.append((src, f.name, dst, list(diffs)))
            print(f"[DIVERGENCE] {src}/{f.name} as {dst}: {list(diffs)}")
    print(f"cross-dialect: {n_pairs} cases, {len(failures)} divergences")
    return failures


TORTURE = [
    ("crlf", "ansi", "SELECT a,\r\n b\r\nFROM t\r\n"),
    ("cr_only", "ansi", "SELECT 1\rFROM t"),
    ("unicode_ident", "ansi", 'SELECT "héllo", "日本語" FROM t'),
    ("emoji_str", "ansi", "SELECT '🔥💥' FROM t"),
    ("nul_adjacent", "ansi", "SELECT 'a\tb' FROM t"),
    (
        "long_line",
        "ansi",
        "SELECT " + ", ".join(f"c{i}" for i in range(500)) + " FROM t",
    ),
    ("many_stmts", "ansi", "SELECT 1;" * 200),
    ("many_unions", "ansi", " UNION ALL ".join(["SELECT 1"] * 100)),
    ("deep_case", "ansi", "SELECT " + "CASE WHEN 1 THEN " * 40 + "0" + " END" * 40),
    ("unclosed_case", "ansi", "SELECT " + "CASE WHEN 1 THEN " * 40 + "0"),
    (
        "in_list_huge",
        "ansi",
        "SELECT 1 WHERE x IN (" + ",".join(map(str, range(500))) + ")",
    ),
    (
        "dangling_in_huge",
        "ansi",
        "SELECT 1 WHERE x IN (" + ",".join(map(str, range(100))) + ",",
    ),
    ("only_operators", "ansi", "+ - * / = < >"),
    ("only_brackets", "ansi", "()[]()[]"),
    ("bracket_soup", "ansi", "([)](])(["),
    ("semicolon_ws", "ansi", " ; ; \n ; "),
    ("comment_then_garbage", "ansi", "-- c\n%%%%"),
    ("tsql_go", "tsql", "SELECT 1\nGO\nSELECT 2\nGO"),
    ("tsql_go_bad", "tsql", "GO GO GO"),
    ("bq_struct", "bigquery", "SELECT STRUCT<a INT64>(1)"),
    ("bq_struct_bad", "bigquery", "SELECT STRUCT<a INT64(1)"),
    ("snow_stage", "snowflake", "LIST @my_stage"),
    ("snow_dollar", "snowflake", "SELECT $1, $2 FROM @stage"),
    ("pg_dollar_bad", "postgres", "SELECT $tag$unclosed"),
    ("mysql_delim", "mysql", "DELIMITER //\nSELECT 1//"),
    ("exasol_bad", "exasol", "SELECT 1 %%% FROM"),
    ("duckdb_lambda", "duckdb", "SELECT list_transform([1,2], x -> x + 1)"),
    ("athena_bad", "athena", "SELECT a FROM t WHERE"),
    ("sparksql_hint", "sparksql", "SELECT /*+ BROADCAST(t) */ a FROM t"),
]


def run_torture():
    failures = []
    for label, dialect, sql in TORTURE:
        try:
            diffs, legacy, native = compare(sql, dialect)
        except BaseException as e:
            failures.append((label, f"HARNESS: {e!r}"))
            traceback.print_exc()
            continue
        if diffs:
            failures.append((label, list(diffs)))
            print(f"[DIVERGENCE] {label} ({dialect}): {list(diffs)}")
            for k, (lv, nv) in diffs.items():
                print(f"    {k}: legacy={str(lv)[:200]}")
                print(f"    {k}: native={str(nv)[:200]}")
        else:
            print(f"[ok] {label}")
    print(f"torture: {len(TORTURE)} cases, {len(failures)} divergences")
    return failures


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    fails = []
    if which in ("all", "torture"):
        fails += run_torture()
    if which in ("all", "cross"):
        fails += run_cross_dialect()
    if which in ("all", "mutate"):
        fails += run_mutation_fuzz()
    print(f"TOTAL: {len(fails)} divergences")
    for f in fails:
        print("  ", f)
