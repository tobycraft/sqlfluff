use std::fmt::Display;

use fancy_regex::{Regex as FancyRegex, RegexBuilder as FancyRegexBuilder};
use hashbrown::HashSet;
use regex::{Regex, RegexBuilder};

use crate::{token::CaseFold, PositionMarker, RegexModeGroup, Token, TokenConfig};

// use sqlfluffrs_dialects::Dialect;

/// Function pointer type for token generation: one of `Token::{kind}_token`.
pub type TokenGenerator = fn(String, PositionMarker, TokenConfig) -> Token;

#[derive(Debug, Clone)]
pub enum LexerMode {
    String(String),                           // Match a literal string
    Regex(Regex, fn(&str) -> bool),           // Match using a regex
    FancyRegex(FancyRegex, fn(&str) -> bool), // Match using a regex
    Function(fn(&str) -> Option<&str>),
}

impl Display for LexerMode {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match *self {
            LexerMode::Regex(_, _) => write!(f, "RegexMatcher"),
            LexerMode::FancyRegex(_, _) => write!(f, "FancyRegexMatcher"),
            LexerMode::String(_) => write!(f, "StringMatcher"),
            LexerMode::Function(_) => write!(f, "FunctionMatcher"),
        }
    }
}

pub struct LexedElement<'a> {
    pub raw: &'a str,
    pub matcher: &'a LexMatcher,
}

impl<'a> LexedElement<'a> {
    pub fn new(raw: &'a str, matcher: &'a LexMatcher) -> Self {
        Self { raw, matcher }
    }
}

#[derive(Debug, Clone)]
pub struct LexMatcher {
    // pub dialect: Dialect,
    pub name: String,
    pub mode: LexerMode,
    pub token_class_func: TokenGenerator,
    pub subdivider: Option<Box<LexMatcher>>,
    pub trim_post_subdivide: Option<Box<LexMatcher>>,
    pub trim_start: Option<Vec<String>>,
    pub trim_chars: Option<Vec<String>>,
    pub quoted_value: Option<(String, RegexModeGroup)>,
    pub escape_replacements: Option<(String, String)>,
    pub casefold: CaseFold,
    pub kwarg_type: Option<String>,
    /// See [`FirstByteSet`]; computed once at construction.
    pub first_bytes: FirstByteSet,
}

/// Grouped optional parameters shared by [`LexMatcher::string_lexer`],
/// [`LexMatcher::regex_lexer`], and [`LexMatcher::regex_subdivider`].
#[derive(Debug, Clone, Default)]
pub struct LexMatcherConfig {
    pub subdivider: Option<Box<LexMatcher>>,
    pub trim_post_subdivide: Option<Box<LexMatcher>>,
    pub trim_start: Option<Vec<String>>,
    pub trim_chars: Option<Vec<String>>,
    pub quoted_value: Option<(String, RegexModeGroup)>,
    pub escape_replacements: Option<(String, String)>,
    pub casefold: CaseFold,
    pub kwarg_type: Option<String>,
}

/// 256-bit set of bytes a [`LexMatcher`] could possibly match at the start
/// of its input. Used as a cheap gate in [`LexMatcher::scan_match_into`]
/// before touching the regex engine: the matcher walk visits every matcher
/// in order for every token position, and most matchers cannot possibly
/// match at most positions (a comma matcher at the start of a keyword, the
/// whitespace regex at the start of an identifier, ...).
///
/// The set is a conservative OVER-approximation - a byte being present only
/// means the matcher must be consulted, never that it will match - so
/// gating on it cannot change which matcher wins.
#[derive(Debug, Clone, Copy)]
pub struct FirstByteSet([u64; 4]);

impl FirstByteSet {
    /// Every byte possible: used whenever the pattern cannot be analysed.
    const ALL: FirstByteSet = FirstByteSet([u64::MAX; 4]);

    fn empty() -> Self {
        FirstByteSet([0; 4])
    }

