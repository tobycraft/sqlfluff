use super::Token;
use std::fmt::Display;

/// Render a string the way Python's `repr()` renders a `str`.
///
/// PYTHON PARITY: token Displays surface verbatim in user-visible
/// UnparsableSegment "Expected: ... Found <...>" messages, where Python
/// formats the found segment as `<{Class}: ({pos}) {raw!r}>`. Python's repr
/// prefers single quotes, switches to double quotes when the content contains
/// a single quote (and no double quote), escapes the backslash / the chosen
/// quote / \n \r \t, hex-escapes other control characters, and leaves
/// printable unicode as-is - none of which matches Rust's escape_debug.
fn py_repr(s: &str) -> String {
    let has_single = s.contains('\'');
    let has_double = s.contains('"');
    let quote = if has_single && !has_double { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || (c as u32) == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

impl Display for Token {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "<{}: ({}) {}>",
            self.class_name.clone(),
            self.pos_marker.clone().expect("PositionMarker unset"),
            py_repr(self.raw.as_str()),
        )
    }
}
