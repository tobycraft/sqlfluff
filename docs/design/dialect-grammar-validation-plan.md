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
variants on top of. Bounded by a cycle guard (see below) and `--max-examples`
(breadth cap). Every generated example is filtered through
sqlfluff's own `Linter` (`self_check`) before being printed, catching generator
bugs before they'd reach a human or a future layer 4.

**Configurable coverage (`--coverage 0-100`), added after the fact.** The
original single-toggle-from-baseline design turned out to have two compounding
limitations, found by direct investigation of `postgres`/`CreateTableStatementSegment`:
its baseline renders as bare `CREATE TABLE foo ( )` — the column list is itself
`optional=True`, so it's omitted by default — and because candidate discovery
only ever registered new candidates during the *baseline* walk, nothing nested
inside that omitted column list (real column definitions, constraints) was
reachable at *any* `--max-examples`, not just deprioritized. Separately, every
example was exactly one toggle away from baseline, so interacting optional
clauses (e.g. `WHERE` + `GROUP BY` together) were never exercised jointly.

Fixed by generalizing `_WalkState.target_index: Optional[int]` to
`target_indices: frozenset[int]`, switching candidate identity from a
per-walk position counter to `(kind, id(payload))` (grammar objects are
constructed once per dialect load and reused for the process's lifetime, so
`id()` is a stable cross-walk identity), and adding a `requires: frozenset[int]`
field to each candidate (its prerequisite chain of ancestor optional/branch
points). This enabled two internal mechanisms: **discovery rounds** (render an
already-known candidate, look for *new* candidates nested inside it, repeat)
and **combination width** (toggle several known candidates on simultaneously in
one example, round-robin through the candidate list). A single `--coverage
0-100` CLI flag maps onto both via a fixed formula
(`discovery_depth = round(coverage/100 * 5)`,
`combination_width = max(1, round(coverage/100 * 4))`) — considered exposing the
two knobs separately, but simplicity won out; `coverage=100` means "the most
thorough setting this tool considers practical," not literally exhaustive
(`--max-examples` still caps output regardless). `coverage=0` is the default and
was verified to reproduce the pre-existing behavior for every case in the test
suite (one incidental, disclosed improvement: output is now deduped by exact
text, since the old code could emit byte-identical examples more than once).

Confirmed working: `postgres`/`CreateTableStatementSegment --coverage 100`
reaches real column definitions with `NULL`/`CHECK`/`WITH OPTIONS` constraints
and partition clauses (`FOR VALUES FROM ... TO ...`) that `--coverage 0` could
never produce, and running that richer output through layer 4's `check_duckdb`
immediately found new divergences (e.g. `CREATE TABLE foo (CHECK (CURRENT_CATALOG))`)
that the old shallow generation never surfaced. No crashes across all 28
dialects at `--coverage 50`/`100`, and a worst-case self-referential entry point
(`ExpressionSegment` at `--coverage 100`) still completes in ~1s.

**Vocab-gap warnings deduped per segment.** Higher coverage means many more
walks per `generate()` call, and `_terminal_for`'s "no vocab entry for X"
notice was printed on every single occurrence, not once per distinct `X` -
fine at `coverage=0`'s handful of walks, unusable at `coverage=100` (one real
run: 6679 stderr lines, almost all exact repeats of names already seen).
Fixed by threading a `warned: set[str]` through `_WalkState` the same way
`known` already is - shared across every walk in one `generate()` call
(including `_branch_score`'s scoring probes, which hit the same terminals and
would otherwise warn into a separate, discarded set) - so each gap name
prints at most once per segment. Same run: 134 lines, one per distinct name.

**Two more bugs found via a user report of zero output** (`--dialect mariadb
--segment DeleteStatementSegment`), both in grammar shared across most
dialects, so the fix generalized rather than being a one-off patch:

1. `TableExpressionSegment` - used by nearly every dialect's FROM clause -
   lists `Ref("BareFunctionSegment")` (bare no-parens functions like
   `CURRENT_DATE`) ahead of `Ref("TableReferenceSegment")`. Both resolve in a
   single token, so `_branch_score` ties them, and the stable sort silently
   picked whichever was declared first - the special case, not the normal
   one. Confirmed: `mariadb`/`DeleteStatementSegment` generated only `DELETE
   FROM CURRENT_DATE ...`, self-check failing every single example. Fixed
   with a secondary sort key, `_prefers_reference`: on a score tie, prefer a
   `Ref` whose name matches the same `SUFFIX_VOCAB` identifier/reference
   suffixes already used for terminal vocab - a principled, reusable
   tie-break rather than a mariadb-specific special case.
