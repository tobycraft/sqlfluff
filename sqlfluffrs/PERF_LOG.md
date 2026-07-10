# Parse-performance work log

Record of the optimization campaign on branch
`claude/benchmark-perf-improvement-sschx9` (July 2026) targeting the CodSpeed
benchmark `test/test_codspeed_tpc_parse.py::test_native_ast_tpcds`
(simulation mode = valgrind instruction counts). **Read this before starting
new parser/benchmark performance work** — it lists what was measured to work,
and (equally important) what was measured NOT to work.

**Result: 19,336,856,656 → 8,995,175,662 instructions per iteration (−53.5%)**
across 25 commits, with byte-identical parse trees throughout.

## Methodology (reproduce before optimizing)

- CodSpeed "simulation mode" counts instructions (valgrind), not wall time.
  Wall time drifts across a session on shared machines; only instruction
  counts are comparable between runs. Wall numbers below are indicative only.
- Definitive measurement: run the benchmark body in a harness with n=1 and
  n=3 iterations under `valgrind --tool=callgrind`; per-iteration cost is
  `(Ir(n3) − Ir(n1)) / 2`. Quick checkpoints: `Ir(n1) − startup`, where
  startup (~1.07G: interpreter + imports + grammar tables) is measured once
  per environment with a no-op harness.
- Build for iteration with `CARGO_PROFILE_RELEASE_LTO=thin maturin build
  --release` (from `sqlfluffrs/`, where `pyproject.toml` lives). The
  committed release profile is fat LTO + `codegen-units = 1`
  (`sqlfluffrs/Cargo.toml`), worth ~2% but much slower to build.
- **Parity gate after every change**: parse all 121 TPC-DS + TPC-H fixture
  queries via `Linter.parse_string` with the native AST path
  (`set_native_ast(True)`) and digest stringify + raws + position markers +
  `as_record()` + violations per query. Every kept commit produced digests
  byte-identical to the pre-optimization baseline.
- Profile with `callgrind_annotate` (debug symbols:
  `CARGO_PROFILE_RELEASE_DEBUG=true STRIP=false`), DHAT for allocation
  counts, and `parse_match_result_with_stats` for match/prune/cache metrics.

## What worked (chronological; numbers from per-commit measurements)

| Commit | Change | Effect |
|---|---|---|
| `c16dc16` | Box `TableParseFrame` (312 B) on the frame stack — moves become pointer copies | −16.1% alone (19.34G → 16.22G); memcpy was 18% of all instructions |
| `1558b4d` | Skip Jinja env for template-free files; recursive dict copy instead of `deepcopy` in `FluffConfig.copy` | wall 2705→2534 ms |
| `1ca10d7` | `RsMatchResult.flatten()`: whole match tree as one pre-order tuple list, one PyO3 call (was ~10 getters × 100k nodes) | wall 2534→2410 ms |
| `55d8230` | `RsParser.parse_with_ast()`: fuse parse + flatten + arena build in one boundary crossing; owned match enables `Arc::try_unwrap`; monotonic arena ids instead of `Uuid::new_v4` | wall 2410→2308 ms |
| `cd1cb05` | Cache `MatchResult.node_count` (was accidentally quadratic via per-commit re-walks); single-pass `from_child_markers`; drop no-op `_recalculate_caches` | wall 2308→2250 ms |
| `7611e4b` | Lexer: one pre-built tuple per token across the PyO3 boundary; skip PositionMarker bisect; reuse caller's TemplatedFile | wall 2250→2088 ms |
| `637fe77`, `3d008ae`, `43debb5`, `0866b08` | Allocation trims: move Node tree into arena, linear dedupe, identity-normalize fast path, merge sorted triggers in `apply()` (no HashMap), Arc-backed `RawString` | mostly allocation-count wins (weigh heavier in Ir than wall) |
| `c2d9356`, `f597251`, `ec9aacf` | Python construction fast paths: PositionMarker as `__slots__` class, direct instance-dict fill for default-`__init__` classes, inline `set_as_parent` writes | wall 2088→1805 ms |
| `fdf9605` | Compute frame-cache key once per frame (Copy struct) | wall 1950→1900 ms |
| `b07048b`, `db389ed` | Allocate-only-after-match in String/Typed parser attempts; `element_children_slice` returning static table slices (`GrammarId` is `repr(transparent)` u32); skip unparsable tree walk for provably-clean parses | wall →1745 ms |
| `cb12a39` | Terminator SmallVec inline capacity 4→12 (560k heap spills/pass); fast-path meta construction | cumulative −47.1% at this point |
| `c8fea34` | Fat LTO + codegen-units=1 | ~2% |
| `cf40a30`, `405cf0f`, `d61ba2c` | **Frame-free terminal evaluation** (`try_terminal_inline`): OneOf candidates, Sequence elements, then Ref-to-terminal targets; inline terminator probes | cumulative −50.6% definitive (9.55G) at `405cf0f`; Ref inlining −0.42G |
| `87c168f` | Same for Delimited (delimiter + initial element) and AnyNumberOf candidates | −11M (delimiters are far fewer than candidate probes) |
| `f93c38b` | **First-token simple-hint gate for framed Sequence elements** — hint pruning previously only covered OneOf/AnyNumberOf candidate lists, so every absent optional clause slot (WHERE, GROUP BY, …) paid a full Ref+Sequence frame cascade | **−126M**, biggest late-stage win |

