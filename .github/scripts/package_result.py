#!/usr/bin/env python3
r"""Package per-commit benchmark results into a JSON artifact.

Usage:
    SHA=<sha> PASSED=N FAILED=N DURATION=Xs \\
    python3 .github/scripts/package_result.py \\
        --bench-txt <path>  \\
        --repo      <path>  \\
        --out       <path>
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

_SCALE = {"ns": 1, "µs": 1_000, "ms": 1_000_000, "s": 1_000_000_000}
_CRITERION = re.compile(r"^(\S[\w/]+)\s+time:\s+\[[\d.]+ \S+ ([\d.]+) ([a-zµ]+)", re.M)


def parse_criterion(text: str) -> dict[str, int]:
    """Parse criterion benchmark output and return name→nanoseconds mapping."""
    result = {}
    for m in _CRITERION.finditer(text):
        name, val, unit = m.group(1), float(m.group(2)), m.group(3)
        result[name] = round(val * _SCALE.get(unit, 1))
    return result


def main() -> None:
    """Package benchmark + pytest results for one commit into a JSON artifact."""
    p = argparse.ArgumentParser()
    p.add_argument("--bench-txt", required=True, help="Path to cargo bench stdout")
    p.add_argument(
        "--repo", required=True, help="Path to git repo for commit subject lookup"
    )
    p.add_argument("--out", required=True, help="Output JSON path")
    args = p.parse_args()

    sha = os.environ["SHA"]
    bench = parse_criterion(Path(args.bench_txt).read_text())
    subject = subprocess.check_output(
        ["git", "-C", args.repo, "log", "-1", "--format=%s"], text=True
    ).strip()

    data = {
        "sha": sha,
        "short": sha[:7],
        "subject": subject,
        "pytest": {
            "passed": int(os.environ.get("PASSED") or 0),
            "failed": int(os.environ.get("FAILED") or 0),
            "duration": os.environ.get("DURATION", "n/a"),
        },
        "bench": bench,
    }
    out = Path(args.out)
    out.write_text(json.dumps(data, indent=2))
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
