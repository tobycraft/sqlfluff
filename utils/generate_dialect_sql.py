"""Generate SQL text by walking a dialect's grammar objects.

Starting from a named segment or grammar (e.g. ``SelectStatementSegment``),
walks the ``Sequence``/``OneOf``/``AnySetOf``/``Bracketed``/``Delimited``/``Ref``
grammar objects sqlfluff builds in memory for a dialect and renders concrete SQL
text: one "baseline" example, plus one variant per optional element (with just
that one added back in) and one variant per branch of a
``OneOf``/``AnySetOf``/``Delimited`` (with that branch substituted in).

Every point where the grammar offers a choice - which branch of a
``OneOf``/``AnySetOf``/``Delimited`` to take, which ``MultiStringParser``
keyword to use (e.g. a bare-function set like ``CURRENT_DATE``/
``CURRENT_TIME``/...), how many items a ``Delimited`` renders (1, 2, 3, or a
trailing-delimiter variant where the grammar allows one - bare
``AnyNumberOf``/``AnySetOf`` are deliberately excluded, since their options
are usually heterogeneous siblings rather than a repeatable homogeneous list),
and which terminal vocabulary value to use for an unresolved identifier/
literal - is decided **randomly**, not by a heuristic. Each decision is made
once per decision point, the first time it's encountered in a given
``generate()`` call, then remembered for the rest of that call (see
``_WalkState._choose``): the discovery/combination machinery below depends on
a decision point rendering consistently across the many separate walks one
``generate()`` call makes, so "random" means "roll once per point, not once
per render." Pass ``--seed`` for reproducible output; omit it for a fresh
random seed each run (printed to stderr so a specific run can be reproduced
later). Whichever option isn't picked at a given point still registers as an
ordinary candidate in the same registry optional-elements and branches
already use, so it's still discovered and combined by the machinery below -
nothing is less reachable for not being the random default.

Optional elements are still omitted by default (unaffected by the random
choices above - that's a separate, always-deterministic mechanism), so the
baseline is minimal in that sense. But by default only the optional elements
and branches reachable *while rendering that one baseline walk* are ever
discovered - anything nested inside an optional that's omitted by default, or
a branch that wasn't the baseline's random pick, is invisible. The
``coverage`` parameter (0-100, default 0) trades runtime for looking deeper:
it internally controls two things -

- how many rounds of "render this known candidate, see what new candidates
  turn up nested inside it" to run, so e.g. a column-definition list that's
  itself optional gets rendered at least once, revealing the real column/
  constraint grammar nested inside it, not just an empty ``()``;
- how many candidates get toggled on *simultaneously* in one example, so
  interactions between two optional clauses (not just one at a time against
  the baseline) get exercised too.

``coverage=100`` is "the most thorough setting this tool considers practical,"
not literally exhaustive - the true combinatorial space is exponential, and
``max_examples`` remains a hard cap on total output regardless of ``coverage``.

Terminal matchers that don't carry their own literal text (identifiers, numeric
and quoted literals, ...) are filled from a small fixed vocabulary keyed by the
``Ref`` name that led to them. Recursion is bounded solely by a cycle guard
(a ``Ref`` name already visited on the current path renders as a terminal
instead of being followed again), since dialect grammars are frequently
self-referential (e.g. expressions containing expressions). A dialect has on
the order of a thousand distinct ``Ref`` names, an absolute upper bound on
how deep any single path can recurse before the guard fires, so no separate
numeric depth cap is needed - confirmed empirically to stay fast (a full
28-dialect sweep completes in ~2s) even without one. An earlier numeric depth
cap was removed after it caused a subtle bug: a completely resolvable
``Ref`` (e.g. a one-character ``.`` literal reached deep inside a
self-referential expression chain) would render as a generic fallback value
purely because it was encountered past the cap, not because it was actually
unresolvable.

This only produces SQL text - it does not check that text against anything.
That is a separate, later step.
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field
from typing import Optional

from sqlfluff.core import Linter
from sqlfluff.core.dialects import dialect_selector
from sqlfluff.core.dialects.base import Dialect
from sqlfluff.core.parser.grammar.anyof import AnyNumberOf
from sqlfluff.core.parser.grammar.base import Nothing, Ref
from sqlfluff.core.parser.grammar.conditional import Conditional
from sqlfluff.core.parser.grammar.delimited import Delimited
from sqlfluff.core.parser.grammar.sequence import Bracketed, Sequence
from sqlfluff.core.parser.parsers import MultiStringParser, StringParser
from sqlfluff.core.parser.segments import BaseSegment, MetaSegment

# Terminal matchers that don't carry their own literal text, keyed by the
# `Ref` name that led to them. Each entry is a list of representative values,
# not just one - the first is the default (baseline) value, identical to
# every earlier version of this tool; the rest are registered as ordinary
# "value" candidates the same way branch alternates are, so e.g. a second
# NumericLiteralSegment value can get its own generated example. Names
# reached through a `Ref` that don't fit the suffix patterns below.
TERMINAL_VOCAB: dict[str, list[str]] = {
    "ParameterNameSegment": ["foo", "bar"],
    "NumericLiteralSegment": ["1", "-1", "1.5"],
    "QuotedLiteralSegment": ["'a'", "''"],
    "BooleanLiteralGrammar": ["true", "false"],
}

# Every dialect defines its own family of identifier/reference grammar names
# (NakedIdentifierSegment, SingleCSIdentifierGrammar, TableReferenceSegment,
# HiveReferenceIdentifierGrammar, ...) - rather than enumerating each dialect's
# variants by exact name, match by suffix. More specific suffixes are listed
# first since matching stops at the first hit (e.g. "QuotedIdentifierSegment"
# would otherwise also match the plain "IdentifierSegment" entry below it).
# Same list-of-values shape as TERMINAL_VOCAB, same reasoning.
SUFFIX_VOCAB: list[tuple[str, list[str]]] = [
    ("QuotedIdentifierSegment", ['"foo"', '"bar baz"']),
    ("IdentifierSegment", ["foo", "bar_1"]),
    ("IdentifierGrammar", ["foo", "bar_1"]),
    ("ReferenceSegment", ["foo", "bar_1"]),
    ("ReferenceGrammar", ["foo", "bar_1"]),
]

# Anything matching neither TERMINAL_VOCAB nor SUFFIX_VOCAB falls back to
# this and prints a note to stderr, so gaps stay visible instead of silently
# producing bad SQL. Deliberately a single value, not a list - there's
# nothing principled to enumerate for a genuinely unknown gap.
DEFAULT_FALLBACK = "1"

BRACKET_CHARS = {
    "round": ("(", ")"),
    "square": ("[", "]"),
    "curly": ("{", "}"),
    "angle": ("<", ">"),
}

# `coverage` (0-100) maps onto these two internal knobs. Deliberately coarse -
# there are only MAX_DISCOVERY_DEPTH+1 distinct depth values and
# MAX_COMBINATION_WIDTH distinct width values across the whole 0-100 range, so
# nearby coverage values frequently produce identical output. That's expected,
# not a bug: coverage picks a point on a small, discrete grid, not a
# continuous dial.
MAX_DISCOVERY_DEPTH = 5  # generous - reaching a real column definition inside
# CREATE TABLE's (optional) column list only needed depth 2 in testing.
MAX_COMBINATION_WIDTH = 4  # enough for e.g. WHERE + GROUP BY + ORDER BY +
# LIMIT together, without approaching factorial blowup.

# How many repeated items to consider for a Delimited node (see _can_repeat
# for why this doesn't extend to bare AnyNumberOf/AnySetOf), one axis of the
# random "how many items does the baseline show" choice alongside 1 (no
# repeat) and, where the grammar allows it, a trailing-delimiter variant -
# see _WalkState._choose. Fixed and small - not derived from the grammar's
# own bounds, and not attempting to fully replicate sqlfluff's real matching
# rules. 2 proves "does a second item and its delimiter parse," 3 adds a
# little more confidence without meaningfully increasing cost.
REPEAT_COUNTS = (2, 3)

# Registered unconditionally (not gated behind --coverage), same as "add"
# and "branch" candidates always have been - so, as of this change,
# --coverage 0's default output is richer than earlier versions of this
# tool produced. Disclosed and deliberate: these are exactly as fundamental
# a source of grammar variation as optional elements and branches are.


class GenerationError(Exception):
    """Raised when the requested entry point can't be resolved."""


