"""Check grammar-generated SQL against a real database engine's own parser.

Consumes SQL from ``generate_dialect_sql.py``'s generator, filters it through
sqlfluff's own parser (``self_check``), then checks what survives against a
real engine's parser. Three engines are wired up so far, all embedded (no
server, network, or schema needed):

- Postgres, via ``pglast`` (bundles ``libpg_query``) - a pure parser, so
  ``pglast.Error`` alone is a reliable syntax-only signal.
- DuckDB, via the ``duckdb`` package. DuckDB has no equivalent pure-parse API
  for non-SELECT statements (``json_serialize_sql`` looks like one but only
  supports ``SELECT`` - ``CREATE``/``INSERT``/etc. return
  ``"Only SELECT statements can be serialized to json!"`` even when the SQL is
  fine), so this checker executes against a fresh in-memory connection instead
  and catches ``duckdb.ParserException`` specifically - not the broader
  ``duckdb.Error``, since executing (rather than just parsing) surfaces many
  non-syntax exception types (``CatalogException`` for a missing table,
  ``BinderException``, ``ConstraintException``, ...) that aren't a syntax
  question at all and must not be treated as one.
- SparkSQL, via ``pyspark`` (local, in-process Spark - ``local[1]`` master, no
  cluster). ``pyspark.errors.ParseException`` is a *subclass* of
  ``pyspark.errors.AnalysisException`` (the semantic-error type), so it must be
  caught first, same shape as the DuckDB exception filtering. The real cost
  here isn't execution semantics, it's that starting a ``SparkSession`` takes
  ~7 seconds - far more than pglast (instant) or duckdb (~13ms/connection) - so
  unlike the other two checkers, this one reuses a single lazily-created,
  module-level session across every call instead of a fresh one per call. That
  makes catalog-state accumulation across calls possible (e.g. an earlier
  example's ``CREATE TABLE foo`` persisting), but it's a non-issue for the same
  reason it was for DuckDB: only ``ParseException`` counts as a divergence, and
  state accumulation only changes *semantic* outcomes, which are ignored.
- ClickHouse, via ``chdb`` (embedded ClickHouse - no server). ``chdb.query()``
  is already stateless per call (a ``CREATE TABLE`` in one call does not
  persist to the next), so there's no fresh-connection or session-reuse
  concern here at all. But unlike the other three, ``chdb`` doesn't expose
  typed exceptions - every error is a plain ``RuntimeError``, so this checker
  filters by *message content* instead of exception type: ClickHouse's error
  messages reliably end with a symbolic error name in parentheses (confirmed:
  ``(SYNTAX_ERROR)`` for genuine syntax errors, ``(UNKNOWN_TABLE)``,
  ``(UNKNOWN_STORAGE)``, etc. for semantic ones), so only messages containing
  ``"(SYNTAX_ERROR)"`` are treated as a divergence.

A pass here means "the pinned engine version this checker uses accepts this,"
not "every version of that engine sqlfluff's dialect targets accepts this" -
each engine pins to one fixed version (printed at the start of every run).

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

try:
    import duckdb
except ImportError:
    duckdb = None

try:
    import pyspark
    from pyspark.errors import ParseException, PySparkException
    from pyspark.sql import SparkSession
except ImportError:
    pyspark = None
    ParseException = PySparkException = SparkSession = None

try:
    import chdb
except ImportError:
    chdb = None

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


def check_duckdb(sql: str) -> Optional[str]:
    """Return an error message if DuckDB's real parser rejects `sql`.

    Returns None if it accepts it (including if it fails for a non-syntax
    reason - DuckDB has no pure-parse API for non-SELECT statements, so this
    executes against a scratch in-memory database and only treats a
    ParserException as a divergence).
    """
    if duckdb is None:
        raise RuntimeError(
            "duckdb is not installed. Install it with `pip install duckdb` "
            "(see requirements_dev.txt)."
        )
    con = duckdb.connect(":memory:")
    try:
        con.execute(sql)
    except duckdb.ParserException as err:
        return str(err)
    except duckdb.Error:
        return None  # Some other (non-syntax) error - not this tool's concern.
    finally:
        con.close()
    return None


_spark_session: Optional["SparkSession"] = None


def _get_spark_session() -> "SparkSession":
    """Return a lazily-created, process-wide local SparkSession.

    Session startup takes ~7s, versus near-instant for pglast/duckdb, so
    (unlike those two) this is created once and reused for every call rather
    than fresh per call.
    """
    global _spark_session
    if _spark_session is None:
        _spark_session = (
            SparkSession.builder.master("local[1]")
            .appName("realengine_check")
            .getOrCreate()
        )
        _spark_session.sparkContext.setLogLevel("OFF")
    return _spark_session


def check_sparksql(sql: str) -> Optional[str]:
    """Return an error message if Spark's real parser rejects `sql`.

    Returns None if it accepts it (including if it fails for a non-syntax
    reason - only a ParseException counts as a divergence; ParseException is
    a subclass of AnalysisException, so it must be caught first).

    Unlike Postgres/DuckDB, Spark's own exception hierarchy is not an
    exhaustive catch-all for "anything that isn't a syntax error": some
    inputs trip Spark's *own* internal bugs (observed: `CREATE STREAMING
    TABLE foo` raises a raw `py4j.protocol.Py4JJavaError` wrapping a Scala
    `AssertionError`, entirely outside the `pyspark.errors` hierarchy). None
    of that is evidence about sqlfluff's grammar, so the fallback catches
    Exception broadly rather than just PySparkException.
    """
    if pyspark is None:
        raise RuntimeError(
            "pyspark is not installed. Install it with `pip install pyspark` "
            "(see requirements_dev.txt). It also requires a local Java runtime."
        )
    spark = _get_spark_session()
    try:
        spark.sql(sql)
    except ParseException as err:
        return str(err)
    except Exception:
        return None  # Some other (non-syntax) error - not this tool's concern.
    return None


def check_clickhouse(sql: str) -> Optional[str]:
    """Return an error message if ClickHouse's real parser rejects `sql`.

    Returns None if it accepts it (including if it fails for a non-syntax
    reason). Unlike the other checkers, chdb doesn't expose typed exceptions -
    every error is a plain RuntimeError - so this filters by message content:
    only a message ending in "(SYNTAX_ERROR)" counts as a divergence, not
    other ClickHouse error kinds like "(UNKNOWN_TABLE)"/"(UNKNOWN_STORAGE)".
    """
    if chdb is None:
        raise RuntimeError(
            "chdb is not installed. Install it with `pip install chdb` "
            "(see requirements_dev.txt)."
        )
    try:
        chdb.query(sql, "CSV")
    except RuntimeError as err:
        message = str(err)
        if "(SYNTAX_ERROR)" in message:
            return message
        return None  # Some other (non-syntax) error - not this tool's concern.
    return None


CHECKERS = {
    "postgres": check_postgres,
    "duckdb": check_duckdb,
    "sparksql": check_sparksql,
    "clickhouse": check_clickhouse,
}


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
    coverage: int = 0,
) -> bool:
    """Run the check. Returns True if there are no unresolved divergences."""
    checker = CHECKERS[dialect]
    examples = gds.generate(dialect, segment, max_depth, max_examples, coverage)
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
    parser.add_argument(
        "--coverage",
        type=gds._coverage_arg,
        default=0,
        help="0-100, forwarded to generate_dialect_sql.py - see its --help.",
    )
    parser.add_argument("--skiplist", type=Path, default=DEFAULT_SKIPLIST)
    args = parser.parse_args(argv)

    if args.dialect == "postgres" and pglast is not None:
        pg_version = ".".join(str(part) for part in pglast.get_postgresql_version())
        print(
            f"[realengine_check] checking against pglast {pglast.__version__} "
            f"(bundles PostgreSQL {pg_version} grammar - a pass here does not "
            "cover every Postgres version)",
            file=sys.stderr,
        )
    elif args.dialect == "duckdb" and duckdb is not None:
        print(
            f"[realengine_check] checking against duckdb {duckdb.__version__} "
            "(one pinned DuckDB version - a pass here does not cover every "
            "DuckDB version)",
            file=sys.stderr,
        )
    elif args.dialect == "sparksql" and pyspark is not None:
        print(
            f"[realengine_check] checking against pyspark {pyspark.__version__} "
            "(one pinned Spark version - a pass here does not cover every "
            "Spark version; starting the local Spark session takes ~7s, paid "
            "once for this whole run)",
            file=sys.stderr,
        )
    elif args.dialect == "clickhouse" and chdb is not None:
        print(
            f"[realengine_check] checking against chdb {chdb.__version__} "
            f"(bundles ClickHouse {chdb.engine_version} - a pass here does not "
            "cover every ClickHouse version)",
            file=sys.stderr,
        )

    try:
        skiplist = load_skiplist(args.skiplist)
        ok = run(
            args.dialect,
            args.segment,
            args.max_depth,
            args.max_examples,
            skiplist,
            args.coverage,
        )
    except (gds.GenerationError, RuntimeError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
