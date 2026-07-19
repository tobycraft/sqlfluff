# Parity audit tooling

Differential harnesses used to audit byte-level parity between the pure-Python
parser/lexer and the Rust engine (`sqlfluffrs`). These are exploration tools,
not CI tests - the fast, deterministic regression guards distilled from their
findings live in `test/core/parser/rust_parser_test.py` and
`test/core/parser/rust_parity_guards_test.py`.

Run them from this directory (running from the repo root shadows the installed
`sqlfluffrs` extension with the source tree).

- `pyrs_harness.py` - strict Python-vs-Rust parse comparison (trees with
  positions, stringify bytes, raw round-trip, full exception details) over the
  dialect fixture corpus.
- `lexer_diff.py` - PyLexer vs PyRsLexer token-stream differential (raw, type,
  class, positions, normalization kwargs, errors).
- `table_check.py` - semantic cross-check of the generated Rust grammar tables
  against the expanded Python dialect library (dispatch completeness, dangling
  refs, variant/child/flag/parse-mode/terminator parity, parser aux fidelity).
- `invariant_fuzz.py` - structural invariant validator for raw RsMatchResults
  (bounds, overlap, ordering, zero-length rules) driven by corpus mutation
  fuzzing; any violation is a rust-core bug with no Python comparison needed.
- `regex_diff.py` - RegexParser accept-decision differential between Python's
  `regex` module semantics (upper-cased raw + IGNORECASE) and the Rust
  regex/fancy-regex semantics, over every dialect's parsers and real corpus
  token raws (requires building the small `rxdiff` helper crate it describes).