    fn insert(&mut self, b: u8) {
        self.0[(b >> 6) as usize] |= 1u64 << (b & 63);
    }

    fn insert_range(&mut self, lo: u8, hi: u8) {
        for b in lo..=hi {
            self.insert(b);
        }
    }

    #[inline]
    pub fn contains(&self, b: u8) -> bool {
        self.0[(b >> 6) as usize] & (1u64 << (b & 63)) != 0
    }

    /// Derive the set for a regex pattern, or `ALL` when the pattern cannot
    /// be parsed (e.g. fancy-regex syntax) or could match the empty string.
    fn for_regex_pattern(pattern: &str) -> Self {
        let Ok(hir) = regex_syntax::ParserBuilder::new().build().parse(pattern) else {
            return Self::ALL;
        };
        let mut set = Self::empty();
        if Self::collect_hir(&hir, &mut set) {
            // The whole pattern can match empty: any byte may follow.
            return Self::ALL;
        }
        set
    }

    /// Add the possible first bytes of `hir` to `set`; returns whether `hir`
    /// can match the empty string (so a following concat element's first
    /// bytes are also reachable).
    fn collect_hir(hir: &regex_syntax::hir::Hir, set: &mut FirstByteSet) -> bool {
        use regex_syntax::hir::{Class, HirKind};
        match hir.kind() {
            HirKind::Empty => true,
            HirKind::Literal(lit) => {
                if let Some(&b) = lit.0.first() {
                    set.insert(b);
                    false
                } else {
                    true
                }
            }
            HirKind::Class(Class::Unicode(cls)) => {
                for range in cls.ranges() {
                    let lo = range.start() as u32;
                    let hi = range.end() as u32;
                    if lo <= 0x7F {
                        set.insert_range(lo as u8, hi.min(0x7F) as u8);
                    }
                    if hi > 0x7F {
                        // Any non-ASCII char: over-approximate with every
                        // possible UTF-8 lead byte.
                        set.insert_range(0x80, 0xFF);
                    }
                }
                false
            }
            HirKind::Class(Class::Bytes(cls)) => {
                for range in cls.ranges() {
                    set.insert_range(range.start(), range.end());
                }
                false
            }
            HirKind::Look(_) => true,
            HirKind::Repetition(rep) => {
                let sub_empty = Self::collect_hir(&rep.sub, set);
                rep.min == 0 || sub_empty
            }
            HirKind::Capture(cap) => Self::collect_hir(&cap.sub, set),
            HirKind::Concat(items) => {
                for item in items {
                    if !Self::collect_hir(item, set) {
                        return false;
                    }
                }
                true
            }
            HirKind::Alternation(items) => {
                let mut any_empty = false;
                for item in items {
                    any_empty |= Self::collect_hir(item, set);
                }
                any_empty
            }
        }
    }
}

impl Display for LexMatcher {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "<{}: {}>", self.mode, self.name)
    }
}

impl LexMatcher {
    pub fn string_lexer(
        // dialect: Dialect,
        name: &str,
        template: &str,
        token_class_func: TokenGenerator,
        config: LexMatcherConfig,
    ) -> Self {
        let first_bytes = {
            let mut set = FirstByteSet::empty();
            match template.as_bytes().first() {
                Some(&b) => set.insert(b),
                None => set = FirstByteSet::ALL,
            }
            set
        };
        Self {
            // dialect,
            name: name.to_string(),
            mode: LexerMode::String(template.to_string()),
            token_class_func,
            subdivider: config.subdivider,
            trim_post_subdivide: config.trim_post_subdivide,
            trim_start: config.trim_start,
            trim_chars: config.trim_chars,
            quoted_value: config.quoted_value,
            escape_replacements: config.escape_replacements,
            casefold: config.casefold,
            kwarg_type: config.kwarg_type,
            first_bytes,
        }
    }

