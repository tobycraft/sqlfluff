"""Generate SQL text by walking a dialect's grammar objects.

Starting from a named segment or grammar (e.g. ``SelectStatementSegment``),
walks the ``Sequence``/``OneOf``/``AnySetOf``/``Bracketed``/``Delimited``/``Ref``
grammar objects sqlfluff builds in memory for a dialect and renders concrete SQL
text: one minimal "baseline" example with every optional element omitted, plus
one variant per optional element (with just that one added back in) and one
variant per branch of a ``OneOf``/``AnySetOf``/``Delimited`` (with that branch
substituted in). Minimal is the baseline (rather than "everything present")
because it's far more likely to be valid SQL - fewer chances to combine two
optional clauses that don't make sense together or land in a rarely-used
optional branch.

Three further dimensions get the same "one candidate per option" treatment:
``MultiStringParser`` keyword alternatives (e.g. a bare-function set like
``CURRENT_DATE``/``CURRENT_TIME``/...), repetition count on ``Delimited``
(rendering 2 or 3 items instead of always exactly one, plus a trailing-
delimiter variant where the grammar allows one - bare ``AnyNumberOf``/
``AnySetOf`` are deliberately excluded, since their options are usually
heterogeneous siblings rather than a repeatable homogeneous list, and
re-rendering the same chosen element N times there produces nonsense like
repeating a whole unrelated clause), and alternate values for terminal
vocabulary categories (a second/third representative identifier, number,
etc.). All of these register as ordinary candidates in the same registry
optional-elements and branches already use, so they're discovered and
combined by the same machinery described below.

By default only the optional elements and branches reachable *while rendering
the minimal baseline* are ever discovered - anything nested inside an optional
that's omitted by default, or a branch that isn't chosen, is invisible. The
``coverage`` parameter (0-100, default 0) trades runtime for looking deeper:
it internally controls two things -

- how many rounds of "render this known candidate, see what new candidates
  turn up nested inside it" to run, so e.g. a column-definition list that's
  itself optional gets rendered at least once, revealing the real column/
  constraint grammar nested inside it, not just an empty ``()``;
- how many candidates get toggled on *simultaneously* in one example, so
  interactions between two optional clauses (not just one at a time against
  the minimal baseline) get exercised too.

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

# How many repeated items to try for a Delimited node (see _can_repeat for
# why this doesn't extend to bare AnyNumberOf/AnySetOf). Fixed and small -
# not derived from the grammar's own bounds, and not attempting to fully
# replicate sqlfluff's real matching rules. 2 proves "does a second item and
# its delimiter parse," 3 adds a little more confidence without meaningfully
# increasing cost.
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
    kind: str  # "add", "branch", "template", "value", "repeat", or "trailing"
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
    """

    dialect: Dialect
    known: dict[tuple, _Candidate]
    # Vocab-gap names already warned about - shared across every walk in one
    # `generate()` call (like `known`), so each gap prints once per segment
    # rather than once per occurrence.
    warned: set[str] = field(default_factory=set)
    target_indices: frozenset[int] = frozenset()
    collecting: bool = False
    new_candidates: list[_Candidate] = field(default_factory=list)
    # True while rendering a _branch_score probe: skip the (expensive,
    # recursive) branch-ranking below and just take element 0, so scoring
    # one branch can't cascade into scoring every branch beneath it.
    scoring_probe: bool = False

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


def _entry_point(name: str, dialect: Dialect):
    """Resolve a --segment name to something _render can walk."""
    try:
        return dialect.ref(name)
    except (ValueError, RuntimeError) as err:
        raise GenerationError(f"Unknown segment/grammar {name!r}: {err}") from err


