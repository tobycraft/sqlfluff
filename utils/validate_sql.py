# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb", "pglast"]
# ///
"""Validate SQL against a real database engine's parser.

sqlfluff's dialect fixtures are mostly provided through contributions.
Often, maintainers don't have access to all database engines and can't
validate the correctness themselves. A fixture can end up asserting
that sqlfluff parses some SQL as valid when the real engine would not.
This script lets a maintainer check a given SQL snippet against the real
engines for syntactical correctness, with minimal setup.

Run it with `uv run`, which installs the engines' parsers from the inline
script metadata above. sqlfluff itself is deliberately not pinned there:
bring the version whose grammar you want to check with `--with` or
`--with-editable`. Pass the SQL as a file (or `-` for stdin) and name the
dialects to check, e.g.:

    # Against a local checkout (e.g. a contributor's PR branch)
    uv run --with-editable . utils/validate_sql.py --dialects postgres --sql query.sql

    # Against a released version
    echo "SELECT 1;" | uv run --with sqlfluff==4.0.0 utils/validate_sql.py \
        --dialects duckdb sqlite --sql -

It prints a Markdown report of any statement a real engine's own parser
rejects, even though sqlfluff's grammar accepts it. Statements sqlfluff
can't parse itself are skipped, with a warning on stderr.

Limitations: currently only duckdb, postgres, and sqlite are supported,
this approach only catches syntax errors and not semantic ones (missing
tables/columns, etc.).
"""

import argparse
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

try:
    from sqlfluff.core import FluffConfig, Linter
    from sqlfluff.core.linter import RenderedFile
    from sqlfluff.core.templaters import TemplatedFile
except ImportError:
    sys.exit(
        "sqlfluff is not installed. Bring the version to check, e.g. "
        "`uv run --with-editable . utils/validate_sql.py ...` or "
        "`uv run --with sqlfluff==<version> utils/validate_sql.py ...`."
    )

try:
    import duckdb
except ImportError:
    duckdb = None

try:
    import pglast
except ImportError:
    pglast = None

# Substrings of SQLite's parser error messages. Semantic errors (e.g. "no such
# table") contain none of these, so they're still ignored.
SQLITE_SYNTAX_MARKERS = ("syntax error", "incomplete input", "unrecognized token")


def _require(module, package: str, *, note: str = "") -> None:
    """Raise RuntimeError with a standard install hint if `module` is None."""
    if module is not None:
        return
    raise RuntimeError(
        f"{package} is not installed{note}. Run this script with "
        f"`uv run utils/validate_sql.py ...`."
    )


@contextmanager
def _scratch_cwd() -> Iterator[None]:
    """Temporarily chdir into a fresh scratch directory, then restore cwd.

    Some statements have real filesystem side effects when executed,
    so a checker that executes SQL runs from a throwaway temp directory
    rather than the caller's working directory.
    """
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as scratch_dir:
        os.chdir(scratch_dir)
        try:
            yield
        finally:
            os.chdir(cwd)


def check_duckdb(sql: str) -> Optional[str]:
    """Return an error message if DuckDB's real parser rejects `sql`.

    Returns None if it accepts it (including if it fails for a non-syntax
    reason - DuckDB has no pure-parse API for non-SELECT statements, so this
    executes against a scratch in-memory database and only treats a
    ParserException as a divergence).

    External access and extension auto-install/load are disabled, so
    statements like `INSTALL` or `COPY ... TO '/abs/path'` raise a
    PermissionException (ignored as non-syntax) rather than downloading
    extensions or touching the filesystem outside the scratch directory.
    """
    _require(duckdb, "duckdb")
    result: Optional[str] = None
    with _scratch_cwd():
        con = duckdb.connect(
            ":memory:",
            config={
                "enable_external_access": False,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
            },
        )
        try:
            con.execute(sql)
        except duckdb.ParserException as err:
            result = str(err)
        except duckdb.Error:
            pass  # Non-syntax error - not this tool's concern.
        finally:
            # Close the connection before the temp dir goes out of scope,
            # otherwise Windows refuses to remove a directory that still
            # has open file handles in it.
            con.close()
    return result


def check_postgres(sql: str) -> Optional[str]:
    """Return an error message if Postgres's real parser rejects `sql`.

    Returns None if it accepts it. Unlike DuckDB, `pglast` (which bundles
    `libpg_query`) is a pure parser with no execution involved, so there's
    no schema/semantic ambiguity and no filesystem side effects to guard
    against - any rejection is a genuine syntax divergence.
    """
    _require(pglast, "pglast")
    try:
        pglast.parse_sql(sql)
    except pglast.Error as err:
        return str(err)
    return None