Final definitive (fat LTO, n1/n3): **8,995,175,662 Ir/iter = −53.48%**.

## Tried and rejected — do not redo without new evidence

All measured on the same harness; numbers are per-iteration instruction deltas.

- **Frame free-list pool**: only −0.3% instructions despite −5% wall.
  Rejected — the benchmark metric is instructions.
- **Keyword dispatch table for `prune_options`** (per-grammar inverted map
  `raw_upper → candidate indices`, memoized): **+21M** with no fanout
  threshold, **+8M** at fanout ≥ 12, **+13M** at ≥ 24. The LTO-compiled
  linear hint scan (a few loads + short string compares per option) beats a
  string hash + map probe + sort/dedup + re-collect at the fanouts this
  grammar actually has.
- **Two-token hints** (for STRICT keyword-led Sequence candidates in
  OneOf/AnyNumberOf, check the second required element's hint one code token
  ahead before framing): **+3.2M**. First-token pruning already filters
  almost everything in TPC-DS; "first keyword matches but second doesn't" is
  too rare to pay for the per-candidate check.
- **Dropping/re-keying the frame cache**: deliberately not attempted
  (excluded by request), still unexplored.

## Where the remaining ~9.0G/iter goes (symbolized callgrind)

- malloc/free family ~1.5G; memcpy ~0.65G (top caller: `TableParseFrame`
  Box allocations); CPython bytecode eval ~1.3G; parser main loop self only
  ~120M. Match attempts and pruning volumes are already minimal
  (~22.9k attempts / 93.4k options pruned per 4 queries, unchanged by the
  inlining work — the frames were the cost, not the matching).
- Most promising next levers, in rough order: allocator-level work
  (mimalloc, or arena/pooling for frames and `MatchResult`s — but see the
  free-list result above), remaining Python-side tree construction, PGO.

## Invariants and gotchas (violating these broke parity or tests)

- Lexer fast path must build source slices with an explicit step
  (`slice(a, b, 1)`) to compare equal to the slices from the per-token
  getter path.
- The Jinja template-free fast path must exclude empty input
  (`test__linter__empty_file`): the traced path yields no slices for an
  empty file while the default `TemplatedFile` synthesizes one.
- Direct-call inline evaluation (`try_terminal_inline` + calling the
  `handle_*_waiting_for_child` handler directly) must keep recursion
  bounded: only inline chains that terminate in a frame push. In Delimited,
  never inline BOTH the element and the delimiter in the repetition loop —
  that recurses once per list item with no unwind.
- `try_terminal_inline` contract: on `Some`, parser pos is advanced past
  the match on success or left at the candidate position on failure,
  exactly like the frame handlers; terminal variants are never
  frame-cached, so no cache semantics are lost.
- Hint-gate soundness: a simple hint is a *necessary* condition, so
  hint-miss ⇒ empty match is safe for any grammar. But second-token
  reasoning is only sound for STRICT sequences (greedy modes return
  partial/unparsable matches after a first-element match).
- PyO3 `IntoPyObject` tuples cap at ~13 elements; build larger tuples via
  `[Py<PyAny>; N]` + `PyTuple::new`.
- `test/core/plugin_test.py` has 3 failures unless
  `plugins/sqlfluff-plugin-example` is pip-installed — pre-existing
  environment issue, unrelated to parser changes.
- `maturin build` must run from `sqlfluffrs/` (the `pyproject.toml` there
  carries the required `features = [..., "python"]`); passing
  `-m sqlfluffrs_python/Cargo.toml` from elsewhere fails feature resolution.
