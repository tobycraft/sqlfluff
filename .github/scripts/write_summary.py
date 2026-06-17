#!/usr/bin/env python3
r"""Generate the GitHub Actions Job Summary from benchmark artifacts.

The first entry in COMMITS is always the merge-base commit, which acts as the
comparison baseline for all subsequent commits.

Usage:
    REPOSITORY=... BRANCH=... TOTAL=N MERGE_BASE=abc1234 \\
    COMMITS='["sha0","sha1","sha2"]' GITHUB_STEP_SUMMARY=<path> \\
    python3 .github/scripts/write_summary.py --artifacts <dir>
"""

import argparse
import json
import os
from pathlib import Path


def _fmt_delta(pct: float) -> str:
    icon = "🔺" if pct > 3 else ("🟢" if pct < -3 else "➖")
    sign = "+" if pct >= 0 else ""
    return f"{icon} ({sign}{pct:.1f}%)"


def main() -> None:
    """Read per-commit artifacts and write the GitHub Actions Job Summary."""
    p = argparse.ArgumentParser()
    p.add_argument(
        "--artifacts", required=True, help="Directory of downloaded artifacts"
    )
    args = p.parse_args()

    e = os.environ
    repo = e.get("REPOSITORY", "?")
    branch = e.get("BRANCH", "?")
    total = e.get("TOTAL", "?")
    merge_base = e.get("MERGE_BASE", "?")
    commits = json.loads(e.get("COMMITS", "[]"))
    artifacts = Path(args.artifacts)

    # Load all per-commit results and restore commit order.
    result_map: dict[str, dict] = {}
    for d in artifacts.iterdir():
        f = d / "result.json"
        if f.exists():
            r = json.loads(f.read_text())
            result_map[r["sha"]] = r
    results = [result_map[sha] for sha in commits if sha in result_map]

    # The merge-base (first commit) is the baseline for comparisons.
    baseline = results[0] if results else {}
    bl_pt = baseline.get("pytest", {})
    bl_bench = baseline.get("bench", {})
    bl_short = baseline.get("short", merge_base)

    L: list[str] = []

    def a(s: str = "") -> None:
        L.append(s)

    a("## Commit-Range Benchmark Report\n")
    a("| | |")
    a("|---|---|")
    a(f"| **Repository** | `{repo}` |")
    a(f"| **Branch** | `{branch}` |")
    a(f"| **Merge-base** | `{merge_base}` |")
    a(f"| **Commits benchmarked** | {len(results)} / {total} |")
    a()

    # ── Baseline (merge-base) ──────────────────────────────────────────────────
    a(f"### Baseline — merge-base `{bl_short}`\n")
    a("#### pytest `parse_suite`")
    a(
        f"Passed: **{bl_pt.get('passed', '?')}** &nbsp; "
        f"Failed: **{bl_pt.get('failed', '?')}** &nbsp; "
        f"Duration: {bl_pt.get('duration', '?')}"
    )
    if bl_bench:
        a()
        a("#### cargo bench")
        a("| Benchmark | Estimate (ns) |")
        a("|-----------|-------------:|")
        for name, ns in sorted(bl_bench.items()):
            a(f"| `{name}` | {ns:,} |")
    a()

    # ── Per-commit results ─────────────────────────────────────────────────────
    branch_results = results[1:]  # exclude baseline row from comparison tables
    if not branch_results:
        a("> No branch commits were benchmarked.")
    else:
        a("### pytest `parse_suite` — per commit\n")
        a("| Commit | Subject | ✅ Passed | ❌ Failed | Duration |")
        a("|--------|---------|----------:|----------:|----------|")
        for r in branch_results:
            pt = r.get("pytest", {})
            a(
                f"| `{r['short']}` | {r.get('subject', '')[:60]} "
                f"| {pt.get('passed', '—')} | {pt.get('failed', '—')} "
                f"| {pt.get('duration', '—')} |"
            )

        bench_rows = [r for r in branch_results if r.get("bench")]
        if bench_rows:
            bench_names = sorted({k for r in bench_rows for k in r["bench"]})
            a()
            a(f"### cargo bench — per commit vs merge-base `{bl_short}` (ns)\n")
            a(
                "| Commit | Subject | "
                + " | ".join(f"`{n}`" for n in bench_names)
                + " |"
            )
            a("|--------|---------|" + "|".join("---:" for _ in bench_names) + "|")
            for r in bench_rows:
                cells = []
                for n in bench_names:
                    ns = r["bench"].get(n)
                    if ns is None:
                        cells.append("—")
                        continue
                    bns = bl_bench.get(n)
                    if bns:
                        pct = (ns - bns) / bns * 100
                        cells.append(f"{ns:,} {_fmt_delta(pct)}")
                    else:
                        cells.append(f"{ns:,}")
                a(
                    f"| `{r['short']}` | {r.get('subject', '')[:40]} | "
                    + " | ".join(cells)
                    + " |"
                )

    summary_path = e.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        Path(summary_path).write_text("\n".join(L) + "\n")
        print("Summary written.")
    else:
        print("\n".join(L))


if __name__ == "__main__":
    main()
