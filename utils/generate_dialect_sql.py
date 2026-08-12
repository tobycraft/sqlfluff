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
``Ref`` name that led to them. Recursion is bounded by both a depth limit and a
cycle guard, since dialect grammars are frequently self-referential (e.g.
expressions containing expressions).

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
from sqlfluff.core.parser.grammar.sequence import Bracketed, Sequence
from sqlfluff.core.parser.parsers import MultiStringParser, StringParser
from sqlfluff.core.parser.segments import BaseSegment, MetaSegment

# Terminal matchers that don't carry their own literal text, keyed by the
# `Ref` name that led to them. Names reached through a `Ref` that don't fit
# the suffix patterns below.
TERMINAL_VOCAB = {
    "ParameterNameSegment": "foo",
    "NumericLiteralSegment": "1",
    "QuotedLiteralSegment": "'a'",
    "BooleanLiteralGrammar": "true",
}

# Every dialect defines its own family of identifier/reference grammar names
# (NakedIdentifierSegment, SingleCSIdentifierGrammar, TableReferenceSegment,
# HiveReferenceIdentifierGrammar, ...) - rather than enumerating each dialect's
# variants by exact name, match by suffix. More specific suffixes are listed
# first since matching stops at the first hit (e.g. "QuotedIdentifierSegment"
# would otherwise also match the plain "IdentifierSegment" entry below it).
SUFFIX_VOCAB = [
    ("QuotedIdentifierSegment", '"foo"'),
    ("IdentifierSegment", "foo"),
    ("IdentifierGrammar", "foo"),
    ("ReferenceSegment", "foo"),
    ("ReferenceGrammar", "foo"),
]

# Anything matching neither TERMINAL_VOCAB nor SUFFIX_VOCAB falls back to
# this and prints a note to stderr, so gaps stay visible instead of silently
# producing bad SQL.
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


class GenerationError(Exception):
    """Raised when the requested entry point can't be resolved."""


def _terminal_for(name: Optional[str]) -> str:
    if name and name in TERMINAL_VOCAB:
        return TERMINAL_VOCAB[name]
    if name:
        for suffix, value in SUFFIX_VOCAB:
            if name.endswith(suffix):
                return value
    if name:
        print(
            f"[generate_dialect_sql] no vocab entry for {name!r}, "
            f"using fallback {DEFAULT_FALLBACK!r}",
            file=sys.stderr,
        )
    return DEFAULT_FALLBACK


def _coverage_to_params(coverage: int) -> tuple[int, int]:
    """Map a 0-100 coverage value onto (discovery_depth, combination_width)."""
    if not 0 <= coverage <= 100:
        raise ValueError(f"coverage must be between 0 and 100, got {coverage}")
    discovery_depth = round(coverage / 100 * MAX_DISCOVERY_DEPTH)
    combination_width = max(1, round(coverage / 100 * MAX_COMBINATION_WIDTH))
    return discovery_depth, combination_width


@dataclass
class _Candidate:
    """One point in the grammar tree where a variant could be generated."""

    index: int
    kind: str  # "add" (an omitted optional) or "branch" (an alternate option)
    payload: object  # the element to render
    # Other candidates' global ids that must also be active to reach this one
    # (its ancestors in the optional/branch nesting).
    requires: frozenset[int] = frozenset()


@dataclass
class _WalkState:
    """Threaded through one recursive walk of the grammar tree.

    `known` is a registry shared across *every* walk in one `generate()` call
    (not reset per walk), keyed by `(kind, id(payload))`. Grammar objects are
    constructed once when a dialect module loads and reused for the process's
    lifetime, so `id(payload)` is a stable identity for "is this the same
    grammar decision point" across arbitrarily many walks with different
    active overrides - simpler than tracking a full structural path.
    """

    dialect: Dialect
    max_depth: int
    known: dict[tuple[str, int], _Candidate]
    target_indices: frozenset[int] = frozenset()
    collecting: bool = False
    new_candidates: list[_Candidate] = field(default_factory=list)
    # True while rendering a _branch_score probe: skip the (expensive,
    # recursive) branch-ranking below and just take element 0, so scoring
    # one branch can't cascade into scoring every branch beneath it.
    scoring_probe: bool = False

    def _register(self, kind: str, payload: object) -> Optional[_Candidate]:
        """Look up (or, if collecting, create) the candidate for this point."""
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


