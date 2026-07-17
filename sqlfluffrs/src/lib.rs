#[cfg(feature = "python")]
pub mod python;
pub mod test_harness;

// Route every allocation in the extension module through mimalloc. The parser
// allocates a Box<TableParseFrame> per grammar frame and an Arc<MatchResult>
// per match, so alloc/free traffic dominates the parse profile; mimalloc's
// segregated free-list services these fixed-size blocks in far fewer
// instructions than glibc's _int_malloc/_int_free. See PERF_LOG.md.
//
// Only installed for the built extension (the `python` cdylib), which is what
// the CodSpeed benchmark and shipped wheel exercise. The workspace's library
// crates keep the system allocator so their unit tests are unaffected.
#[cfg(feature = "python")]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;
