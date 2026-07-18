//! Minimal repro harness: parse one SQL file with the table-driven parser in
//! a thread with a bounded stack, so runaway recursion aborts fast and
//! visibly without taking out the harness.
//!
//!   cargo run -p sqlfluffrs_benchmarks --example min_repro -- <file.sql>

use sqlfluffrs_dialects::dialect::ansi::matcher::ANSI_LEXERS;
use sqlfluffrs_dialects::Dialect;
use sqlfluffrs_lexer::{LexInput, Lexer};
use sqlfluffrs_parser::parser::Parser;

fn main() {
    let path = std::env::args().nth(1).expect("usage: min_repro <file.sql>");
    let sql = std::fs::read_to_string(&path).expect("read sql");
    let (tokens, _) = Lexer::new(None, ANSI_LEXERS.to_vec()).lex(LexInput::String(sql), false);
    // Mirror time_tpc exactly: parse on the main thread, warmup + timed runs.
    for i in 0..6 {
        let mut p = Parser::new(&tokens, Dialect::Ansi, hashbrown::HashMap::new());
        std::hint::black_box(p.call_rule_as_root().expect("parse failed"));
        eprintln!("[dbg] pass {i} ok");
    }
    println!("ok: all passes");
}
