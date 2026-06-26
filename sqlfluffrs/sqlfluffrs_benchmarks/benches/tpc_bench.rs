/// TPC-H and TPC-DS lex and parse benchmarks.
///
/// Query fixtures are downloaded at build time (not committed); see this
/// crate's README. For lex benchmarks, SQL strings are loaded once outside the
/// timed loop and lexed inside it. For parse benchmarks, tokens are produced
/// once outside the timed loop so only parse time is measured.
///
/// Each benchmark runs a single query to keep Valgrind callgrind output within
/// CodSpeed's memory limits.  The query is chosen by an environment variable
/// so any query can be measured without recompiling:
///
///   TPCH_QUERY_IDX=5  (1-based, default 13)
///   TPCDS_QUERY_IDX=7 (1-based, default 50)
///
/// Run all TPC benchmarks (the `fetch` feature downloads the fixtures):
///   cargo bench -p sqlfluffrs_benchmarks --features fetch
///
/// Run only TPC-H:
///   cargo bench -p sqlfluffrs_benchmarks --features fetch -- tpch
use codspeed_criterion_compat::{criterion_group, criterion_main, Criterion};

use sqlfluffrs_benchmarks::tpc_fixture;
use sqlfluffrs_dialects::dialect::ansi::matcher::ANSI_LEXERS;
use sqlfluffrs_dialects::Dialect;
use sqlfluffrs_lexer::{LexInput, Lexer};
use sqlfluffrs_parser::parser::Parser;
use sqlfluffrs_types::Token;
use std::fs;
use std::path::Path;
use std::time::Duration;

// Default query indices (1-based) matching Python defaults:
//   random.Random(0).randrange(22) → 12 (0-based) → Q13
//   random.Random(0).randrange(99) → 49 (0-based) → Q50
const DEFAULT_TPCH_Q: u32 = 13;
const DEFAULT_TPCDS_Q: u32 = 50;

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

fn query_idx(env_var: &str, default: u32) -> u32 {
    std::env::var(env_var)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn bench_tpch_lex(c: &mut Criterion) {
    let q = query_idx("TPCH_QUERY_IDX", DEFAULT_TPCH_Q);
    let sql = read_file(&tpc_fixture("tpc-h", q));

    let mut group = c.benchmark_group("tpch");
    group.sample_size(30).warm_up_time(Duration::from_secs(3));
    group.bench_function("lex_tpch", |b| {
        b.iter(|| std::hint::black_box(lex_sql(&sql)))
    });
    group.finish();
}

fn bench_tpch_parse(c: &mut Criterion) {
    let q = query_idx("TPCH_QUERY_IDX", DEFAULT_TPCH_Q);
    let tokens = lex_sql(&read_file(&tpc_fixture("tpc-h", q)));

    let mut group = c.benchmark_group("tpch");
    group.sample_size(30).warm_up_time(Duration::from_secs(3));
    group.bench_function("parse_tpch", |b| b.iter(|| parse_tokens(&tokens)));
    group.finish();
}

fn bench_tpcds_lex(c: &mut Criterion) {
    let q = query_idx("TPCDS_QUERY_IDX", DEFAULT_TPCDS_Q);
    let sql = read_file(&tpc_fixture("tpc-ds", q));

    let mut group = c.benchmark_group("tpcds");
    group.sample_size(30).warm_up_time(Duration::from_secs(3));
    group.bench_function("lex_tpcds", |b| {
        b.iter(|| std::hint::black_box(lex_sql(&sql)))
    });
    group.finish();
}

fn bench_tpcds_parse(c: &mut Criterion) {
    let q = query_idx("TPCDS_QUERY_IDX", DEFAULT_TPCDS_Q);
    let tokens = lex_sql(&read_file(&tpc_fixture("tpc-ds", q)));

    let mut group = c.benchmark_group("tpcds");
    group.sample_size(30).warm_up_time(Duration::from_secs(3));
    group.bench_function("parse_tpcds", |b| b.iter(|| parse_tokens(&tokens)));
    group.finish();
}

criterion_group!(tpch_benches, bench_tpch_lex, bench_tpch_parse);
criterion_group!(tpcds_benches, bench_tpcds_lex, bench_tpcds_parse);
criterion_main!(tpch_benches, tpcds_benches);
