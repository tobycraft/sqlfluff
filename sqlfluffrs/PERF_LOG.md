# Parse-performance work log

Record of the optimization campaign on branch
`claude/benchmark-perf-improvement-sschx9` (July 2026) targeting the CodSpeed
benchmark `test/test_codspeed_tpc_parse.py::test_native_ast_tpcds`
(simulation mode = valgrind instruction counts). **Read this before starting
new parser/benchmark performance work** — it lists what was measured to work,
and (equally important) what was measured NOT to work.

**Result: 19,336,856,656 → 8,995,175,662 instructions per iteration (−53.5%)**
across 25 commits, with byte-identical parse trees throughout. A later
allocator pass (mimalloc + two per-frame `Vec` elisions) took this a further
**−12.5%** to **7,867,637,585 Ir/iter** (fat LTO) — cumulative **−59.3%** from
the original 19.34G baseline. See "Allocation pass" below.

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

## Allocation pass (mimalloc + per-frame Vec elision)

A follow-up campaign driven by `valgrind --tool=dhat` (heap profiling) rather
than callgrind. DHAT on the native-AST TPC-DS pass showed ~7.9M allocations
totalling ~1.96 GB, dominated by fixed-size churn: `Box<TableParseFrame>`
(1.37M blocks / 514 MB — one per grammar frame) and `Arc<MatchResult>` (~1.5M
blocks — one per match). The matching callgrind baseline attributed ~1.71G
Ir/iter (≈17% of the total) to the glibc malloc/free family alone
(`_int_malloc` 0.44G, `_int_free` 0.46G, `malloc`/`free`/`malloc_consolidate`
the rest).

| Change | Effect |
|---|---|
| **mimalloc as the extension's `#[global_allocator]`** (`sqlfluffrs/Cargo.toml` + `src/lib.rs`, gated on the `python` feature) — services the parser's fixed-size `Box`/`Arc` blocks from a segregated free-list instead of glibc's bin-management path | the bulk of the −12.3% |
| **`handle_sequence_initial`: borrow children via `children_ids_slice()`** instead of `children(..).collect::<Vec>()` — the same static-table slice the WaitingForChild handler already uses (DHAT: ~308k allocs/pass) | folded into the pass total |
| **`handle_sequence_child_success`: `extend(child.child_matches.iter().cloned())`** instead of `extend(child.child_matches.clone())`, and `.iter().copied()` for the Copy insert tuples — drops the throwaway intermediate `Vec` (DHAT: ~190k allocs/pass) | folded into the pass total |

Definitive (committed fat LTO + codegen-units=1, n1/n3):
**8,995,175,662 → 7,867,637,585 Ir/iter = −12.53%** (−1,127,538,077). The
thin-LTO build used for iteration measured the same swing independently
(9,013,832,742 → 7,901,134,601 = −12.34%). Parity gate: byte-identical
stringify + raws + position markers + `as_record()` + violations digests
across all 121 TPC-H/TPC-DS fixtures, on both builds. Almost all of the win is
the allocator swap — the two Vec elisions are small on their own but remove
real per-frame allocations that compound across the ~23k frames a 4-query pass
builds.

Notes/gotchas from this pass:
- **DHAT cannot measure the *post*-mimalloc build**: DHAT intercepts
  `malloc`/`free`, but mimalloc satisfies allocations from `mmap` directly, so
  a DHAT run on the mimalloc build reports almost no heap traffic. Use
  callgrind (instruction count — the CodSpeed metric) for the before/after,
  and DHAT only on a *glibc* build to find allocation *sites*.
- mimalloc's one-time heap init lands in both n1 and n3, so it cancels out of
  `(Ir(n3) − Ir(n1)) / 2`; the per-iteration figure is clean.
- `children_ids_slice()` returns `&'a [GrammarId]` tied to the grammar tables,
  not to `&self`, so it stays live across the `&mut self` calls in the handler
  — the borrow the Sequence WaitingForChild path already depended on.
- The allocator is gated on the `python` feature so a plain
  `cargo build --workspace` (CI, unit tests) keeps the system allocator.

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
- Most promising next levers, in rough order: ~~allocator-level work
  (mimalloc, …)~~ **done — see "Allocation pass" above (−12.3%)**; remaining
  per-frame `Vec`/`SmallVec` churn (`local_terminators` still collects in the
  OneOf/Ref/Delimited/AnyNumberOf initials, and the AnyNumberOf
  `option_counter` HashMap — all now cheap under mimalloc but still real
  allocations); remaining Python-side tree construction; PGO.

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