def _render(
    matchable: object,
    dialect: Dialect,
    vocab_hint: Optional[str],
    depth: int,
    active_refs: frozenset[str],
    state: _WalkState,
) -> list[str]:
    """Render `matchable` (and everything beneath it) to a list of tokens."""
    if isinstance(matchable, Ref):
        name = matchable._ref
        if name in active_refs or depth >= state.max_depth:
            return [_terminal_for(name)]
        try:
            target = dialect.ref(name)
        except (ValueError, RuntimeError):
            # Some keyword/grammar names are only wired up for certain
            # dialects (e.g. a shared grammar referencing a keyword that
            # one dialect doesn't define) - treat as unresolvable.
            return [_terminal_for(name)]
        return _render(target, dialect, name, depth + 1, active_refs | {name}, state)

    if isinstance(matchable, type) and issubclass(matchable, BaseSegment):
        if issubclass(matchable, MetaSegment):
            # Indent/Dedent/ImplicitIndent etc. are parse-tree markers with
            # no raw text of their own - they consume nothing when matched.
            return []
        grammar = getattr(matchable, "match_grammar", None)
        if grammar is None:
            return [_terminal_for(vocab_hint or matchable.__name__)]
        return _render(grammar, dialect, vocab_hint, depth, active_refs, state)

    if isinstance(matchable, Nothing):
        # A placeholder that never matches (dialect-extension point) -
        # renders as nothing, same as an omitted optional element.
        return []

    if isinstance(matchable, Bracketed):
        open_c, close_c = BRACKET_CHARS.get(matchable.bracket_type, ("(", ")"))
        inner = _render_sequence(
            matchable._elements, dialect, depth, active_refs, state
        )
        return [open_c, *inner, close_c]

    if isinstance(matchable, Sequence):
        return _render_sequence(matchable._elements, dialect, depth, active_refs, state)

    if isinstance(matchable, AnyNumberOf):
        return _render_branch(matchable._elements, dialect, depth, active_refs, state)

    if isinstance(matchable, (StringParser, MultiStringParser)):
        template = getattr(matchable, "template", None)
        if template is None:
            templates = getattr(matchable, "templates", None)
            if not templates:
                # e.g. a MultiStringParser built from an empty dialect set
                # (some dialects have no "bare functions", etc.)
                return [_terminal_for(vocab_hint)]
            template = sorted(templates)[0]
        return [template]

    # RegexParser, TypedParser, or anything else without literal text.
    return [_terminal_for(vocab_hint)]


def _render_sequence(
    elements: list,
    dialect: Dialect,
    depth: int,
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
        tokens.extend(_render(elem, dialect, None, depth, active_refs, state))
    return tokens


def _branch_score(elem: object, dialect: Dialect, probe_depth: int = 3) -> int:
    """Cheap proxy for how 'simple' a branch is: token count of a shallow render.

    Grammars frequently list their most exotic form first (e.g. a BigQuery
    ML.PREDICT table function ahead of a plain table reference), so always
    picking element 0 tends to produce the least representative example.
    A short, depth-capped probe render is a cheap enough stand-in for "which
    branch is the plain/common one" without fully expanding every option.

    The probe itself must not re-rank branches it encounters (that would
    cascade into scoring every branch beneath every branch), so it runs with
    `scoring_probe=True`, which makes nested `_render_branch` calls fall back
    to plain element-0 selection instead of calling back into this function.
    It also uses its own throwaway `known` registry, not the real discovery
    process's, so probing never registers or consumes candidate identities.
    """
    probe_state = _WalkState(
        dialect=dialect, max_depth=probe_depth, known={}, scoring_probe=True
    )
    try:
        return len(_render(elem, dialect, None, 0, frozenset(), probe_state))
    except RecursionError:  # pragma: no cover - defensive only
        return 10**6


def _render_branch(
    elements: list,
    dialect: Dialect,
    depth: int,
    active_refs: frozenset[str],
    state: _WalkState,
) -> list[str]:
    if not elements:
        return []
    if state.scoring_probe:
        ranked = elements
    else:
        ranked = sorted(elements, key=lambda e: _branch_score(e, dialect))
    chosen = ranked[0]  # The default: whichever alternate isn't targeted renders this.
    for alt in ranked[1:]:
        candidate = state._register("branch", alt)
        if candidate is not None and candidate.index in state.target_indices:
            chosen = alt
            break  # A OneOf-style choice: at most one alternate can be active.
    return _render(chosen, dialect, None, depth, active_refs, state)


def _run_walk(
    entry: object,
    dialect: Dialect,
    max_depth: int,
    known: dict[tuple[str, int], _Candidate],
    target_indices: frozenset[int] = frozenset(),
    collecting: bool = False,
) -> tuple[list[str], _WalkState]:
    state = _WalkState(
        dialect=dialect,
        max_depth=max_depth,
        known=known,
        target_indices=target_indices,
        collecting=collecting,
    )
    tokens = _render(entry, dialect, None, 0, frozenset(), state)
    return tokens, state


def generate(
    dialect_name: str,
    segment_name: str,
    max_depth: int = 8,
    max_examples: int = 50,
    coverage: int = 0,
) -> list[str]:
    """Generate SQL example strings for `segment_name` in `dialect_name`.

    `coverage` (0-100, default 0) trades runtime for how much of the grammar
    gets exercised - see the module docstring. `coverage=0` reproduces the
    exact output of every earlier version of this tool.
    """
    discovery_depth, combination_width = _coverage_to_params(coverage)

    dialect = dialect_selector(dialect_name)
    entry = _entry_point(segment_name, dialect)

    known: dict[tuple[str, int], _Candidate] = {}
    baseline_tokens, baseline_state = _run_walk(
        entry, dialect, max_depth, known, collecting=True
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
                entry, dialect, max_depth, known, target_indices=target, collecting=True
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
            entry, dialect, max_depth, known, target_indices=frozenset(target)
        )
        text = " ".join(tokens)
        if text not in seen_texts:
            seen_texts.add(text)
            examples.append(text)

    return examples


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
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--max-examples", type=int, default=50)
    parser.add_argument(
        "--coverage",
        type=_coverage_arg,
        default=0,
        help=(
            "0-100, default 0 (today's minimal behavior). Trades runtime for "
            "exploring deeper into optional/branch nesting and combining more "
            "toggles per example. Coarse-grained - see module docstring."
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
            args.max_depth,
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