@dataclass
class _Candidate:
    """One point in the grammar tree where a variant could be generated."""

    index: int
    kind: str  # "add", "branch", "template", "value", or "count"
    payload: object  # informational only - never read back, see call sites
    # Other candidates' global ids that must also be active to reach this one
    # (its ancestors in the optional/branch nesting).
    requires: frozenset[int] = frozenset()


@dataclass
class _WalkState:
    """Threaded through one recursive walk of the grammar tree.

    `known` is a registry shared across *every* walk in one `generate()` call
    (not reset per walk), keyed by an explicit, caller-chosen key (defaulting
    to `(kind, id(payload))` when the payload is a grammar object with stable
    identity - grammar objects are constructed once when a dialect module
    loads and reused for the process's lifetime, so `id()` works there. Kinds
    whose payload is a plain string or int (template/value alternates, repeat
    counts) pass an explicit key built from stable parts instead, since
    string `id()` isn't reliable identity.

    `rng`/`defaults` are also shared across every walk in one `generate()`
    call, the same way `known`/`warned` are: `defaults` memoizes each
    decision point's randomly-chosen default (see `_choose`) the first time
    it's encountered, so the same point renders the same way in every later
    walk of the same call regardless of which candidates are targeted.
    """

    dialect: Dialect
    known: dict[tuple, _Candidate]
    rng: random.Random
    # Vocab-gap names already warned about - shared across every walk in one
    # `generate()` call (like `known`), so each gap prints once per segment
    # rather than once per occurrence.
    warned: set[str] = field(default_factory=set)
    defaults: dict[tuple, object] = field(default_factory=dict)
    target_indices: frozenset[int] = frozenset()
    collecting: bool = False
    new_candidates: list[_Candidate] = field(default_factory=list)

    def _register(
        self, kind: str, payload: object, key: Optional[tuple] = None
    ) -> Optional[_Candidate]:
        """Look up (or, if collecting, create) the candidate for this point."""
        if key is None:
            key = (kind, id(payload))
        candidate = self.known.get(key)
        if candidate is None and self.collecting:
            candidate = _Candidate(
                len(self.known), kind, payload, requires=self.target_indices
            )
            self.known[key] = candidate
            self.new_candidates.append(candidate)
        return candidate

    def _choose(
        self,
        kind: str,
        key: tuple,
        options: list,
        key_fn: Optional[callable] = None,
    ) -> object:
        """This decision point's default: random, memoized for this `generate()` call.

        The first time `key` is seen (in any walk of this call), one option
        is drawn at random and remembered in `defaults` for every later walk.
        Every other option is still registered as an ordinary candidate
        (`key_fn(option)`, or the usual `(kind, id(option))` if omitted), so
        a `--coverage`-targeted walk can still render it instead - random
        only decides what happens when nothing targets an alternative.

        `options` must be in a stable, non-hash-order-dependent sequence
        (callers holding a `set`/`frozenset` should `sorted()` it first) -
        otherwise the same `--seed` could pick a different default on a
        different process (`PYTHONHASHSEED`-dependent iteration order),
        breaking reproducibility.
        """
        if key not in self.defaults:
            self.defaults[key] = self.rng.choice(list(options))
        chosen = self.defaults[key]
        for option in options:
            if option == chosen:
                continue
            candidate = self._register(
                kind, option, key=key_fn(option) if key_fn else None
            )
            if candidate is not None and candidate.index in self.target_indices:
                chosen = option
                break
        return chosen