2. `Conditional` grammar objects (wrap an `Indent`/`Dedent` meta segment that
   only fires per reflow config - confirmed by inspecting `Conditional.__init__`)
   weren't recognized by `_render`'s dispatch at all, so they fell through to
   the generic terminal fallback and rendered as a stray `"1"` - e.g. `FROM
   DUAL 1 1` instead of `FROM DUAL`, corrupting output that was otherwise
   correct. Fixed by giving `Conditional` the same empty-render treatment as
   the existing `MetaSegment` case.

Backfilled a before/after comparison across 8 dialects x 3 segments: 4
dialects (`ansi`, `mysql`, `mariadb`, `sqlite`) had a **zero-valid-example**
`DeleteStatementSegment` before this fix (the `Conditional` bug is in
`FromExpressionSegment`, which `DELETE ... FROM` shares with `SELECT`), all
now produce valid output; other segments saw smaller improvements. Full
28-dialect x 3-segment crash sweep stayed clean throughout.

**Three previously-unenumerated dimensions, fixed.** An audit of "what still
isn't fully enumerated" (beyond optional elements and `OneOf`/`AnySetOf`
branches, both already covered) turned up three decision points that always
rendered exactly one fixed representative and never varied it:
`MultiStringParser` keyword alternatives (e.g. `snowflake`'s `DatetimeUnitSegment`
has 93 templates; `sorted(templates)[0]` was always chosen, the other 92 never
tried), repetition count on `Delimited` (always exactly one item — multi-item
lists and trailing-comma behavior, the literal shape of the trailing-comma
regression that originally motivated this whole project, were structurally
unreachable output), and terminal vocabulary (`TERMINAL_VOCAB`/`SUFFIX_VOCAB`
each mapped to one fixed string — every generated identifier was always
literally `"foo"`). Accepted trade-off, explicitly signed off on: richer
`--coverage 0` output, a slower default run, in exchange for reaching this
content at all — `--max-examples`'s default is unchanged by this pass; raising
it is a separate, later step.

