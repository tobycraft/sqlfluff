# ruff: noqa: D101,D102,D103,E402
"""#2: differential of RegexParser accept-decisions: Python vs Rust semantics.

Python (parsers.py): `regex` module; template compiled with IGNORECASE when
ignore_case; matched against raw_upper (str.upper()!) when ignore_case, with
a group(0)==raw full-match check; anti_template is PREFIX-matched (.match with
no full check).

Rust (core.rs/RegexMode): regex/fancy-regex crates; `^(?:pat)$` fullmatch on
the ORIGINAL raw with case_insensitive; the anti is ALSO a fullmatch.

The rig extracts every RegexParser from every expanded dialect, gathers real
token raws per dialect from the fixture corpus (plus case/unicode variants),
and compares the two accept decisions case by case.
"""

import json
import subprocess
import sys
from pathlib import Path

import regex as regex_mod

from sqlfluff.core import FluffConfig
from sqlfluff.core.dialects import dialect_readout, dialect_selector
from sqlfluff.core.parser import Lexer
from sqlfluff.core.parser.parsers import RegexParser

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "test" / "fixtures" / "dialects"
RXDIFF = Path(__file__).parent / "rxdiff/target/release/rxdiff"

UNICODE_PROBES = [
    "straße",
    "STRASSE",
    "ẞIG",
    "İSTANBUL",
    "istanbul",
    "ıd",
    "ﬁeld",
    "KELVIN",
    "ABC",
    "abc",
    "A1_$",
    "N'x'",
    "ﬀ",
]


def collect_parsers():
    out = {}
    for readout in dialect_readout():
        d = dialect_selector(readout.label)
        for name, g in d._library.items():
            if g.__class__ is RegexParser:
                out[(readout.label, name)] = (
                    g.template,
                    g.anti_template,
                    g.ignore_case,
                )
    return out


def collect_raws(dialect, limit_files=25):
    raws = set()
    config = FluffConfig(overrides={"dialect": dialect})
    lexer = Lexer(config=config)
    for f in sorted((FIXTURE_DIR / dialect).glob("*.sql"))[:limit_files]:
        try:
            segs, _ = lexer.lex(f.read_text(encoding="utf-8"))
        except BaseException:
            continue
        for s in segs:
            if s.is_code and s.raw and len(s.raw) < 60:
                raws.add(s.raw)
    return raws


def py_accept(template, anti, ignore_case, raw):
    flags = regex_mod.IGNORECASE if ignore_case else 0
    _raw = raw.upper() if ignore_case else raw
    m = regex_mod.match(template, _raw, flags)
    if not m or m.group(0) != _raw:
        return False
    if anti and regex_mod.match(anti, _raw, flags):
        return False
    return True


def main():
    parsers = collect_parsers()
    print(f"{len(parsers)} RegexParsers across dialects", file=sys.stderr)
    raw_cache = {}
    cases = []  # (dialect, name, template, anti, ci, raw)
    lines = []
    for (dialect, name), (template, anti, ci) in sorted(parsers.items()):
        if dialect not in raw_cache:
            raw_cache[dialect] = collect_raws(dialect)
            print(f"  raws[{dialect}]: {len(raw_cache[dialect])}", file=sys.stderr)
        texts = set()
        for r in raw_cache[dialect]:
            texts.add(r)
            texts.add(r.upper())
            texts.add(r.lower())
            texts.add(r.swapcase())
        texts.update(UNICODE_PROBES)
        for raw in sorted(texts):
            cases.append((dialect, name, template, anti, ci, raw))
            lines.append(
                json.dumps({"template": template, "anti": anti, "ci": ci, "raw": raw})
            )
    print(f"{len(cases)} decision cases", file=sys.stderr)
    proc = subprocess.run(
        [str(RXDIFF)],
        input="\n".join(lines).encode(),
        capture_output=True,
        check=True,
    )
    rust_out = proc.stdout.decode().split()
    assert len(rust_out) == len(cases), (len(rust_out), len(cases))

    diverging = {}
    for (dialect, name, template, anti, ci, raw), r in zip(cases, rust_out):
        rs = r == "1"
        py = py_accept(template, anti, ci, raw)
        if py != rs:
            key = (dialect, name)
            diverging.setdefault(key, []).append((raw, py, rs))

    for (dialect, name), hits in sorted(diverging.items()):
        template, anti, ci = parsers[(dialect, name)]
        print(f"[DIVERGE] {dialect}.{name} ci={ci}")
        print(f"    template={template!r}")
        print(f"    anti={anti!r}")
        for raw, py, rs in hits[:5]:
            print(f"    raw={raw!r}: py={py} rs={rs}")
        if len(hits) > 5:
            print(f"    ... and {len(hits) - 5} more")
    print(f"TOTAL diverging parsers: {len(diverging)} / {len(parsers)}")


if __name__ == "__main__":
    main()
