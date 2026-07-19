# ruff: noqa: D101,D102,D103,E402
"""Round 2: heavier, nastier fuzzing for native-vs-legacy parity."""

import random
import string
import sys
import traceback
from pathlib import Path

from diff_harness import compare, diff_result, parse_templated

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "test" / "fixtures" / "dialects"


def char_mutations(sql, rng, n=4):
    for _ in range(n):
        kind = rng.choice(["ins", "del", "flip", "splice"])
        if not sql:
            return
        i = rng.randrange(len(sql))
        if kind == "ins":
            c = rng.choice("()[]{}<>,;'\"`.-|+*/\\\n\t " + string.ascii_letters)
            yield f"ins@{i}", sql[:i] + c + sql[i:]
        elif kind == "del":
            yield f"del@{i}", sql[:i] + sql[i + 1 :]
        elif kind == "flip":
            yield f"flip@{i}", sql[:i] + sql[i].swapcase() + sql[i + 1 :]
        else:
            j = rng.randrange(len(sql))
            a, b = min(i, j), max(i, j)
            yield f"splice@{a}:{b}", sql[:a] + sql[b:]


def bracket_soup(rng):
    parts = []
    vocab = [
        "(",
        ")",
        "[",
        "]",
        "SELECT",
        "1",
        "a",
        ",",
        "CASE",
        "WHEN",
        "END",
        "FROM",
        "t",
        "WHERE",
        "AND",
        "'x'",
        ";",
        "+",
        "=",
    ]
    for _ in range(rng.randrange(3, 40)):
        parts.append(rng.choice(vocab))
    return " ".join(parts)


def run_char_fuzz(per_dialect=15, seed=777):
    rng = random.Random(seed)
    fails = 0
    n = 0
    for ddir in sorted(d for d in FIXTURE_DIR.iterdir() if d.is_dir()):
        files = sorted(ddir.glob("*.sql"))
        if not files:
            continue
        for f in rng.sample(files, min(per_dialect, len(files))):
            sql = f.read_text(encoding="utf-8")[:2500]
            for label, mut in char_mutations(sql, rng):
                n += 1
                try:
                    diffs, *_ = compare(mut, ddir.name)
                except BaseException as e:
                    print(f"[HARNESS] {ddir.name}/{f.name} {label}: {e!r}")
                    fails += 1
                    continue
                if diffs:
                    fails += 1
                    print(f"[DIVERGENCE] {ddir.name}/{f.name} {label}: {list(diffs)}")
        print(f"... {ddir.name} ({n} so far)", file=sys.stderr)
    print(f"char fuzz: {n} cases, {fails} divergences")
    return fails


def run_soup(n_cases=400, seed=31337):
    rng = random.Random(seed)
    dialects = sorted(d.name for d in FIXTURE_DIR.iterdir() if d.is_dir())
    fails = 0
    for i in range(n_cases):
        sql = bracket_soup(rng)
        dialect = rng.choice(dialects)
        try:
            diffs, *_ = compare(sql, dialect)
        except BaseException as e:
            print(f"[HARNESS] soup {dialect} {sql[:60]!r}: {e!r}")
            fails += 1
            continue
        if diffs:
            fails += 1
            print(f"[DIVERGENCE] soup {dialect} {sql[:80]!r}: {list(diffs)}")
    print(f"soup fuzz: {n_cases} cases, {fails} divergences")
    return fails


JINJA_NASTY = [
    "SELECT a {% if x %} FROM t",  # unclosed block
    "{% for i in range(3) %}SELECT {{ i }};{% endfor %}",
    "{% for i in range(3) %}SELECT {{ i }};",  # unclosed for
    "SELECT {{ undefined_var }} FROM t",
    "{{ '{%' }} SELECT 1",
    "{% raw %}SELECT {{ not_a_var }}{% endraw %} FROM t",
    "SELECT {# comment #}{# another #}1",
    "{% set cols = ['a','b','c'] %}SELECT {{ cols | join(', ') }} FROM t",
    "SELECT * FROM {% if true %}t1{% else %}t2{% endif %} WHERE {% if false %}a{% else %}b{% endif %} = 1",
    "{% macro m(x) %}{{ x }} + 1{% endmacro %}SELECT {{ m(2) }}",
    "SELECT 1{% if true %}{% endif %}",  # empty block
    "{% if true %}{% endif %}",  # block only, empty render
    "SELECT '{{ \"quoted\" }}'",
    "SELECT {{ 1 + }} FROM t",  # jinja syntax error
]


def run_jinja():
    fails = 0
    for sql in JINJA_NASTY:
        try:
            legacy = parse_templated(sql, "ansi", native=False)
            native = parse_templated(sql, "ansi", native=True)
        except BaseException as e:
            print(f"[HARNESS] jinja {sql[:50]!r}: {e!r}")
            traceback.print_exc()
            fails += 1
            continue
        diffs = diff_result(legacy, native)
        if diffs:
            fails += 1
            print(f"[DIVERGENCE] jinja {sql[:60]!r}: {list(diffs)}")
            for k, (lv, nv) in diffs.items():
                print(f"    {k}: legacy={str(lv)[:200]}")
                print(f"    {k}: native={str(nv)[:200]}")
        else:
            print(f"[ok] jinja {sql[:50]!r}")
    print(f"jinja fuzz: {len(JINJA_NASTY)} cases, {fails} divergences")
    return fails


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    total = 0
    if which in ("all", "jinja"):
        total += run_jinja()
    if which in ("all", "soup"):
        total += run_soup()
    if which in ("all", "char"):
        total += run_char_fuzz()
    print(f"TOTAL divergences: {total}")
