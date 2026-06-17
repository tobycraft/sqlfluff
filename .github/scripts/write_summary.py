#!/usr/bin/env python3
"""Generate the GitHub Actions Job Summary from benchmark artifacts.

Usage:
    REPOSITORY=... BRANCH=... TOTAL=N MERGE_BASE=abc1234 \\
    COMMITS='["sha1","sha2"]' GITHUB_STEP_SUMMARY=<path> \\
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
    p = argparse.ArgumentParser()
    p.add_argument("--artifacts", required=True, help="Directory of downloaded artifacts")
    args = p.parse_args()

    e          = os.environ
    repo       = e.get("REPOSITORY", "?")
    branch     = e.get("BRANCH", "?")
    total      = e.get("TOTAL", "?")
    merge_base = e.get("MERGE_BASE", "?")
    commits    = json.loads(e.get("COMMITS", "[]"))
    artifacts  = Path(args.artifacts)

    # Load baseline artifact.
    bl_path  = artifacts / "baseline" / "baseline.json"
    baseline = json.loads(bl_path.read_text()) if bl_path.exists() else {}
    bl_pt    = baseline.get("pytest", {})
    bl_bench = baseline.get("bench", {})

    # Load per-commit results and restore commit order.
    result_map: dict[str, dict] = {}
    for d in artifacts.iterdir():
        f = d / "result.json"
        if f.exists():
            r = json.loads(f.read_text())
            result_map[r["sha"]] = r
    results = [result_map[sha] for sha in commits if sha in result_map]

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

    # ── Baseline ──────────────────────────────────────────────────────────────
    a("### Baseline — `rubytobi/sqlfluff@main`\n")
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
    if not results:
        a("> No results collected.")
    else:
        a("### pytest `parse_suite` — per commit\n")
        a("| Commit | Subject | ✅ Passed | ❌ Failed | Duration |")
        a("|--------|---------|----------:|----------:|----------|")
        for r in results:
            pt = r.get("pytest", {})
            a(
                f"| `{r['short']}` | {r.get('subject', '')[:60]} "
                f"| {pt.get('passed', '—')} | {pt.get('failed', '—')} "
                f"| {pt.get('duration', '—')} |"
            )

        bench_rows = [r for r in results if r.get("bench")]
        if bench_rows:
            bench_names = sorted({k for r in bench_rows for k in r["bench"]})
            a()
            a("### cargo bench — per commit (ns vs baseline)\n")
            a("| Commit | Subject | " + " | ".join(f"`{n}`" for n in bench_names) + " |")
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
