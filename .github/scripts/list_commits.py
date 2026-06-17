#!/usr/bin/env python3
"""Resolve a commit range and write results to GITHUB_OUTPUT.

Usage:
    python3 .github/scripts/list_commits.py \
        --repo <path>     \\
        --base-ref <ref>  \\
        --max <n>
"""
import argparse
import json
import os
import subprocess
import sys


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True, help="Path to git repo")
    p.add_argument("--base-ref", required=True, help="Branch to find merge-base against")
    p.add_argument("--max", type=int, default=10, help="Maximum commits to include")
    args = p.parse_args()

    repo = args.repo

    # Ensure the base ref is available locally.
    _run(["git", "-C", repo, "fetch", "origin", args.base_ref, "--depth=200"])

    # Resolve merge-base with progressive fallbacks.
    merge_base = None
    for cmd in (
        ["git", "-C", repo, "merge-base", "HEAD", f"origin/{args.base_ref}"],
        ["git", "-C", repo, "rev-parse", f"HEAD~{args.max}"],
        ["git", "-C", repo, "rev-list", "HEAD", "--max-count=1"],
    ):
        r = _run(cmd)
        if r.returncode == 0 and r.stdout.strip():
            merge_base = r.stdout.strip()
            break

    if not merge_base:
        sys.exit("error: could not resolve merge-base")

    # Collect commits oldest → newest.
    r = _run(["git", "-C", repo, "log", "--format=%H", f"{merge_base}..HEAD"])
    shas = r.stdout.split()[: args.max]
    shas.reverse()

    commits_json = json.dumps(shas)
    short_base = merge_base[:7]

    print(f"merge_base : {short_base}")
    print(f"total      : {len(shas)}")
    print(f"commits    : {commits_json}")

    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"commits={commits_json}\n")
            f.write(f"total={len(shas)}\n")
            f.write(f"merge_base={short_base}\n")


if __name__ == "__main__":
    main()