def _terminal_for(name: Optional[str], state: _WalkState) -> str:
    """Return a representative literal for an unresolved terminal `name`.

    Registers any *additional* representative values (beyond the first,
    which is always the default/baseline value) as ordinary "value"
    candidates, the same way _render_branch registers branch alternates.
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

    chosen = values[0]
    for alt in values[1:]:
        candidate = state._register("value", alt, key=("value", name, alt))
        if candidate is not None and candidate.index in state.target_indices:
            chosen = alt
            break
    return chosen


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
        ranked = sorted(templates)
        chosen = ranked[0]
        for alt in ranked[1:]:
            candidate = state._register(
                "template", alt, key=("template", id(matchable), alt)
            )
            if candidate is not None and candidate.index in state.target_indices:
                chosen = alt
                break
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


def _prefers_reference(elem: object) -> bool:
    """Tie-break hint: does this option look like the "plain identifier" case?

    Several grammars offer a keyword/bare-function alternative alongside a
    plain table/column reference in the same OneOf (e.g. `TableExpressionSegment`
    - shared by nearly every dialect - lists `BareFunctionSegment` ahead of
    `TableReferenceSegment`). A bare keyword resolves in a single token just
    like a plain reference does, so `_branch_score` alone often ties them, and
    the stable sort then silently prefers whichever was declared first - which
    is the special case here, not the common one. Confirmed concretely:
    mariadb's `DeleteStatementSegment` generated only `DELETE FROM CURRENT_DATE
    ...` (a bare no-parens datetime function standing in for a table name)
    because `BareFunctionSegment` beat `TableReferenceSegment` on exactly this
    tie. Reuses the same suffix convention as `SUFFIX_VOCAB` - a `Ref` name
    ending in one of these means "this is what a real identifier/reference
    looks like here," which is what a tie should resolve in favor of.
    """
    if not isinstance(elem, Ref):
        return False
    return any(elem._ref.endswith(suffix) for suffix, _ in SUFFIX_VOCAB)


def _branch_score(elem: object, dialect: Dialect, warned: set[str]) -> int:
    """Cheap proxy for how 'simple' a branch is: token count of a shallow render.

    Grammars frequently list their most exotic form first (e.g. a BigQuery
    ML.PREDICT table function ahead of a plain table reference), so always
    picking element 0 tends to produce the least representative example. A
    probe render is a cheap enough stand-in for "which branch is the plain/
    common one" without fully expanding every option - "cheap" here comes
    from the cycle guard alone (no separate depth cap): confirmed across a
    full 28-dialect x 3-segment sweep that unbounded-by-depth probing still
    completes in ~2s total, ~100ms worst case, since a probe can only
    recurse through the dialect's few thousand distinct `Ref` names once
    each before the cycle guard stops it.

    The probe itself must not re-rank branches it encounters (that would
    cascade into scoring every branch beneath every branch), so it runs with
    `scoring_probe=True`, which makes nested `_render_branch` calls fall back
    to plain element-0 selection instead of calling back into this function.
    It also uses its own throwaway `known` registry, not the real discovery
    process's, so probing never registers or consumes candidate identities
    (harmless even when it tries: `collecting` is False by default, and
    `_register` only ever creates an entry while collecting).
    `warned` is still the real, shared one, though - a probe can bottom out
    at the same vocab gaps as a real render, and those should count against
    the same once-per-segment budget rather than a separate, discarded one.
    """
    probe_state = _WalkState(
        dialect=dialect,
        known={},
        warned=warned,
        scoring_probe=True,
    )
    try:
        return len(_render(elem, dialect, None, frozenset(), probe_state))
    except RecursionError:  # pragma: no cover - defensive only
        return 10**6


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
    if state.scoring_probe:
        ranked = elements
    else:
        ranked = sorted(
            elements,
            key=lambda e: (
                _branch_score(e, dialect, state.warned),
                0 if _prefers_reference(e) else 1,
            ),
        )
    chosen = ranked[0]  # The default: whichever alternate isn't targeted renders this.
    for alt in ranked[1:]:
        candidate = state._register("branch", alt)
        if candidate is not None and candidate.index in state.target_indices:
            chosen = alt
            break  # A OneOf-style choice: at most one alternate can be active.

    if _can_repeat(matchable):
        # _can_repeat guarantees Delimited here, which always has a real
        # delimiter (default Ref("CommaSegment")) - never None.
        delimiter = matchable.delimiter
        for count in REPEAT_COUNTS:
            candidate = state._register(
                "repeat", count, key=("repeat", id(matchable), count)
            )
            if candidate is not None and candidate.index in state.target_indices:
                return _render_repeated(
                    chosen, delimiter, count, dialect, active_refs, state
                )
        if matchable.allow_trailing:
            candidate = state._register(
                "trailing", True, key=("trailing", id(matchable))
            )
            if candidate is not None and candidate.index in state.target_indices:
                tokens = _render_repeated(
                    chosen, delimiter, 2, dialect, active_refs, state
                )
                tokens.extend(_render(delimiter, dialect, None, active_refs, state))
                return tokens

    return _render(chosen, dialect, None, active_refs, state)


def _run_walk(
    entry: object,
    dialect: Dialect,
    known: dict[tuple, _Candidate],
    warned: set[str],
    target_indices: frozenset[int] = frozenset(),
    collecting: bool = False,
) -> tuple[list[str], _WalkState]:
    state = _WalkState(
        dialect=dialect,
        known=known,
        warned=warned,
        target_indices=target_indices,
        collecting=collecting,
    )
    tokens = _render(entry, dialect, None, frozenset(), state)
    return tokens, state


def generate(
    dialect_name: str,
    segment_name: str,
    max_examples: int = 50,
    coverage: int = 0,
) -> list[str]:
    """Generate SQL example strings for `segment_name` in `dialect_name`.

    `coverage` (0-100, default 0) trades runtime for how much of the grammar
    gets exercised - see the module docstring.
    """
    discovery_depth, combination_width = _coverage_to_params(coverage)

    dialect = dialect_selector(dialect_name)
    entry = _entry_point(segment_name, dialect)

    known: dict[tuple, _Candidate] = {}
    warned: set[str] = set()
    baseline_tokens, baseline_state = _run_walk(
        entry, dialect, known, warned, collecting=True
    )
    examples = [" ".join(baseline_tokens)]

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
            entry, dialect, known, warned, target_indices=frozenset(target)
        )
        text = " ".join(tokens)
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
    args = parser.parse_args(argv)

    try:
        examples = generate(
            args.dialect,
            args.segment,
            args.max_examples,
            args.coverage,
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