    fn base_regex_lexer(
        // dialect: Dialect,
        name: &str,
        pattern: &str,
        token_class_func: TokenGenerator,
        fallback_lexer: Option<fn(&str) -> Option<&str>>,
        precheck: fn(&str) -> bool,
        config: LexMatcherConfig,
    ) -> Self {
        let mode = match RegexBuilder::new(pattern).build() {
            Ok(regex) => LexerMode::Regex(regex, precheck),
            Err(_) => match FancyRegexBuilder::new(pattern).build() {
                Ok(regex) => LexerMode::FancyRegex(regex, precheck),
                Err(_) => {
                    if let Some(fallback) = fallback_lexer {
                        LexerMode::Function(fallback)
                    } else {
                        panic!(
                            "Unable to compile regex {} and no fallback function provided",
                            pattern
                        )
                    }
                }
            },
        };
        // Function-mode fallbacks can match anything; regex modes get the
        // conservative pattern-derived set (fancy-regex patterns fail
        // regex-syntax parsing and also fall back to ALL inside).
        let first_bytes = match &mode {
            LexerMode::Function(_) => FirstByteSet::ALL,
            _ => FirstByteSet::for_regex_pattern(pattern),
        };

        Self {
            // dialect,
            name: name.to_string(),
            mode,
            token_class_func,
            subdivider: config.subdivider,
            trim_post_subdivide: config.trim_post_subdivide,
            trim_start: config.trim_start,
            trim_chars: config.trim_chars,
            quoted_value: config.quoted_value,
            escape_replacements: config.escape_replacements,
            casefold: config.casefold,
            kwarg_type: config.kwarg_type,
            first_bytes,
        }
    }

    pub fn regex_lexer(
        // dialect: Dialect,
        name: &str,
        template: &str,
        token_class_func: TokenGenerator,
        fallback_lexer: Option<fn(&str) -> Option<&str>>,
        precheck: fn(&str) -> bool,
        config: LexMatcherConfig,
    ) -> Self {
        let pattern = format!(r"(?s)\A(?:{})", template);
        Self::base_regex_lexer(
            // dialect,
            name,
            &pattern,
            token_class_func,
            fallback_lexer,
            precheck,
            config,
        )
    }

    pub fn regex_subdivider(
        // dialect: Dialect,
        name: &str,
        template: &str,
        token_class_func: TokenGenerator,
        fallback_lexer: Option<fn(&str) -> Option<&str>>,
        precheck: fn(&str) -> bool,
        config: LexMatcherConfig,
    ) -> Self {
        let pattern = format!(r"(?:{})", template);
        Self::base_regex_lexer(
            // dialect,
            name,
            &pattern,
            token_class_func,
            fallback_lexer,
            precheck,
            config,
        )
    }