def check_sqlite(sql: str) -> Optional[str]:
    """Return an error message if SQLite's real parser rejects `sql`.

    Returns None if it accepts it (including if it fails for a non-syntax
    reason). Unlike DuckDB, sqlite3 doesn't distinguish a syntax error from
    other operational errors (e.g. a missing table) by exception type - both
    raise `sqlite3.OperationalError` - so this filters by message content
    instead: only a message containing one of `SQLITE_SYNTAX_MARKERS` counts
    as a divergence.
    """
    result: Optional[str] = None
    with _scratch_cwd():
        con = sqlite3.connect(":memory:")
        try:
            con.execute(sql)
        except sqlite3.Error as err:
            message = str(err)
            if any(marker in message for marker in SQLITE_SYNTAX_MARKERS):
                result = message
        finally:
            # Close before the temp dir is removed - see check_duckdb.
            con.close()
    return result


CHECKERS: dict[str, Callable[[str], Optional[str]]] = {
    "duckdb": check_duckdb,
    "postgres": check_postgres,
    "sqlite": check_sqlite,
}


def iter_statements_from_sql(dialect: str, raw: str) -> Iterator[tuple[int, str]]:
    """Yield (line_no, raw_sql) for each statement in `raw`.

    `raw` is treated as plain SQL, not a Jinja template, even though
    sqlfluff's default templater is Jinja - some legitimate SQL (e.g. nested
    array literals) can confuse it. So this bypasses templating entirely, the
    same way existing dialect fixture tests do
    (test/dialects/dialects_test.py's `lex_and_parse`), by handing the
    parser an already-rendered file.

    Statements sqlfluff itself can't fully parse are skipped (with a warning
    on stderr), since an engine rejecting them isn't a divergence.
    """
    config = FluffConfig(overrides={"dialect": dialect})
    templated_file = TemplatedFile.from_string(raw)
    rendered_file = RenderedFile(
        [templated_file], [], config, {}, templated_file.fname, "utf8", raw
    )
    parsed = Linter(config=config).parse_rendered(rendered_file)
    for violation in parsed.violations:
        print(
            f"Warning: sqlfluff ({dialect}) could not parse line "
            f"{violation.line_no}, skipping it: {violation.desc()}",
            file=sys.stderr,
        )
    for statement in parsed.tree.recursive_crawl("statement", recurse_into=False):
        if "unparsable" in statement.descendant_type_set:
            continue
        yield statement.pos_marker.working_line_no, statement.raw


def run_sql(
    dialects: set[str], raw: str
) -> tuple[list[str], list[str], list[tuple[str, int, str, str]]]:
    """Check SQL text `raw` against each of `dialects`.

    Returns (checked_dialects, skipped_dialects, findings), where each
    finding is (dialect, line, sql, error).
    """
    checked = []
    skipped = []
    findings = []
    for dialect in sorted(dialects):
        checker = CHECKERS.get(dialect)
        if checker is None:
            skipped.append(dialect)
            continue
        checked.append(dialect)
        for line_no, sql in iter_statements_from_sql(dialect, raw):
            error = checker(sql)
            if error is not None:
                findings.append((dialect, line_no, sql, error))
    return checked, skipped, findings


def format_report(
    checked: list[str],
    skipped: list[str],
    findings: list[tuple[str, int, str, str]],
) -> str:
    """Format the results of `run_sql` as a Markdown report."""
    lines = ["# Dialect SQL Validation"]
    if checked:
        lines.append(f"Checked against a real engine: {', '.join(checked)}")
    if skipped:
        lines.append(f"No real-engine validator available yet: {', '.join(skipped)}")
    if findings:
        lines.append("")
        lines.append("| Dialect | Line | SQL | Error |")
        lines.append("|---|---|---|---|")
        for dialect, line_no, sql, error in findings:
            sql_cell = sql.replace("|", "\\|").replace("\n", " ")
            error_cell = error.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {dialect} | {line_no} | `{sql_cell}` | {error_cell} |")
    elif checked:
        lines.append("")
        lines.append("No divergences found.")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dialects",
        nargs="+",
        required=True,
        help="Dialect names to check (e.g. duckdb postgres sqlite).",
    )
    parser.add_argument(
        "--sql",
        metavar="PATH",
        required=True,
        help="File of raw SQL to check, or '-' for stdin.",
    )
    args = parser.parse_args(argv)

    raw = (
        sys.stdin.read()
        if args.sql == "-"
        else Path(args.sql).read_text(encoding="utf-8")
    )
    checked, skipped, findings = run_sql(set(args.dialects), raw)
    print(format_report(checked, skipped, findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