def _entry_point(name: str, dialect: Dialect):
    """Resolve a --segment name to something _render can walk."""
    try:
        return dialect.ref(name)
    except (ValueError, RuntimeError) as err:
        raise GenerationError(f"Unknown segment/grammar {name!r}: {err}") from err


def _terminal_for(name: Optional[str], state: _WalkState) -> str:
    """Return a representative literal for an unresolved terminal `name`.

    Randomly picks (and memoizes) one of this name's vocab values as the
    default; every other value still registers as an ordinary "value"
    candidate, the same way _render_branch registers branch alternates.
    """
    if not name:
        return DEFAULT_FALLBACK

    values = TERMINAL_VOCAB.get(name)
    if values is None:
        for suffix, suffix_values in SUFFIX_VOCAB:
            if name.endswith(suffix):
                values = suffix_values
                break

    if values is None:
        if name not in state.warned:
            state.warned.add(name)
            print(
                f"[generate_dialect_sql] no vocab entry for {name!r}, "
                f"using fallback {DEFAULT_FALLBACK!r}",
                file=sys.stderr,
            )
        return DEFAULT_FALLBACK

    return state._choose(
        "value", ("value", name), values, key_fn=lambda alt: ("value", name, alt)
    )


def _render(
    matchable: object,
    dialect: Dialect,
    vocab_hint: Optional[str],
    active_refs: frozenset[str],
    state: _WalkState,
) -> list[str]:
    """Render `matchable` (and everything beneath it) to a list of tokens."""
    if isinstance(matchable, Ref):
        name = matchable._ref
        if name in active_refs:
            return [_terminal_for(name, state)]
        try:
            target = dialect.ref(name)
        except (ValueError, RuntimeError):
            # Some keyword/grammar names are only wired up for certain
            # dialects (e.g. a shared grammar referencing a keyword that
            # one dialect doesn't define) - treat as unresolvable.
            return [_terminal_for(name, state)]
        return _render(target, dialect, name, active_refs | {name}, state)

    if isinstance(matchable, type) and issubclass(matchable, BaseSegment):
        if issubclass(matchable, MetaSegment):
            # Indent/Dedent/ImplicitIndent etc. are parse-tree markers with
            # no raw text of their own - they consume nothing when matched.
            return []
        grammar = getattr(matchable, "match_grammar", None)
        if grammar is None:
            return [_terminal_for(vocab_hint or matchable.__name__, state)]
        return _render(grammar, dialect, vocab_hint, active_refs, state)

    if isinstance(matchable, Nothing):
        # A placeholder that never matches (dialect-extension point) -
        # renders as nothing, same as an omitted optional element.
        return []

    if isinstance(matchable, Conditional):
        # Wraps an Indent/Dedent meta segment that only fires based on
        # reflow config rules - like MetaSegment above, no raw text of its
        # own. Without this case it fell through to the generic terminal
        # fallback and rendered as a stray "1", corrupting otherwise-valid
        # output (confirmed: mariadb's FromExpressionSegment renders "DUAL
        # 1 1" instead of "DUAL" - the two "1"s are two Conditionals).
        return []

    if isinstance(matchable, Bracketed):
        open_c, close_c = BRACKET_CHARS.get(matchable.bracket_type, ("(", ")"))
        inner = _render_sequence(matchable._elements, dialect, active_refs, state)
        return [open_c, *inner, close_c]

    if isinstance(matchable, Sequence):
        return _render_sequence(matchable._elements, dialect, active_refs, state)

    if isinstance(matchable, AnyNumberOf):
        return _render_branch(matchable, dialect, active_refs, state)

    if isinstance(matchable, (StringParser, MultiStringParser)):
        template = getattr(matchable, "template", None)
        if template is not None:
            return [template]  # StringParser: exactly one possible value.
        templates = getattr(matchable, "templates", None)
        if not templates:
            # e.g. a MultiStringParser built from an empty dialect set
            # (some dialects have no "bare functions", etc.)
            return [_terminal_for(vocab_hint, state)]
        # sorted(): templates is a set, whose iteration order depends on
        # PYTHONHASHSEED - sort first so the random pick below (and the
        # candidate indices assigned to the rest) are --seed-reproducible
        # across processes, not just within one.
        ranked = sorted(templates)
        chosen = state._choose(
            "template",
            ("template", id(matchable)),
            ranked,
            key_fn=lambda alt: ("template", id(matchable), alt),
        )
        return [chosen]

    # RegexParser, TypedParser, or anything else without literal text.
    return [_terminal_for(vocab_hint, state)]


