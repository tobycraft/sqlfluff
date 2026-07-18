//! Lex-only benchmark over the TPC fixtures: times N passes of the pure-Rust
//! lexer (no parsing, no Python), printing per-suite totals.
//!
//!   cargo run -p sqlfluffrs_benchmarks --example lex_bench --release [-- N]

use std::time::Instant;

use sqlfluffrs_benchmarks::{tpc_fixture, TPCDS_N, TPCH_N};
use sqlfluffrs_dialects::dialect::ansi::matcher::ANSI_LEXERS;
use sqlfluffrs_lexer::{LexInput, Lexer};

fn main() {
    let n: usize = std::env::args()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(20);
    let mut suites = Vec::new();
    for (sub, count) in [("tpc-h", TPCH_N), ("tpc-ds", TPCDS_N)] {
        let sqls: Vec<String> = (1..=count)
            .map(|i| std::fs::read_to_string(tpc_fixture(sub, i)).expect("fixture"))
            .collect();
        suites.push((sub, sqls));
    }
    // Warmup
    let mut tokens_total = 0usize;
    for (_, sqls) in &suites {
        for sql in sqls {
            let (tokens, _) =
                Lexer::new(None, std::borrow::Cow::Borrowed(ANSI_LEXERS.as_slice())).lex(LexInput::String(sql.clone()), false);
            tokens_total += tokens.len();
        }
    }
    println!("tokens per pass: {tokens_total}");
    for (name, sqls) in &suites {
        let t0 = Instant::now();
        for _ in 0..n {
            for sql in sqls {
                let (tokens, _) = Lexer::new(None, std::borrow::Cow::Borrowed(ANSI_LEXERS.as_slice()))
                    .lex(LexInput::String(sql.clone()), false);
                std::hint::black_box(tokens);
            }
        }
        let el = t0.elapsed().as_secs_f64() * 1000.0 / n as f64;
        println!("{name}: {el:.2}ms per suite pass");
    }
}