All three register their alternates as ordinary candidates in the same
`_WalkState.known` registry optional-elements and branches already use
(`_WalkState._register` was generalized to accept an explicit dedup key,
since the default `(kind, id(payload))` only works when `payload` is a
grammar object with stable identity — a plain string/int payload, like a
template or vocab value, needs an explicit key built from stable parts, since
string `id()` isn't reliable identity). That means they're discovered and
combined by the exact same discovery-round/combination-width machinery
`--coverage` already drives — no separate mechanism needed.

**Correction found during implementation, not in the original plan for this
work:** the plan going in assumed `Delimited`'s `max_times` (or, absent that,
bare `AnyNumberOf`/`AnySetOf`'s `max_times is None or > 1`) was a reliable
"can this repeat" signal, and that repeating "the same chosen element N times"
was safe for all three grammar types. Both assumptions were wrong, found by
testing rather than caught upfront:
- `OneOf` (which `Delimited` subclasses) hardcodes `max_times=1, min_times=1`
  unconditionally in its own `__init__`, for every instance regardless of how
  many items it can actually match — `max_times` there is leftover "pick
  exactly one branch template" bookkeeping, unrelated to item count. Not a
  usable repeatability signal for `Delimited` at all.
- Applying the same "re-render the same chosen element N times" logic to bare
  `AnyNumberOf`/`AnySetOf` (which the plan called for, since their `max_times`
  *is* genuinely configurable) produced nonsense: `CREATE TABLE foo ( )
  WITHOUT OIDS , WITHOUT OIDS` and `... PARTITION BY RANGE ( ) , PARTITION BY
  RANGE ( )`. Root cause: those containers are frequently a bare `AnyNumberOf`
  wrapping several *different*, heterogeneous sibling clause options (postgres'
  table-options tail wraps `PARTITION BY`/`USING`/`WITH(OUT) OIDS`/`ON COMMIT`/
  `TABLESPACE` this way), not a homogeneous repeatable list — repeating a
  single chosen branch doesn't model "pick several different options," it
  just duplicates one clause.

Fixed by restricting repetition to `isinstance(matchable, Delimited)` only,
dropping the bare-`AnyNumberOf`/`AnySetOf` case from this pass entirely
(comma-delimited lists are reliably homogeneous — the failure mode above
doesn't apply to them). Confirmed via direct grammar introspection after the
fix: `ansi`/`SelectStatementSegment` at `--coverage 0` now includes
`SELECT * , *`, `SELECT * , * , *`, and a trailing-comma `SELECT * , * ,`
example (its `SelectClauseSegment` Delimited has `allow_trailing=True`);
`ansi`/`CreateTableStatementSegment`'s column-list `Delimited`, once its
enclosing optional branch is active, produces `CREATE TABLE foo ( foo , foo
)`; the nonsense duplicate-clause cases are gone. **Known, disclosed
simplification kept from the original plan:** each repetition re-renders the
same chosen element, so a 2-column table's columns share one name/type
(`foo, foo`) rather than varying per repetition — syntactically sufficient for
what this exercises (does the delimiter/trailing-comma/multi-item structure
parse), and any real-engine "duplicate column" complaint a real engine raises
is a semantic error, not a syntax one, so it's already correctly ignored by
every layer 4 checker.

Full 28-dialect x 3-segment (`SelectStatementSegment`,
`CreateTableStatementSegment`, `DeleteStatementSegment`) crash sweep stayed
clean; total runtime for all 84 combinations was under 40 seconds, well
within the "slower is fine, minutes would not be" bound.

**Numeric depth cap dropped; bounded solely by the cycle guard now.**
Prompted by a question about why `Ref("DotSegment")` - a trivial
`StringParser(".", ...)` - was falling back to the generic `"1"` terminal
instead of resolving to `.`. Traced with instrumentation (same class of
false alarm as the earlier `EqualsSegment` question, but this time run to a
concrete root cause): it was **not** the main walk's `--max-depth` (confirmed
by rerunning with it set to 100000 - the warning still fired). The actual
cause was `_branch_score`'s scoring probe, which built its own throwaway
`_WalkState` with a hardcoded `probe_depth=3` completely independent of
`--max-depth`, but wrote into the same shared `warned` set as the real walk
- so its own shallow truncations were reported as if they were real
generator gaps. Every `DotSegment` hit had `state.max_depth == 3,
scoring_probe == True`: 100% probe-driven, not a real-walk truncation.

Rather than patch just the probe, the numeric depth cap was dropped
entirely - from both the main walk and the probe - relying solely on the
pre-existing cycle guard (`active_refs`: a `Ref` name already visited on the
current path renders as a terminal instead of being followed again).
Verified safe before shipping: unbounded-depth single walks across 9
dialect/segment combos, including the worst self-referential case
(`ExpressionSegment`), all completed in under 2ms with no `RecursionError`,
even at Python's default recursion limit (1000) - a dialect has on the order
of ~1200-1400 distinct `Ref` names, an absolute upper bound on how deep any
single path can go before a name repeats and the guard fires. The more
expensive case - `_branch_score` itself unbounded, since it runs once per
sibling at *every* branch point, not once per walk - was also measured:
full `generate()` across all 28 dialects x 3 segments completed in ~1.8s
total, worst single case ~97ms. Confirmed it resolves the reported symptom:
the `DotSegment` warning disappears entirely, and `mariadb`/
`DeleteStatementSegment`'s total distinct vocab warnings drop from 32 to 5 -
the remainder being genuine cycle-guard terminations (e.g.
`WithCompoundStatementSegment` recursing into itself for nested CTEs), not
artifacts. Since `depth` was only ever read for the removed comparison, it
was dropped as a parameter from every function that threaded it through
(`_render`, `_render_sequence`, `_render_repeated`, `_render_branch`,
`_run_walk`), not left as dead plumbing. `--max-depth` is gone from both
CLIs (`generate_dialect_sql.py`, `realengine_check.py`) and from `generate()`'s
signature - a clean removal rather than a deprecated-but-ignored parameter,
consistent with this being internal dev tooling with no external callers to
stay compatible with.

**Deliberately deferred, not part of what shipped:**
- Wiring to layer 1 (auto-picking `--segment` from "what changed in this PR").
- Base-vs-head generation to filter pre-existing issues surfaced via shared/
  `Ref`'d grammar: the same targeted element would be generated from both the
  base and head versions of the grammar (via the isolated dual-import from
  layer 1) and only a real-engine verdict that changed between the two would
  surface as a finding.
- Per-dialect vocabulary overrides (the shared dict has covered every dialect
  tried so far; dialect-specific overrides can be added if a gap turns up).

### Layer 4 — Real-engine ground truth

**Implemented** as `utils/realengine_check.py` (tests in
`test/utils/realengine_check_test.py`), consuming layer 3's `generate()` +
`self_check()` directly (same sys.path import trick as layer 3's own tests, no
subprocess). Four engines are wired up, all confirmed installed and working
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
- **ClickHouse**, via `chdb` (embedded ClickHouse - no server). Two things
  simpler here than DuckDB/Spark, one thing harder:
  - Simpler: `chdb.query()` is already stateless per call (confirmed: a
    `CREATE TABLE` in one call does not persist to the next), so unlike DuckDB
    there's no need for an explicit fresh-connection-per-call pattern, and
    unlike Spark there's no expensive session to reuse - just call it directly.
  - Harder: `chdb` exposes no typed exceptions at all - every error is a plain
    `RuntimeError`. So this checker can't filter by exception type like the
    other three; it filters by *message content* instead. Confirmed reliable
    across 8 distinct test cases: ClickHouse's own error messages consistently
    end with a symbolic error name in parentheses (`(SYNTAX_ERROR)` for every
    genuine syntax error tried; `(UNKNOWN_TABLE)`, `(UNKNOWN_STORAGE)`, etc.
    for semantic ones), so only `"(SYNTAX_ERROR)" in message` counts as a
    divergence.

**What it does:** for each layer-3-generated example that passes sqlfluff's own
`self_check`, calls the dialect's checker (`check_postgres`/`check_duckdb`/
`check_sparksql`/`check_clickhouse`). Three outcomes: agreement (silent, the
expected case), a known divergence (exact `(dialect, sql)` pair listed in
`utils/realengine_skiplist.json` with a reason - printed as `[skipped]`, doesn't
fail the run), or an unresolved divergence (printed as `[DIVERGENCE]` with the
real parse error, fails the run with exit code 1). The skiplist starts empty - no
divergences have been triaged yet, since the backfill audit (item 5 below) is a
separate, later step.