def _render_sequence(
    elements: list,
    dialect: Dialect,
    active_refs: frozenset[str],
    state: _WalkState,
) -> list[str]:
    """Render a Sequence's elements.

    The baseline is the *minimal* rendering: optional elements are omitted
    by default. A minimal statement is far more likely to be valid than a
    maximal one (fewer chances to combine two optional clauses that don't
    actually make sense together, or to hit a rarely-exercised optional
    branch), so minimal is the safer anchor to build "one optional added"
    variants on top of, rather than "one optional removed" from a maximal
    (and more fragile) starting point.
    """
    tokens: list[str] = []
    for elem in elements:
        if elem.is_optional():
            candidate = state._register("add", elem)
            if candidate is None or candidate.index not in state.target_indices:
                continue  # Omitted by default; only a targeted variant adds it.
        tokens.extend(_render(elem, dialect, None, active_refs, state))
    return tokens


def _can_repeat(matchable: AnyNumberOf) -> bool:
    """Can this node meaningfully render its *chosen* element more than once?

    Only `Delimited` - not bare `AnyNumberOf`/`AnySetOf` more generally,
    despite the plan for this having originally said otherwise. Reasoning,
    corrected after finding the bug empirically: `_render_branch` picks one
    `chosen` element from `_elements` and (via this function) considers
    rendering *that same element* again N times. For `Delimited`, the
    elements are options for what a single homogeneous list item looks like,
    so re-rendering the chosen one twice, comma-separated, is exactly a real
    2-item list. For a bare `AnyNumberOf`/`AnySetOf` with `max_times > 1`
    (e.g. postgres's `CREATE TABLE` trailing-options node - `PARTITION BY`/
    `TABLESPACE`/`WITH(OUT) OIDS`/`ON COMMIT`/`USING`, each a *different*
    optional clause, `max_times=None`), the elements are heterogeneous
    alternatives, not repeatable content - confirmed generating
    "WITHOUT OIDS , WITHOUT OIDS" (nonsense: real SQL would need two
    *different* clauses together, e.g. "TABLESPACE foo WITHOUT OIDS", which
    `_render_branch`'s single-`chosen` model can't produce). Getting that
    case right would mean rendering several *different* elements together,
    not repeating one - a different mechanism than this function provides,
    and out of scope here. `Delimited` also subclasses `OneOf`, whose
    `__init__` hardcodes `max_times=1` unconditionally regardless of item
    count (confirmed: a real column-list `Delimited` still reports
    `max_times=1`), which is why `max_times` can't be used as the signal
    even for `Delimited` itself.
    """
    return isinstance(matchable, Delimited)


