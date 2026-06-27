/// TPC-H and TPC-DS lex and parse benchmarks using iai-callgrind.
///
/// Each benchmark runs a single query selected by an environment variable:
///
///   TPCH_QUERY_IDX=5  (1-based, default 13)
///   TPCDS_QUERY_IDX=7 (1-based, default 1)
///
/// Run all TPC benchmarks (the `fetch` feature downloads the fixtures):
///   cargo bench -p sqlfluffrs_benchmarks --features fetch --bench tpc_bench
///
/// Run only TPC-H lex:
///   cargo bench -p sqlfluffrs_benchmarks --bench tpc_bench -- lex_tpch
use std::fs;
use std::hint::black_box;
use std::path::Path;

use iai_callgrind::{library_benchmark, library_benchmark_group, main};
use sqlfluffrs_benchmarks::tpc_fixture;
use sqlfluffrs_dialects::dialect::ansi::matcher::ANSI_LEXERS;
use sqlfluffrs_dialects::Dialect;
use sqlfluffrs_lexer::{LexInput, Lexer};
use sqlfluffrs_parser::parser::Parser;
use sqlfluffrs_types::Token;

// Default query indices (1-based) matching Python defaults:
//   TPC-H:  random.Random(0).randrange(22) → 12 (0-based) → Q13
//   TPC-DS: Q1 — one of the shortest queries.
const DEFAULT_TPCH_Q: u32 = 13;
const DEFAULT_TPCDS_Q: u32 = 1;

fn query_idx(env_var: &str, default: u32) -> u32 {
    std::env::var(env_var)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn read_file(path: &Path) -> String {
    fs::read_to_string(path).unwrap_or_else(|e| panic!("Failed to read {}: {}", path.display(), e))
}

fn lex_sql(sql: &str) -> Vec<Token> {
    let (tokens, _) =
        Lexer::new(None, ANSI_LEXERS.to_vec()).lex(LexInput::String(sql.to_owned()), false);
    tokens
}

fn parse_tokens(tokens: &[Token]) {
    let mut parser = Parser::new(
        std::hint::black_box(tokens),
        Dialect::Ansi,
        hashbrown::HashMap::new(),
    );
    std::hint::black_box(parser.call_rule_as_root().expect("Parse failed"));
}

// Setup functions — run outside Valgrind instrumentation.
fn tpch_sql() -> String {
    let q = query_idx("TPCH_QUERY_IDX", DEFAULT_TPCH_Q);
    read_file(&tpc_fixture("tpc-h", q))
}
fn tpch_tokens() -> Vec<Token> {
    lex_sql(&tpch_sql())
}
fn tpcds_sql() -> String {
    let q = query_idx("TPCDS_QUERY_IDX", DEFAULT_TPCDS_Q);
    read_file(&tpc_fixture("tpc-ds", q))
}
fn tpcds_tokens() -> Vec<Token> {
    lex_sql(&tpcds_sql())
}

// Benchmark functions — bodies run inside Valgrind instrumentation.
#[library_benchmark]
#[bench::q(tpch_sql())]
fn bench_lex_tpch(sql: String) {
    black_box(lex_sql(&sql));
}

#[library_benchmark]
#[bench::q(tpch_tokens())]
fn bench_parse_tpch(tokens: Vec<Token>) {
    parse_tokens(black_box(&tokens));
}

#[library_benchmark]
#[bench::q(tpcds_sql())]
fn bench_lex_tpcds(sql: String) {
    black_box(lex_sql(&sql));
}

#[library_benchmark]
#[bench::q(tpcds_tokens())]
fn bench_parse_tpcds(tokens: Vec<Token>) {
    parse_tokens(black_box(&tokens));
}

library_benchmark_group!(name = lex_tpch; benchmarks = bench_lex_tpch);
library_benchmark_group!(name = parse_tpch; benchmarks = bench_parse_tpch);
library_benchmark_group!(name = lex_tpcds; benchmarks = bench_lex_tpcds);
library_benchmark_group!(name = parse_tpcds; benchmarks = bench_parse_tpcds);

main!(
    library_benchmark_groups = lex_tpch,
    parse_tpch,
    lex_tpcds,
    parse_tpcds
);