    /// Match at the start of `input`, appending the resulting element(s) to
    /// `out` and returning the matched byte length. Writing into the caller's
    /// buffer avoids a `Vec` allocation per matched token (the common case is
    /// exactly one element - see `subdivide_into`).
    pub fn scan_match_into<'a>(
        &'a self,
        input: &'a str,
        out: &mut Vec<LexedElement<'a>>,
    ) -> Option<usize> {
        if input.is_empty() {
            panic!("Unexpected empty string!");
        }

        // First-byte gate (see FirstByteSet): skip the regex/starts_with
        // machinery entirely when this matcher provably cannot match here.
        if !self.first_bytes.contains(input.as_bytes()[0]) {
            return None;
        }

        // Match based on the mode
        let matched = match &self.mode {
            LexerMode::String(template) => input
                .starts_with(template)
                .then(|| LexedElement::new(template, self)),
            LexerMode::Regex(regex, is_match_valid) => {
                if !(is_match_valid)(input) {
                    return None;
                }
                regex
                    .find(input)
                    .map(|mat| LexedElement::new(mat.as_str(), self))
            }
            LexerMode::FancyRegex(regex, is_match_valid) => {
                if !(is_match_valid)(input) {
                    return None;
                }
                regex
                    .find(input)
                    .ok()
                    .flatten()
                    .map(|mat| LexedElement::new(mat.as_str(), self))
            }
            LexerMode::Function(function) => (function)(input).map(|s| LexedElement::new(s, self)),
        };

        // Handle subdivision and trimming
        if let Some(matched) = matched {
            let len = matched.raw.len();
            self.subdivide_into(matched, out);
            Some(len)
        } else {
            None
        }
    }

    /// Compatibility wrapper over [`Self::scan_match_into`].
    pub fn scan_match<'a>(&'a self, input: &'a str) -> Option<(Vec<LexedElement<'a>>, usize)> {
        let mut out = Vec::new();
        let len = self.scan_match_into(input, &mut out)?;
        Some((out, len))
    }

    fn search(&self, input: &str) -> Option<(usize, usize)> {
        match &self.mode {
            LexerMode::String(template) => input.find(template).map(|start| {
                let end = start + template.len();
                (start, end)
            }),
            LexerMode::Regex(regex, _) => regex.find(input).map(|mat| (mat.start(), mat.end())),
            LexerMode::FancyRegex(regex, _) => regex
                .find(input)
                .ok()
                .flatten()
                .map(|mat| (mat.start(), mat.end())),
            _ => todo!(),
        }
    }

    fn subdivide_into<'a>(&'a self, matched: LexedElement<'a>, out: &mut Vec<LexedElement<'a>>) {
        if let Some(subdivider) = &self.subdivider {
            let mut buffer = matched.raw;
            while !buffer.is_empty() {
                if let Some((start, end)) = subdivider.search(buffer) {
                    self.trim_match_into(&buffer[..start], out);
                    out.push(LexedElement {
                        raw: &buffer[start..end],
                        matcher: subdivider,
                    });
                    buffer = &buffer[end..];
                } else {
                    self.trim_match_into(buffer, out);
                    break;
                }
            }
        } else {
            out.push(matched);
        }
    }

    fn trim_match_into<'a>(&'a self, raw: &'a str, out: &mut Vec<LexedElement<'a>>) {
        let mut buffer = raw;
        let mut content_buffer = 0..0;

        if let Some(trim_post_subdivide) = &self.trim_post_subdivide {
            while !buffer.is_empty() {
                if let Some((start, end)) = trim_post_subdivide.search(buffer) {
                    if start == 0 {
                        // Starting match
                        out.push(LexedElement {
                            raw: &buffer[..end],
                            matcher: trim_post_subdivide,
                        });
                        buffer = &buffer[end..];
                        content_buffer = end..end;
                    } else if end == buffer.len() {
                        out.push(LexedElement {
                            raw: &raw[content_buffer.start..content_buffer.end + start],
                            matcher: self,
                        });
                        out.push(LexedElement {
                            raw: &buffer[start..end],
                            matcher: trim_post_subdivide,
                        });
                        return;
                    } else {
                        content_buffer.end += end;
                        buffer = &buffer[end..];
                    }
                } else {
                    break;
                }
            }
        }
        if !content_buffer.is_empty() || !buffer.is_empty() {
            out.push(LexedElement::new(&raw[content_buffer.start..], self));
        }
    }

    pub fn construct_token(&self, raw: &str, pos_marker: PositionMarker) -> Token {
        let instance_types = match self.kwarg_type.clone() {
            Some(t) => vec![t],
            None => vec![self.name.clone()],
        };
        (self.token_class_func)(
            raw.to_string(),
            pos_marker,
            TokenConfig {
                class_types: HashSet::new(),
                instance_types,
                trim_start: self.trim_start.clone(),
                trim_chars: self.trim_chars.clone(),
                quoted_value: self.quoted_value.clone(),
                escape_replacement: self.escape_replacements.clone(),
                casefold: self.casefold,
            },
        )
    }
}