def _render_repeated(
    chosen: object,
    delimiter: object,
    count: int,
    dialect: Dialect,
    active_refs: frozenset[str],
    state: _WalkState,
) -> list[str]:
    """Render `chosen` `count` times, joined by `delimiter`'s own rendering.

    Only ever called for `Delimited` (see `_can_repeat`), which always has a
    real delimiter - no "no delimiter" case to handle.

    Known, disclosed simplification: each repetition renders the same
    `chosen` element again via the same deterministic walk, so e.g. a
    2-column table currently generates two columns with the same name/type
    ("foo, foo") rather than varied ones. Syntactically fine for what this
    tests (does the delimiter/trailing-comma/multi-item structure parse) -
    varying content per repetition is a distinct, separable enhancement.
    """
    delim_tokens = _render(delimiter, dialect, None, active_refs, state)
    tokens: list[str] = []
    for i in range(count):
        if i > 0:
            tokens.extend(delim_tokens)
        tokens.extend(_render(chosen, dialect, None, active_refs, state))
    return tokens


def _render_branch(
    matchable: AnyNumberOf,
    dialect: Dialect,
    active_refs: frozenset[str],
    state: _WalkState,
) -> list[str]:
    elements = matchable._elements
    if not elements:
        return []
    # A OneOf-style choice: at most one alternate can be active at a time.
    chosen = state._choose("branch", ("branch", id(matchable)), elements)

    if _can_repeat(matchable):
        # _can_repeat guarantees Delimited here, which always has a real
        # delimiter (default Ref("CommaSegment")) - never None.
        delimiter = matchable.delimiter
        shape_options: list = [1, *REPEAT_COUNTS]  # 1 = no repeat.
        if matchable.allow_trailing:
            shape_options.append("trailing")
        shape = state._choose(
            "count",
            ("shape", id(matchable)),
            shape_options,
            key_fn=lambda opt: ("count", id(matchable), opt),
        )
        if shape == "trailing":
            tokens = _render_repeated(chosen, delimiter, 2, dialect, active_refs, state)
            tokens.extend(_render(delimiter, dialect, None, active_refs, state))
            return tokens
        if shape != 1:
            return _render_repeated(
                chosen, delimiter, shape, dialect, active_refs, state
            )

    return _render(chosen, dialect, None, active_refs, state)