**Confirmed working end-to-end** for all four engines. Postgres: running against
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
double quotes). ClickHouse: `CREATE TABLE foo ENGINE foo COMMENT foo` - and this
one traces to a genuine grammar bug, not just a real-vs-sqlfluff feature gap:
`dialect_clickhouse.py`'s `CREATE TABLE`/`CREATE DATABASE` `COMMENT` clause is
defined as `OneOf(Ref("SingleIdentifierGrammar"), Ref("QuotedIdentifierSegment"))`,
which accepts a bare unquoted identifier - real ClickHouse requires a string
literal (`COMMENT 'foo'`). None of these are in the skiplist - they're live,
currently-unresolved findings, not something this task triaged away.

- Each engine pins to one fixed version (Postgres `18.4` via `pglast`, DuckDB
  `1.5.5`, PySpark `4.2.0`, ClickHouse `26.5.1.1` via `chdb`, all versions as
  seen in this environment) - printed at the start of every run. A pass means
  "the pinned engine version this checker uses accepts this," not "every
  version of that engine sqlfluff's dialect targets accepts this." Stated in
  the module docstring and the run banner for all four engines; editing
  CONTRIBUTING.md itself is left for when the CI job lands (deferred, see
  below).
- A skiplist convention (`utils/realengine_skiplist.json`, matched by exact
  `(dialect, sql)` string equality since generation is deterministic) lets a
  specific known divergence be marked and excluded with a reason, without
  silently dropping it from view - it still prints as `[skipped]`. Already
  multi-dialect (keyed by `(dialect, sql)`), so none of DuckDB, SparkSQL, or
  ClickHouse needed skiplist changes.
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
| 4 | `realengine_check.py` for postgres (pglast), duckdb (`duckdb`), sparksql (`pyspark`), and clickhouse (`chdb`) + skiplist convention + tests | **Done** — CI job and "no checker for this dialect" PR annotation still not started |
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
