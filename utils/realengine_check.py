"""Check grammar-generated SQL against a real database engine's own parser.

Consumes SQL from ``generate_dialect_sql.py``'s generator, filters it through
sqlfluff's own parser (``self_check``), then checks what survives against a
real engine's parser. Postgres is wired up via ``pglast`` (bundles
``libpg_query``; no server, network, or schema needed) - it is the only
engine covered so far.

A pass here means "the Postgres version pglast bundles accepts this," not
"every Postgres version sqlfluff's postgres dialect targets accepts this" -
pglast pins to one fixed libpg_query version (printed at the start of every
run).

Findings are never dropped silently: SQL that sqlfluff's grammar accepts but
the real engine rejects (a "divergence") is always printed - either as a
fresh ``[DIVERGENCE]`` that fails the run, or, if the exact SQL text is
listed in the skiplist with a reason, as a ``[skipped]`` note that stays
visible but doesn't fail the run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_dialect_sql as gds  # noqa: E402

try:
    import pglast
except ImportError:
    pglast = None

DEFAULT_SKIPLIST = Path(__file__).resolve().parent / "realengine_skiplist.json"


def check_postgres(sql: str) -> Optional[str]:
    """Return an error message if Postgres's real parser rejects `sql`.

    Returns None if it accepts it.
    """
    if pglast is None:
        raise RuntimeError(
            "pglast is not installed. Install it with `pip install pglast` "
            "(see requirements_dev.txt)."
        )
    try:
        pglast.parse_sql(sql)
    except pglast.Error as err:
        return str(err)
    return None


CHECKERS = {"postgres": check_postgres}


def load_skiplist(path: Optional[Path]) -> dict[tuple[str, str], str]:
    """Load {(dialect, sql): reason} from a skiplist JSON file."""
    if path is None or not path.exists():
        return {}
    entries = json.loads(path.read_text())
    return {(entry["dialect"], entry["sql"]): entry["reason"] for entry in entries}


def run(
    dialect: str,
    segment: str,
    max_depth: int,
    max_examples: int,
    skiplist: dict[tuple[str, str], str],
) -> bool:
    """Run the check. Returns True if there are no unresolved divergences."""
    checker = CHECKERS[dialect]
    examples = gds.generate(dialect, segment, max_depth, max_examples)
    ok = True
    for sql in examples:
        if not gds.self_check(dialect, sql):
            # Not one of sqlfluff's own grammar claims to defend - skip.
            continue
        error = checker(sql)
        if error is None:
            continue  # Agreement - the expected, silent case.

        reason = skiplist.get((dialect, sql))
        if reason is not None:
            print(f"[skipped] {sql!r}: {reason}")
            continue

        print(f"[DIVERGENCE] sqlfluff accepts, {dialect} real parser rejects: {sql!r}")
        print(f"  {error}")
        ok = False
    return ok


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialect", required=True, choices=sorted(CHECKERS))
    parser.add_argument("--segment", required=True)
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--max-examples", type=int, default=50)
    parser.add_argument("--skiplist", type=Path, default=DEFAULT_SKIPLIST)
    args = parser.parse_args(argv)

    if pglast is not None:
        pg_version = ".".join(str(part) for part in pglast.get_postgresql_version())
        print(
            f"[realengine_check] checking against pglast {pglast.__version__} "
            f"(bundles PostgreSQL {pg_version} grammar - a pass here does not "
            "cover every Postgres version)",
            file=sys.stderr,
        )

    try:
        skiplist = load_skiplist(args.skiplist)
        ok = run(
            args.dialect, args.segment, args.max_depth, args.max_examples, skiplist
        )
    except (gds.GenerationError, RuntimeError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