def _run_walk(
    entry: object,
    dialect: Dialect,
    known: dict[tuple, _Candidate],
    warned: set[str],
    rng: random.Random,
    defaults: dict[tuple, object],
    target_indices: frozenset[int] = frozenset(),
    collecting: bool = False,
) -> tuple[list[str], _WalkState]:
    state = _WalkState(
        dialect=dialect,
        known=known,
        rng=rng,
        warned=warned,
        defaults=defaults,
        target_indices=target_indices,
        collecting=collecting,
    )
    tokens = _render(entry, dialect, None, frozenset(), state)
    return tokens, state


def _join_tokens(tokens: list[str]) -> str:
    """Join rendered tokens into SQL text, gluing a bare `.` tight to its neighbors.

    Every other token pair gets a single separating space - fine for
    everything this tool renders except `DotSegment` (the `.` in a qualified
    reference like `schema.table`), which sqlfluff's own grammar parses
    differently depending on adjacent whitespace: confirmed `foo . bar` is
    unparsable while `foo.bar` isn't. This was a real, pre-existing gap in
    every earlier version of this tool - it stayed invisible because the old
    branch-selection heuristic (shortest-render-wins) almost always preferred
    a bare identifier over any qualified/dotted alternative, so a dotted
    reference was rarely the one thing keeping an example from self-checking.
    Random selection surfaces it constantly, since it no longer avoids the
    dotted form on purpose.
    """
    text = ""
    for token in tokens:
        if not text or token == "." or text.endswith("."):
            text += token
        else:
            text += " " + token
    return text


def generate(
    dialect_name: str,
    segment_name: str,
    max_examples: int = 50,
    coverage: int = 0,
    seed: Optional[int] = None,
) -> list[str]:
    """Generate SQL example strings for `segment_name` in `dialect_name`.

    `coverage` (0-100, default 0) trades runtime for how much of the grammar
    gets exercised - see the module docstring.

    `seed` makes the random default-choice draws (see `_WalkState._choose`)
    reproducible - the same `seed` with the same other arguments always
    produces the same output. Omit it for a fresh, unpredictable seed each
    call (drawn silently here - printing a seed for later reproduction is a
    CLI concern, see `main`).
    """
    discovery_depth, combination_width = _coverage_to_params(coverage)
    if seed is None:
        seed = random.SystemRandom().randrange(2**32)
    rng = random.Random(seed)

    dialect = dialect_selector(dialect_name)
    entry = _entry_point(segment_name, dialect)

    known: dict[tuple, _Candidate] = {}
    warned: set[str] = set()
    defaults: dict[tuple, object] = {}
    baseline_tokens, baseline_state = _run_walk(
        entry, dialect, known, warned, rng, defaults, collecting=True
    )
    examples = [_join_tokens(baseline_tokens)]

    # Discovery: for `discovery_depth` rounds, render each just-found
    # candidate on its own (with whatever prerequisites reach it) and see
    # what *new* candidates turn up nested inside it. Bounded by a safety cap
    # so a high coverage value on a heavily self-referential grammar (e.g.
    # expressions) can't run away.
    max_discovery_walks = max(50, max_examples * 10)
    discovery_walks = 0
    frontier = list(baseline_state.new_candidates)
    for _round in range(discovery_depth):
        if not frontier:
            break
        next_frontier: list[_Candidate] = []
        for candidate in frontier:
            if discovery_walks >= max_discovery_walks:
                break
            discovery_walks += 1
            target = candidate.requires | {candidate.index}
            _, round_state = _run_walk(
                entry,
                dialect,
                known,
                warned,
                rng,
                defaults,
                target_indices=target,
                collecting=True,
            )
            next_frontier.extend(round_state.new_candidates)
        frontier = next_frontier

    all_candidates = list(known.values())

    seen_texts = set(examples)
    for i, candidate in enumerate(all_candidates):
        if len(examples) >= max_examples:
            break
        target = set(candidate.requires) | {candidate.index}
        for width_offset in range(1, combination_width):
            other = all_candidates[(i + width_offset) % len(all_candidates)]
            target |= set(other.requires) | {other.index}
        tokens, _ = _run_walk(
            entry,
            dialect,
            known,
            warned,
            rng,
            defaults,
            target_indices=frozenset(target),
        )
        text = _join_tokens(tokens)
        if text not in seen_texts:
            seen_texts.add(text)
            examples.append(text)

    return examples


