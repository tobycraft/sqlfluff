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


@dataclass
class _Candidate:
    """One point in the grammar tree where a variant could be generated."""

    index: int
    kind: str  # "add" (include an omitted-by-default optional) or "branch" (pick another option)
    payload: object  # the alternate element to render (branch candidates only)


@dataclass
class _WalkState:
    """Threaded through one recursive walk of the grammar tree.

    A single walk either just *counts* candidates (target_index is None) or
    renders the tree while applying one specific override (target_index set).
    Re-walking the (deterministic) tree once per candidate is simpler than
    building and cloning an explicit tree structure, and is cheap at the
    example counts this tool targets.
    """

    dialect: Dialect
    max_depth: int
    target_index: Optional[int] = None
    target_payload: object = None
    counter: int = 0
    candidates: list[_Candidate] = field(default_factory=list)
    applied: bool = False
    # True while rendering a _branch_score probe: skip the (expensive,
    # recursive) branch-ranking below and just take element 0, so scoring
    # one branch can't cascade into scoring every branch beneath it.
    scoring_probe: bool = False


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
            idx = state.counter
            state.counter += 1
            if state.target_index is None and not state.scoring_probe:
                state.candidates.append(_Candidate(idx, "add", elem))
            if idx != state.target_index:
                continue  # Omitted by default; only the targeted variant adds it.
            state.applied = True
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
    """
    probe_state = _WalkState(dialect=dialect, max_depth=probe_depth, scoring_probe=True)
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
    idx = state.counter
    state.counter += 1
    if state.scoring_probe:
        ranked = elements
    else:
        ranked = sorted(elements, key=lambda e: _branch_score(e, dialect))
    chosen = ranked[0]
    if state.target_index is None and not state.scoring_probe:
        for other in ranked[1:]:
            state.candidates.append(_Candidate(idx, "branch", other))
    elif idx == state.target_index:
        state.applied = True
        chosen = state.target_payload
    return _render(chosen, dialect, None, depth, active_refs, state)


def _run_walk(
    entry: object, dialect: Dialect, max_depth: int, target: Optional[_Candidate]
) -> tuple[list[str], _WalkState]:
    state = _WalkState(dialect=dialect, max_depth=max_depth)
    if target is not None:
        state.target_index = target.index
        # _render_branch reads the override payload off the state for
        # "branch" candidates; "add" candidates are handled inline by index.
        state.target_payload = target.payload
    tokens = _render(entry, dialect, None, 0, frozenset(), state)
    return tokens, state


def generate(
    dialect_name: str, segment_name: str, max_depth: int = 8, max_examples: int = 50
) -> list[str]:
    """Generate SQL example strings for `segment_name` in `dialect_name`."""
    dialect = dialect_selector(dialect_name)
    entry = _entry_point(segment_name, dialect)

    baseline_tokens, state = _run_walk(entry, dialect, max_depth, target=None)
    examples = [" ".join(baseline_tokens)]

    for candidate in state.candidates[: max_examples - 1]:
        tokens, _ = _run_walk(entry, dialect, max_depth, target=candidate)
        examples.append(" ".join(tokens))

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


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: generate examples and print those that self-check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialect", required=True)
    parser.add_argument("--segment", required=True)
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--max-examples", type=int, default=50)
    parser.add_argument(
        "--skip-self-check",
        action="store_true",
        help="Don't drop examples that fail to parse under sqlfluff itself.",
    )
    args = parser.parse_args(argv)

    try:
        examples = generate(
            args.dialect, args.segment, args.max_depth, args.max_examples
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
