# Parity test cases

Data-driven regression cases for the Python-vs-Rust engine parity suite.
These fixtures are consumed by `test/core/parser/parity/cases_test.py`; the
shared capture/compare machinery (and the full strictness contract) lives in
`test/core/parser/parity/compare.py`.

Parity tests are **differential**: the same input is run through two engine
paths and the results must be byte-identical. That is why cases carry no
expected output - only the input (SQL), the environment (dialect, config)
and metadata. The Python engine is always the reference.

## The strictness contract

Every comparison captures at **maximal** strictness:

- the parse tree in tuple form with raws, metas and **position markers**,
- `stringify()` bytes (which carry UnparsableSegment "Expected:" messages),
- the raw round-trip,
- per-segment normalization kwargs
  (`quoted_value` / `escape_replacements` / `trim_chars` / `casefold`),
- for raised exceptions: type, message, and `SQLBaseError`
  position/flag attributes (`PanicException` included),
- for lexer cases: token class, raw, type, class_types, position marker and
  normalization kwargs, plus lex errors with their messages.

There is deliberately no per-case way to capture *less*. A known divergence
is instead declared per-leg with an `xfail` entry, which becomes a **strict**
xfail: the moment the underlying gap is fixed, CI flags the stale marker.

## Case format

Each `*.yml` file groups cases for one theme. Top-level keys are case names;
an optional `_meta: {kind: ...}` key selects the driver:

- `parser` (default) - runs each case on two legs:
  - `python_vs_rust`: pure-Python `Parser` vs `RustParser` (legacy
    convert+apply build path),
  - `native_vs_legacy`: `RustParser`'s fused native-AST build path vs its
    legacy path (Python-vs-native parity follows transitively).
- `lexer` - one leg (`lexer`): `PyLexer` vs `PyRsLexer` token streams.
- `invariants` - one leg (`invariants`): structural well-formedness of the
  raw `RsMatchResult` (bounds, ordering, overlap, zero-length rules); no
  comparison - a violation is a rust-core bug by definition.

Case fields:

| Field         | Meaning |
|---------------|---------|
| `sql`         | Inline SQL input (mutually exclusive with `sql_fixture`). |
| `sql_fixture` | Path under `test/fixtures/dialects/` to read the SQL from - use this when the repro is an already-shipped dialect fixture. |
| `dialect`     | Dialect label; defaults to `ansi`. |
| `configs`     | Optional config mapping. Plain keys are `FluffConfig` overrides (e.g. `max_parse_depth`); dotted keys are section paths applied via `set_value` (e.g. `indentation.indented_joins`), which mirrors ini-file typing (int, not bool). |
| `templater`   | Set to `jinja` to drive the case through the Linter (template placeholder tokens; violations are compared too). |
| `context`     | Jinja context variables for templated cases. |
| `pins`        | REQUIRED, human documentation: the bug class this case pins, with issue/commit references where they exist. |
| `expect`      | Optional sanity expectation(s) on the reference leg, guarding the case against going vacuous: `tree`, `clean_tree` (tree with no unparsable segments), `error`, `quoted_kwargs`. String or list. |
| `xfail`       | Optional mapping of leg name → reason. Produces a strict xfail for that leg only. |

## Adding a parity regression

1. Minimize the repro to SQL + dialect + config.
2. Add a case to the matching theme file (or start a new one) with a `pins`
   description of the bug class.
3. If the divergence is real and unfixed, declare it under `xfail` for the
   diverging leg with a precise reason - CI will then force the marker to be
   removed when the engine gap is closed.
4. Well-formed SQL that belongs in the dialect corpus should go to
   `test/fixtures/dialects/` instead (the whole corpus is swept three-way by
   `test/core/parser/parity/corpus_test.py`); reference it here via
   `sql_fixture` only if it needs a pinned parity annotation.

## Related coverage

- `test/core/parser/parity/corpus_test.py` - three-way sweep of every
  dialect fixture at the same strictness.
- `test/core/parser/parity/special_cases_test.py` - parity checks needing
  Python-side instrumentation (logging bytes, parse-node accounting,
  recursion budgets, hash-seed stability).
- `test/core/parser/parity/grammar_test.py` - every dialect's expanded
  grammar must have no dangling refs.
- `utils/parity_audit/` - the exploration harnesses (fuzzers, differential
  sweeps, table checks) these cases were distilled from, with the campaign
  log in `AUDIT_STATE.md`. They are exploration tools, not CI tests.
