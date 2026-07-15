"""Commit .github/codspeed-swept.json if the finalize step updated it.

Used by the `finalize` job in .github/workflows/codspeed-sweep.yml, after
codspeed_sweep_finalize.py has merged newly-swept commits into the file.
No-ops cleanly if nothing changed.

CI-triggered sweeps can overlap (one per green CI run), so two finalize
jobs can race to push to main. On a rejected push this re-merges our
entries into the moved branch and retries, instead of failing and losing
the record (which would make the next sweep of the same commit hit
CodSpeed's duplicate-SHA error).
"""

from __future__ import annotations

import json
import os
import subprocess

SWEPT_PATH = ".github/codspeed-swept.json"
PUSH_ATTEMPTS = 4


def _read_remote_swept() -> set:
    shown = subprocess.run(
        ["git", "show", f"origin/main:{SWEPT_PATH}"],
        capture_output=True,
        text=True,
    )
    return set(json.loads(shown.stdout)) if shown.returncode == 0 else set()


def _commit(message: str) -> None:
    subprocess.run(["git", "add", SWEPT_PATH], check=True)
    subprocess.run(["git", "commit", "-m", message], check=True)


def main() -> None:
    """Commit and push the tracking file if it changed, otherwise no-op."""
    unchanged = (
        subprocess.run(["git", "diff", "--quiet", "--", SWEPT_PATH]).returncode == 0
    )
    if unchanged:
        print("No new commits to record.")
        return

    run_id = os.environ["GITHUB_RUN_ID"]
    message = f"chore: record CodSpeed-swept commits from run {run_id}"
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    subprocess.run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        check=True,
    )

    ours = set(json.load(open(SWEPT_PATH)))
    _commit(message)

    for _ in range(PUSH_ATTEMPTS):
        if subprocess.run(["git", "push"]).returncode == 0:
            return
        # main moved under us (most likely another sweep's finalize).
        # Rebuild on top of the new tip, merging our entries with theirs.
        subprocess.run(["git", "fetch", "origin", "main"], check=True)
        merged = sorted(ours | _read_remote_swept())
        subprocess.run(["git", "reset", "--hard", "origin/main"], check=True)
        with open(SWEPT_PATH, "w") as f:
            json.dump(merged, f, indent=2)
            f.write("\n")
        if subprocess.run(["git", "diff", "--quiet", "--", SWEPT_PATH]).returncode == 0:
            print("Remote already has all our entries.")
            return
        _commit(message)

    raise SystemExit(f"Could not push {SWEPT_PATH} after {PUSH_ATTEMPTS} attempts.")


if __name__ == "__main__":
    main()