## Wall-time comparison: merge-base vs. each split-out branch (July 2026)

The commits above were split out of the single `perf/additional-improvements`
branch into independent, per-idea branches (one dependency-stack per branch;
see each branch's own commit(s) for the individual rationale), rebased onto
`sqlfluff/sqlfluff@main` (`589b1fb`, "Don't hoist a subquery correlated in a
later set expression branch (#8169)") — this is the `merge-base` row below.
This section measures **wall time**, not instructions: CodSpeed's
simulation-mode instruction counts (above) are the comparable-across-runs
metric; wall time is included here because it's what the split-out branches
need reviewed against for a real merge decision.

**Methodology**:
- Each branch was built with `maturin develop --release -F python` in its own
  git worktree (release profile; fat LTO only for `fat-lto-codegen-units`,
  thin/default elsewhere) and benchmarked in isolation — one Python venv,
  builds done sequentially, never concurrently.
- 6 configs per branch: {TPC-H, TPC-DS} query suites × {pure-Python parser
  (`use_rust_parser=False`), Rust legacy convert+apply path, Rust native-AST
  path (`set_native_ast(True)`)} — mirrors
  `test/test_codspeed_tpc_parse.py`'s 6 benchmarks. One "sample" = one
  `Linter.parse_string` pass over the full query suite (22 TPC-H / 99
  TPC-DS queries), `time.perf_counter()`, GC disabled during timing, one
  uncounted warmup pass first.
- Adaptive sampling in steps of 5: after each batch of 5, stop once the
  **relative standard error of the mean** (`stdev/sqrt(n)/mean`) drops below
  1%, capped at 500 samples. (Raw sample-to-sample CV on this shared/
  virtualized sandbox has an intrinsic noise floor of ~1-5% that does *not*
  shrink with more samples — verified up to n=200 — so it isn't a viable
  stopping criterion here; SEM-of-the-mean does shrink as 1/sqrt(n) under
  that same noise and is what's reported as "converged" below.) All 66
  cells (11 branches × 6 configs) converged, mostly at n=5, worst case
  n=20.
- TPC-H/TPC-DS fixtures: same Apache Doris-sourced queries as
  `sqlfluffrs_benchmarks/build.rs` / the CodSpeed suite (SHA
  `3a2d9d55f1e8e2d74187179ef89c36c8562815fd`).
- `docs/perf-log-campaign` (this file + `AGENTS.md` only) is not benchmarked
  — no code change, so no runtime effect.

**Caveat**: each branch's 6 samples-batches converged to <1% SEM
*within that branch's own benchmark run*, but the 11 runs were done
sequentially over about an hour of wall-clock, on a shared sandbox, so
cross-branch deltas below could still carry some run-to-run environmental
drift (noisy-neighbor load, thermal state) that isn't captured by
within-run SEM. Treat deltas under ~2% as noise; `match-result-apply-
sorted-triggers`'s apparent regression in particular should be re-checked
before drawing conclusions, since a HashMap→sorted-merge rewrite should not
plausibly be slower.

Mean wall time per full-suite pass, `Δ%` vs. merge-base (negative = faster):

| branch | python tpch | python tpcds | rust-legacy tpch | rust-legacy tpcds | native-ast tpch | native-ast tpcds |
|---|---|---|---|---|---|---|
| merge-base (`589b1fb`) | 1703ms | 18360ms | 321ms | 3100ms | 342ms | 3145ms |
| native-ast-hotpath-fusion (`575d90d`) | −2.1% | +0.6% | −1.5% | −1.5% | **−13.0%** | **−16.1%** |
| lexer-segment-construction-fastpath (`353798a`) | −0.1% | −0.9% | −4.0% | **−7.8%** | **−11.2%** | **−8.5%** |
| jinja-skip-positionmarker-slots (`13c3f14`) | +6.3% | +1.1% | **−15.8%** | −4.6% | **−16.3%** | **−10.9%** |
| frame-cache-key (`49d18f1`, incl. box-parse-frame prereq) | +0.3% | +0.2% | **−16.6%** | **−11.3%** | **−16.3%** | **−11.2%** |
| match-result-apply-sorted-triggers (`8aef8f7`) | +4.4% | +3.3% | +4.2% | +7.7% | +0.2% | +2.5% |
| raw-segment-skip-normalize (`e27a2ca`) | −0.5% | +1.9% | −2.2% | +3.0% | −4.2% | +2.2% |
| grammar-match-allocations (`4ec24d6`) | +1.5% | +2.7% | −1.1% | +0.2% | −5.4% | −4.8% |
| rawstring-arc (`c01f7eb`) | −1.8% | +2.3% | +0.2% | +2.7% | −2.6% | −1.0% |
| fat-lto-codegen-units (`66d8767`) | −0.8% | +2.4% | +2.6% | −3.0% | −7.5% | −5.5% |
| parse-profile-instrumentation (`7536012`) | −0.8% | +0.8% | −0.5% | +1.1% | −4.2% | +3.2% |

**Reading this**: the pure-Python parser path is flat everywhere (expected —
none of these branches touch `src/sqlfluff/core/parser/*` code paths used
only when `use_rust_parser=False`, beyond the shared `jinja.py`/`markers.py`
edits in `jinja-skip-positionmarker-slots`, which nets out near zero there
too). The four branches with clear, consistent wins across *both* rust
paths and *both* suites — `native-ast-hotpath-fusion`, `lexer-segment-
construction-fastpath`, `jinja-skip-positionmarker-slots`, and `frame-
cache-key` — are the strongest wall-time candidates; `frame-cache-key` and
`jinja-skip-positionmarker-slots` show large legacy-path wins too, not just
native-AST. `grammar-match-allocations`, `rawstring-arc`, and `fat-lto-
codegen-units` show smaller, native-AST-only improvements, consistent with
being lower-level allocation/build-flag changes rather than hot-path
restructuring. `match-result-apply-sorted-triggers` shows a small but
consistent regression across every config, which contradicts its
instruction-count rationale (HashMap removal) — worth re-benchmarking in
isolation (ideally with `callgrind`, which isn't subject to this sandbox's
wall-clock noise) before trusting either result.

### Combined: all 10 branches merged into one

All 20 commits from the 10 code branches above (i.e. everything except
`docs/perf-log-campaign`, which carries no code) were cherry-picked in their
original chronological order onto `sqlfluff/sqlfluff@main` (`589b1fb`) as
`perf/combined-10`. Applied with only the one already-known conflict (the
`43debb5` `*meta_type`-vs-`.clone()` line drift, resolved identically to its
standalone branch) — no other conflicts across all 20 commits, i.e. the
10 branches are compatible with each other as-is.

Same methodology (adaptive sampling, steps of 5, stop at <1% relative SEM),
same 6 configs, all converged:

| config | merge-base | combined-10 | Δ |
|---|---|---|---|
| python tpch | 1703ms | 1561ms | **−8.4%** |
| python tpcds | 18360ms | 17824ms | **−2.9%** |
| rust-legacy tpch | 321ms | 185ms | **−42.5%** |
| rust-legacy tpcds | 3100ms | 1754ms | **−43.4%** |
| rust-native-ast tpch | 342ms | 154ms | **−55.0%** |
| rust-native-ast tpcds | 3145ms | 1432ms | **−54.5%** |

Both Rust paths are roughly halved — a bigger reduction than either a naive
sum of the individual branches' % deltas (≈−35% for rust-legacy tpch) or a
multiplicative compounding of them (≈−31%) would predict. The likely
explanation: `fat-lto-codegen-units`'s whole-workspace LTO gets to inline
across a hot path that the other 9 branches have already made much smaller/
simpler, so its benefit scales with how much of that path they trimmed —
a real positive interaction between changes, not measurement noise (the
effect size here, 40-55%, dwarfs this sandbox's ~1-5% noise floor).

Notably, the pure-Python parser path improves too (−8.4%/−2.9%), which no
individual branch showed clearly on its own — `jinja-skip-positionmarker-
slots`'s Python-side edits (`jinja.py`, `markers.py`, `keyword.py`,
`fluffconfig.py`) plus the lexer/segment-construction fast paths
(`lexer-segment-construction-fastpath`) touch code the pure-Python parser
also runs through. Individually each looked like noise (±1-6%); stacked,
the signal is clear.