def _coverage_to_params(coverage: int) -> tuple[int, int]:
    """Map a 0-100 coverage value onto (discovery_depth, combination_width)."""
    if not 0 <= coverage <= 100:
        raise ValueError(f"coverage must be between 0 and 100, got {coverage}")
    discovery_depth = round(coverage / 100 * MAX_DISCOVERY_DEPTH)
    combination_width = max(1, round(coverage / 100 * MAX_COMBINATION_WIDTH))
    return discovery_depth, combination_width


def self_check(dialect_name: str, sql: str) -> bool:
    """Sanity-check that generated SQL parses cleanly under sqlfluff itself.

    Catches generator bugs (bad token joins, missing required siblings)
    rather than grammar bugs - this is not the real-engine check.
    """
    linter = Linter(dialect=dialect_name)
    parsed = linter.parse_string(sql)
    if not parsed.tree:
        return False
    if "unparsable" in parsed.tree.type_set():
        return False
    return not parsed.violations


def _coverage_arg(value: str) -> int:
    coverage = int(value)
    if not 0 <= coverage <= 100:
        raise argparse.ArgumentTypeError(
            f"--coverage must be between 0 and 100, got {coverage}"
        )
    return coverage


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: generate examples and print those that self-check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialect", required=True)
    parser.add_argument("--segment", required=True)
    parser.add_argument("--max-examples", type=int, default=50)
    parser.add_argument(
        "--coverage",
        type=_coverage_arg,
        default=0,
        help=(
            "0-100, default 0. Trades runtime for exploring deeper into "
            "optional/branch nesting and combining more toggles per example. "
            "Coarse-grained - see module docstring."
        ),
    )
    parser.add_argument(
        "--skip-self-check",
        action="store_true",
        help="Don't drop examples that fail to parse under sqlfluff itself.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help=(
            "Seed the random default-choice draws for reproducible output. "
            "Omit for a fresh random seed each run (printed to stderr so you "
            "can reproduce this exact output later)."
        ),
    )
    args = parser.parse_args(argv)

    seed = args.seed
    if seed is None:
        seed = random.SystemRandom().randrange(2**32)
        print(
            f"[generate_dialect_sql] no --seed given, using {seed} - "
            f"pass --seed {seed} to reproduce this output",
            file=sys.stderr,
        )

    try:
        examples = generate(
            args.dialect,
            args.segment,
            args.max_examples,
            args.coverage,
            seed,
        )
    except GenerationError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    for sql in examples:
        if not args.skip_self_check and not self_check(args.dialect, sql):
            print(
                f"[generate_dialect_sql] dropping unparsable example: {sql!r}",
                file=sys.stderr,
            )
            continue
        print(sql)

    return 0


if __name__ == "__main__":
    sys.exit(main())
