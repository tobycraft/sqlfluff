# ruff: noqa: D101,D102,D103,E402
"""#1: RsMatchResult structural invariant validator + high-volume fuzz.

A malformed match result (out-of-bounds slice, overlapping or unsorted
children, zero-length node carrying a class/children) is a rust-core bug by
definition - no Python comparison needed, so this runs at rust-only speed.
"""

import random
import sys
from pathlib import Path

from sqlfluff.core import FluffConfig
from sqlfluff.core.parser import Lexer
from sqlfluff.core.parser.rust_parser import RustParser

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "test" / "fixtures" / "dialects"

# Requires the mutation helpers alongside this script.
import fuzz2_harness
import fuzz_harness


def validate(rs_match, n_tokens, path="root"):
    """Yield invariant violations in a raw RsMatchResult tree."""
    start, stop = rs_match.matched_slice
    if start > stop:
        yield (path, f"inverted slice ({start},{stop})")
    if stop > n_tokens:
        yield (path, f"slice out of bounds ({start},{stop}) n={n_tokens}")
    if start == stop:
        if rs_match.matched_class:
            yield (path, f"zero-length with class {rs_match.matched_class}")
        if rs_match.child_matches:
            yield (path, "zero-length with children")
    prev_end = start
    prev_start = None
    for i, child in enumerate(rs_match.child_matches):
        cs, ce = child.matched_slice
        if cs < start or ce > stop:
            yield (f"{path}[{i}]", f"child ({cs},{ce}) outside parent ({start},{stop})")
        if prev_start is not None and cs < prev_start:
            yield (f"{path}[{i}]", f"children unsorted ({cs} after {prev_start})")
        if cs < prev_end:
            yield (
                f"{path}[{i}]",
                f"overlap: child starts {cs} before prev end {prev_end}",
            )
        prev_end = max(prev_end, ce)
        prev_start = cs
        yield from validate(
            child, n_tokens, f"{path}>{child.matched_class or '?'}[{i}]"
        )
    for idx, seg_type, _impl in rs_match.insert_segments or []:
        if idx < start or idx > stop:
            yield (path, f"insert @{idx} outside ({start},{stop})")


_PARSERS = {}


def probe(sql, dialect):
    """Parse via the rust core only; return invariant violations."""
    if dialect not in _PARSERS:
        config = FluffConfig(overrides={"dialect": dialect})
        _PARSERS[dialect] = (config, RustParser(config=config))
    config, parser = _PARSERS[dialect]
    try:
        segments, _ = Lexer(config=config).lex(sql)
    except BaseException:
        return None
    s = 0
    for s in range(len(segments)):
        if segments[s].is_code:
            break
    e = len(segments)
    for e in range(len(segments), s - 1, -1):
        if segments[e - 1].is_code:
            break
    if s == e:
        return None
    try:
        tokens = parser._extract_tokens_from_segments(segments[s:e])
        rs = parser._rs_parser.parse_match_result_from_tokens(tokens)
    except BaseException:
        return None  # raising is fine; we only audit *returned* results
    return list(validate(rs, len(tokens)))


def run(volume_mult=5, seed_base=90000):
    findings = []
    n = 0
    dialects = sorted(d for d in FIXTURE_DIR.iterdir() if d.is_dir())
    for round_i in range(volume_mult):
        rng = random.Random(seed_base + round_i)
        for ddir in dialects:
            files = sorted(ddir.glob("*.sql"))
            if not files:
                continue
            for f in rng.sample(files, min(8, len(files))):
                sql = f.read_text(encoding="utf-8")[:4000]
                muts = list(fuzz_harness.mutations(sql, rng))
                muts += [
                    (lab, m) for lab, m in fuzz2_harness.char_mutations(sql, rng, n=3)
                ]
                for label, mut in muts:
                    n += 1
                    v = probe(mut, ddir.name)
                    if v:
                        findings.append((ddir.name, f.name, label, v[:3]))
                        print(f"[INVARIANT] {ddir.name}/{f.name} {label}: {v[:3]}")
        print(f"... round {round_i} done ({n} cases)", file=sys.stderr)
    print(f"invariant fuzz: {n} cases, {len(findings)} violations")
    return findings


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
