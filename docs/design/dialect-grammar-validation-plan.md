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

Walks the actual grammar objects sqlfluff builds in memory for a dialect, starting
from the segments layer 1 flags as changed, and renders concrete SQL text. Broken
into explicit sub-phases given its size:

1. **Declarative-grammar walker**: `Sequence`, `OneOf`, `AnySetOf`, `AnyNumberOf`,
   `Bracketed`, `Delimited`, `Ref` — with/without each optional, one example per
   `OneOf`/`AnySetOf` branch.
2. **Terminal vocabulary strategy**: a small fixed vocabulary of representative
   identifiers/literals/keywords, per dialect where dialect-specific quoting or
   literal syntax applies.
3. **Fallback strategy for non-declarative segments**: an explicit, documented
   answer for segments whose matching is hand-written Python (`RegexParser`/
   `StringParser` subclasses, custom `match()` overrides) rather than a composed
   grammar object — these can't be walked the same way and need either a manual
   vocabulary hook or an explicit "not generatable, needs a hand fixture" marker.
4. **Bounds**: a recursion depth limit, plus an explicit per-PR generation breadth
   budget (cap on total generated examples), since nested optional/branch
   combinatorics under one changed high-level clause can otherwise blow up before
   the real-engine check is ever invoked. Budget enforcement happens before layer 4
   runs, so it's what actually protects the "fast enough for synchronous CI"
   requirement.
5. Generation targets whole minimal statements, not bare fragments, since a clause
   is often only meaningful inside a full `SELECT`/`CREATE`/etc.
6. **Base-vs-head generation** filters pre-existing issues surfaced via shared/
   `Ref`'d grammar: the same targeted element is generated from both the base and
   head versions of the grammar (via the isolated dual-import from layer 1), and
   only a real-engine verdict that changed between the two surfaces as a finding.

### Layer 4 — Real-engine ground truth

`utils/realengine_check.py` checks layer 3's generated SQL against a real engine's
own parser. Postgres is wired up via `pglast` (bundles `libpg_query`; no server,
schema, or network needed), which is why it's the only engine covered so far.

- `pglast` bundles `libpg_query` for one fixed Postgres major version. A pass here
  means "the PG version pglast bundles accepts this," not "every PG version
  sqlfluff's postgres dialect targets accepts this." This is stated explicitly in
  CONTRIBUTING and in the PR-facing check output itself, so a green check isn't
  over-read as full-version-range coverage.
- Dialects without a real-engine checker (everything except postgres, for now) say
  so explicitly on the PR — part of layer 4's CI job, not a separate deferred item.
- A `realengine-skip` convention lets a specific generated example be marked and
  excluded with a reason, without silently dropping it from view.

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
| 3a | Layer 3 declarative-grammar walker (`Sequence`/`OneOf`/`AnySetOf`/`AnyNumberOf`/`Bracketed`/`Delimited`/`Ref`, with/without-optional + per-branch) | Not started |
| 3b | Layer 3 terminal vocabulary strategy (per-dialect literals/identifiers/keywords) | Not started |
| 3c | Layer 3 fallback strategy for hand-written (non-declarative) segments | Not started |
| 3d | Layer 3 breadth budget + depth limit enforcement | Not started |
| 4 | `realengine_check.py` for postgres (pglast), documented PG-version caveat, `realengine-skip` convention, "no checker for this dialect" PR annotation, CI job | Not started |
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
