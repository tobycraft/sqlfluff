#!/usr/bin/env python3
"""Package baseline results (pytest + cargo bench) into a JSON artifact.

Usage:
    BL_PASSED=N BL_FAILED=N BL_DURATION=Xs \\
    python3 .github/scripts/package_baseline.py \\
        --bench-txt <path>  \\
        --out       <path>
"""
import argparse
import json
import os
import re
from pathlib import Path

_SCALE = {"ns": 1, "µs": 1_000, "ms": 1_000_000, "s": 1_000_000_000}
_CRITERION = re.compile(
    r"^(\S[\w/]+)\s+time:\s+\[[\d.]+ \S+ ([\d.]+) ([a-zµ]+)", re.M
)


def parse_criterion(text: str) -> dict[str, int]:
    result = {}
    for m in _CRITERION.finditer(text):
        name, val, unit = m.group(1), float(m.group(2)), m.group(3)
        result[name] = round(val * _SCALE.get(unit, 1))
    return result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--bench-txt", required=True, help="Path to cargo bench stdout")
    p.add_argument("--out", required=True, help="Output JSON path")
    args = p.parse_args()

    bench = parse_criterion(Path(args.bench_txt).read_text())
    data = {
        "pytest": {
            "passed":   os.environ.get("BL_PASSED", "0"),
            "failed":   os.environ.get("BL_FAILED", "0"),
            "duration": os.environ.get("BL_DURATION", "n/a"),
        },
        "bench": bench,
    }
    out = Path(args.out)
    out.write_text(json.dumps(data, indent=2))
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
