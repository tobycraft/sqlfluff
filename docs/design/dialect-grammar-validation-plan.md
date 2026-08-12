# Dialect Grammar Validation

Personal design doc — not filed upstream as a GitHub issue or PR.

## Motivation

Dialect grammars (`src/sqlfluff/dialects/dialect_*.py`) are built from composable
primitives (`Sequence`, `OneOf`, `AnyNumberOf`, ...), and any element can be marked
`optional=True`. Each one is an unverified claim that a real database accepts the
statement without that element.

The existing test suite (2,213 `.sql`/`.yml` fixture pairs) only proves the positive
direction: that specific SQL *does* parse with SQLFluff. Nothing proves the SQL
would actually be accepted by a real database's parser. A reviewer approving a
grammar PR has no fixture, tool, or test run that answers that question. This gap
has already caused real, merged regressions (sqlfluff/sqlfluff #6426, #7427).

## Key structural fact this plan is built around

53 dialect files exist; **27 of them are built via `X_dialect = ansi_dialect.copy_as(...)`
or another dialect's `copy_as`**, often multi-level (`ansi → postgres → greenplum`,
`ansi → mysql → mariadb/doris/starrocks`). A widening change to a parent dialect file
(most commonly `dialect_ansi.py` or `dialect_mysql.py`) silently affects every child
dialect. This is the change most likely to cause a #6426/#7427-style regression, so
every layer below is scoped against the *effective* grammar across the inheritance
graph, not just the file that changed.

## Requirements

- Catch grammar changes that make sqlfluff's own grammar internally inconsistent or
  newly permissive, without needing an external dependency (self-consistency).
- Let a PR prove that SQL which should stay invalid still fails to parse, the same
  way positive fixtures prove valid SQL parses (negative fixtures).
- Where a real database's own parser is available, validate SQL generated directly
  from the dialect grammar against it — not hand-written fixtures — so every branch
  and every optional element the grammar defines gets exercised, not just what a
  contributor thought to check in.
- Every check must be scoped to what a PR actually touches, so it's fast enough to
  run synchronously in CI — not deferred to an async job decoupled from the PR under
  review.
- Prefer zero-server ground truth; don't require a database server unless there's no
  other way to get a real answer.
- Never drop an unresolved finding silently — untriaged divergence must stay visible
  and markable, not deleted or ignored.
- Dialects without a real-engine checker must say so explicitly on the PR, not fail
  silently and imply a guarantee that isn't there.
- A PR-triggered scan must be limited to the dialect(s) it actually modifies — not
  the whole corpus — and must only surface problems newly introduced by that PR,
  never pre-existing ones.

## Design

### Layer 1 — Grammar self-consistency

`utils/dialect_grammar_diff.py` imports the dialect module in isolation at each ref
(base and head — separate `sys.modules` namespace or subprocess per ref) and walks
the *resolved* grammar objects for the target dialect, rather than diffing file text.
A live-object diff is necessary because widening is frequently expressed via
`dialect.replace(...)` or `Segment.match_grammar.copy(insert=..., remove=...)` on
grammar imported from another module — neither shows up as a literal kwarg change in
a single file's source.

- **Resolves the effective grammar across the inheritance graph.** When a changed
  segment lives in a parent dialect file (e.g. `dialect_ansi.py`), the tool
  enumerates every dialect that transitively inherits it via `copy_as` chains and
  reports the change once per affected dialect, not just against the file that
  changed.
- Reports every `optional=`/branch-count change per affected dialect, flagging which
  ones make the grammar more permissive.
- `--strict` fails if a widening change has no matching new negative fixture in the
  same diff, for any dialect it affects.
- This is also what tells layer 3 which grammar elements, in which dialects, to
  generate SQL for.
- The same dual-import, live-object-diff machinery is reused by layer 3's
  base-vs-head filtering (below) — one implementation, not two.

### Layer 2 — Negative fixtures

`test/fixtures/dialects/<dialect>/invalid/*.sql` mirrors the existing positive-fixture
convention: SQL that must fail to parse, collected and run automatically by
`test__dialect__invalid_file_does_not_parse`.

- The existing hand-written test `test__dialect__rejects_trailing_comma_after_final_cte`
  (in `test/dialects/dialects_test.py`) migrates into this convention as the first
  seed fixture(s) — one mechanism for negative tests, not two, and it proves the
  convention works on a real historical regression.
- Generation (layer 3) covers the combinatorial space; fixtures remain the place a
  human pins down a specific known regression by name.

