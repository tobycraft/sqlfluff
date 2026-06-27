use std::fs;
use std::hint::black_box;

use iai_callgrind::{library_benchmark, library_benchmark_group, main};
use sqlfluffrs_dialects::dialect::ansi::matcher::ANSI_LEXERS;
use sqlfluffrs_dialects::Dialect;
use sqlfluffrs_lexer::{LexInput, Lexer};
use sqlfluffrs_parser::parser::Parser;
use sqlfluffrs_types::Token;

fn lex_sql(sql: &str) -> Vec<Token> {
    let (tokens, _) =
        Lexer::new(None, ANSI_LEXERS.to_vec()).lex(LexInput::String(sql.to_owned()), false);
    tokens
}

fn fixture_path(filename: &str) -> std::path::PathBuf {
    let mut path = std::path::PathBuf::from("../test/fixtures/dialects/ansi/");
    path.push(filename);
    path
}

// Setup functions — run outside Valgrind instrumentation.
fn tokens_simple_select() -> Vec<Token> {
    lex_sql("SELECT a, b FROM foo WHERE c = 1")
}
fn tokens_nested_functions() -> Vec<Token> {
    lex_sql("SELECT CONCAT(UPPER(name), LOWER(SUBSTRING(description, 1, 10))) FROM users")
}
fn tokens_long_query() -> Vec<Token> {
    lex_sql("SELECT * FROM foo WHERE bar IN (SELECT baz FROM qux WHERE quux > 10)")
}
fn tokens_many_columns() -> Vec<Token> {
    lex_sql("SELECT col1, col2, col3, col4, col5, col6, col7, col8, col9, col10 FROM big_table")
}
fn tokens_complex_joins() -> Vec<Token> {
    lex_sql("SELECT a FROM t1 JOIN t2 ON t1.id = t2.id LEFT JOIN t3 ON t2.ref = t3.ref")
}
fn tokens_expression_recursion() -> Vec<Token> {
    let path = fixture_path("expression_recursion.sql");
    let sql =
        fs::read_to_string(&path).unwrap_or_else(|_| panic!("Failed to read {}", path.display()));
    lex_sql(&sql)
}
fn tokens_expression_recursion_2() -> Vec<Token> {
    let path = fixture_path("expression_recursion_2.sql");
    let sql =
        fs::read_to_string(&path).unwrap_or_else(|_| panic!("Failed to read {}", path.display()));
    lex_sql(&sql)
}

// Benchmark function — body runs inside Valgrind instrumentation.
#[library_benchmark]
#[bench::simple_select(tokens_simple_select())]
#[bench::nested_functions(tokens_nested_functions())]
#[bench::long_query(tokens_long_query())]
#[bench::many_columns(tokens_many_columns())]
#[bench::complex_joins(tokens_complex_joins())]
#[bench::expression_recursion(tokens_expression_recursion())]
#[bench::expression_recursion_2(tokens_expression_recursion_2())]
fn bench_parse(tokens: Vec<Token>) {
    let mut parser = Parser::new(black_box(&tokens), Dialect::Ansi, hashbrown::HashMap::new());
    black_box(parser.call_rule_as_root().expect("Parse failed"));
}

library_benchmark_group!(
    name = parse_group;
    benchmarks = bench_parse
);

main!(library_benchmark_groups = parse_group);