### Layer 3 — Grammar-driven SQL generation

**Implemented** as a single script, `utils/generate_dialect_sql.py` (tests in
`test/utils/generate_dialect_sql_test.py`), rather than the four-sub-phase split
originally sketched here. Reading the actual grammar primitives
(`src/sqlfluff/core/parser/grammar/`) showed the problem was smaller in practice
than in the abstract:

- `dialect_selector(name)` returns an already-expanded `Dialect`, so
  `SegmentGenerator` lambdas are pre-resolved — nothing extra to handle.
- `AnyNumberOf.is_optional()` already encodes "optional, or `min_times == 0`",
  and `OneOf`/`AnySetOf`/`Delimited` all inherit from it — one branch-handling
  code path covers all three.
- Terminal literals split into two buckets: `StringParser`/`MultiStringParser`
  carry their own text; everything else (mostly identifiers/literals) is reached
  through a `Ref` with a small, predictable set of naming patterns
  (`NakedIdentifierSegment`, `TableReferenceSegment`, ...), so one small exact-name
  dict plus a handful of suffix rules (`*IdentifierSegment`, `*ReferenceGrammar`,
  ...) covers the large majority of dialects without per-dialect vocabularies.

**What it does:** walks `Sequence`/`OneOf`/`AnySetOf`/`AnyNumberOf`/`Bracketed`/
`Delimited`/`Ref` from a named entry point (`--dialect`, `--segment`) and renders
a **minimal baseline** (every optional element omitted) plus one variant per
optional element (added back in) and one variant per `OneOf`/`AnySetOf` branch
(swapped in, ranked by a shallow "shortest render wins" probe so branch selection
doesn't default to whichever exotic form is listed first). Minimal-as-baseline
was a fix made during implementation: an "everything present" baseline was tried
first and almost never parsed cleanly (optional clauses combining in ways real
grammars don't expect); minimal is far more robust to build single-change
variants on top of. Bounded by `--max-depth` (cycle guard + recursion cap) and
`--max-examples` (breadth cap). Every generated example is filtered through
sqlfluff's own `Linter` (`self_check`) before being printed, catching generator
bugs before they'd reach a human or a future layer 4.

**Deliberately deferred, not part of what shipped:**
- Wiring to layer 1 (auto-picking `--segment` from "what changed in this PR").
- Base-vs-head generation to filter pre-existing issues surfaced via shared/
  `Ref`'d grammar: the same targeted element would be generated from both the
  base and head versions of the grammar (via the isolated dual-import from
  layer 1) and only a real-engine verdict that changed between the two would
  surface as a finding.
- Per-dialect vocabulary overrides (the shared dict has covered every dialect
  tried so far; dialect-specific overrides can be added if a gap turns up).
- Feeding output to a real-engine check (layer 4) — this only produces text.

### Layer 4 — Real-engine ground truth

**Implemented** as `utils/realengine_check.py` (tests in
`test/utils/realengine_check_test.py`), consuming layer 3's `generate()` +
`self_check()` directly (same sys.path import trick as layer 3's own tests, no
subprocess). Three engines are wired up, all confirmed installed and working
in-session, added to `requirements_dev.txt` under "utils/realengine_check.py
dependencies":

- **Postgres**, via `pglast` (bundles `libpg_query`). `pglast.parse_sql()` is a
  pure parser - no execution, no schema - so `pglast.Error` alone is a reliable
  syntax-only signal.
- **DuckDB**, via the `duckdb` package. Investigated `json_serialize_sql(sql)` as
  a DuckDB equivalent of `pglast.parse_sql` first (also pure-parse: confirmed
  `SELECT * FROM nonexistent_table` returns `{"error": false}`, no catalog
  resolution) - but it **only supports `SELECT` statements**; `CREATE TABLE ...`
  returns `{"error_type": "not implemented", "error_message": "Only SELECT
  statements can be serialized to json!"}` even when the SQL is fine, and most
  of what layer 3 generates is DDL/DML. The working alternative: execute against
  a fresh in-memory `duckdb.connect(":memory:")` per call and catch
  `duckdb.ParserException` specifically - not the broader `duckdb.Error`, since
  real execution surfaces many non-syntax exception types (`CatalogException`
  for a missing table, `BinderException`, `ConstraintException`, ...) that must
  not be treated as syntax divergences. Confirmed distinct and reliable by direct
  test.
- **SparkSQL**, via `pyspark` (local, in-process `local[1]` master - no cluster).
  `spark.sql()` triggers parse + analysis eagerly, same shape as the other two.
  Two things this engine taught that the first two didn't:
  1. **Session startup is ~7s** - far more than pglast (instant) or duckdb
     (~13ms/connection). A fresh-session-per-call pattern (what postgres/duckdb
     use) would make a 50-example run take 6+ minutes on session startup alone.
     Fix: a single `SparkSession` is created lazily on first use and cached at
     module level (`_get_spark_session()`), reused for every check in the
     process. Catalog-state accumulation across calls is possible as a result
     (an earlier example's `CREATE TABLE foo` can persist), but it's a non-issue
     for the same reason it was for DuckDB - only the syntax-error exception type
     is treated as a divergence, and state accumulation only changes *semantic*
     outcomes, which are already ignored.
  2. **`pyspark.errors.PySparkException` is not an exhaustive catch-all** the
     way `duckdb.Error`/`pglast.Error` are. `ParseException` is a subclass of
     `AnalysisException` (so it must be caught first - confirmed:
     `SELEC 1` -> `ParseException`; `SELECT * FROM nonexistent_tbl` -> plain
     `AnalysisException`), but some inputs trip Spark's own internal bugs
     entirely outside that hierarchy - `CREATE STREAMING TABLE foo` raised a raw
     `py4j.protocol.Py4JJavaError` wrapping a Scala `AssertionError`
     ("No plan for CreateStreamingTable..."), which crashed the checker on first
     try. Fixed by broadening the fallback to catch `Exception`, not just
     `PySparkException` - none of that is evidence about sqlfluff's grammar
     either way, so it's correctly treated the same as any other non-syntax
     outcome. Useful precedent for a future engine: don't assume the library's
     own declared exception hierarchy is exhaustive; verify with a deliberately
     weird/unsupported statement, not just a clean syntax-error probe.

**What it does:** for each layer-3-generated example that passes sqlfluff's own
`self_check`, calls the dialect's checker (`check_postgres`/`check_duckdb`/
`check_sparksql`). Three outcomes: agreement (silent, the expected case), a known
divergence (exact `(dialect, sql)` pair listed in `utils/realengine_skiplist.json`
with a reason - printed as `[skipped]`, doesn't fail the run), or an unresolved
divergence (printed as `[DIVERGENCE]` with the real parse error, fails the run
with exit code 1). The skiplist starts empty - no divergences have been triaged
yet, since the backfill audit (item 5 below) is a separate, later step.

**Confirmed working end-to-end** for all three engines. Postgres: running against
`SelectStatementSegment` and `InsertStatementSegment` surfaced e.g. `SELECT
DISTINCT` with nothing after it (sqlfluff's grammar allows an empty select list;
Postgres's real parser doesn't), and `INSERT INTO foo (foo) DEFAULT VALUES`
(sqlfluff allows combining an explicit column list with `DEFAULT VALUES`;
Postgres rejects the combination). DuckDB: the same bare `SELECT`/`SELECT
DISTINCT` finding reproduces (cross-engine confirmation of the same sqlfluff
grammar gap), plus `CREATE TABLE foo (UNIQUE (foo))` and similar (sqlfluff
allows a table with only a constraint and no column definitions; DuckDB requires
at least one column). SparkSQL: `CREATE TEMP TABLE foo` (Spark requires a
provider for temp tables), `CREATE LIVE TABLE foo` (rejected outright by Spark's
parser), and `CREATE TABLE "foo"` (Spark quotes identifiers with backticks, not
double quotes). None of these are in the skiplist - they're live,
currently-unresolved findings, not something this task triaged away.

- Each engine pins to one fixed version (Postgres `18.4` via `pglast`, DuckDB
  `1.5.5`, PySpark `4.2.0`, all versions as seen in this environment) - printed
  at the start of every run. A pass means "the pinned engine version this
  checker uses accepts this," not "every version of that engine sqlfluff's
  dialect targets accepts this." Stated in the module docstring and the run
  banner for all three engines; editing CONTRIBUTING.md itself is left for when
  the CI job lands (deferred, see below).
- A skiplist convention (`utils/realengine_skiplist.json`, matched by exact
  `(dialect, sql)` string equality since generation is deterministic) lets a
  specific known divergence be marked and excluded with a reason, without
  silently dropping it from view - it still prints as `[skipped]`. Already
  multi-dialect (keyed by `(dialect, sql)`), so neither DuckDB nor SparkSQL
  needed skiplist changes.
- Unlike pglast/duckdb (self-contained compiled wheels), PySpark needs a local
  Java runtime available on the machine - confirmed present and working in this
  session, but a materially different dependency profile worth calling out
  explicitly (done, in `requirements_dev.txt` and here).

**Deliberately deferred, not part of what shipped** (same "keep it simple"
precedent as layer 3):
- The GitHub Actions CI job itself.
- Dialects without a real-engine checker (everything except postgres) explicitly
  saying so on the PR - that's a CI/PR-comment mechanism, not something a
  standalone CLI script does on its own.
- The backfill audit of existing postgres fixtures against pglast (item 5) - a
  separate follow-up now that the checker exists to run it with. The two live
  divergences found above are a first, unaudited taste of what that backlog will
  contain, not the backlog itself.

### Backlog handling

Rather than leaning on `continue-on-error` indefinitely once layer 4 ships:

- A baseline/allowlist file is committed, enumerating the current ~16 known
  postgres divergences at the time the check goes live.
- CI compares new findings against the baseline: anything in the baseline doesn't
  block; anything new does.
- This keeps the backlog visible and trackable while still letting new PRs merge
  without requiring the backlog be triaged first. `continue-on-error` comes off the
  CI job once the baseline exists, not once the backlog is resolved.

### Base-ref resolution

Both tools' `--base` resolves to `git merge-base(<base>, HEAD)` — the commit this
branch actually forked from — rather than diffing against `<base>`'s current tip;
the default (no `--base` given) auto-detects the repo's default branch instead of
the unhelpful literal `HEAD`.

- GitHub Actions' default checkout is shallow, and `merge-base` needs the base
  branch's history present. The CI workflow explicitly fetches the base ref (a
  targeted fetch, not a full unshallow) before either tool runs, and fails loudly
  — rather than silently falling back to comparing against HEAD — if the base ref
  isn't fetchable.
- Passing `--base HEAD` explicitly still works for the local, uncommitted-changes
  dev loop.

**Explicitly out of scope for now:** MySQL/MariaDB, T-SQL, and other engines that
have no standalone/serverless parser — real ground truth for them requires a running
engine (a container, at minimum). Investigated and rejected as a shortcut: a
`runsql`-style CLI wrapper — it also falls back to Docker for exactly these engines,
confirming there's no serverless parser being missed. Building this is deferred to
an environment with real engine access.

## Implementation plan

| # | Item | Status |
|---|------|--------|
| 1 | `dialect_grammar_diff.py`: dual-import live-object diff, inheritance-graph-aware (resolves effective grammar for all affected child dialects) + CONTRIBUTING/AGENTS docs | Not started |
| 2 | `invalid/` fixture convention, test wiring, migrate `test__dialect__rejects_trailing_comma_after_final_cte`, seed fixtures, docs | Not started |
| 3 | Layer 3 grammar-driven SQL generator: `utils/generate_dialect_sql.py` + `test/utils/generate_dialect_sql_test.py`. Built as one script rather than the original 3a-3d split — see note below. | **Done** |
| 4 | `realengine_check.py` for postgres (pglast), duckdb (`duckdb`), and sparksql (`pyspark`) + skiplist convention + tests | **Done** — CI job and "no checker for this dialect" PR annotation still not started |
| 5 | Backfill audit of existing postgres fixtures + commit baseline/allowlist for the ~16 known divergences | Not started |
| 6 | `--base` → `merge-base(base, HEAD)` in both tools, default auto-detects fork point; CI workflow fetches base ref explicitly (shallow-clone fix) | Not started |
| 7 | Wire `realengine_check.py` to consume layer-3-generated SQL as primary source (fixtures remain for self-consistency tests) | Not started |
| 8 | Base-vs-head generation to filter pre-existing issues via shared/`Ref`'d grammar — shares the dual-import mechanism built in item 1, not a separate implementation | Not started |
| 9 | Drop `continue-on-error` once the baseline/allowlist (item 5) + generation (items 3/7) ship | Not started |
| 10 | Real-engine checkers for MySQL/MariaDB, T-SQL, others | Not started — blocked on an environment with real engine/container access |

## Open items still not settled

- Exact mechanism for the dual-import isolation (subprocess per ref vs.
  `sys.modules` namespace trickery vs. `importlib` with a fresh module name) —
  needs a spike before committing to one.
- Where the baseline/allowlist file lives and its exact format.
- Whether `pglast` has wheels for every platform/Python version sqlfluff currently
  supports — not yet verified.

## Status of this doc

Personal planning document, not filed as a GitHub issue or PR. Nothing has been
implemented yet; all ten items in the table above are still "Not started."
