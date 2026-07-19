# ruff: noqa: D101,D102,D103,E402
"""Approach #1: semantic checker of generated Rust tables vs Python grammar.

Loads the expanded Python dialect (_library) and the generated parser.rs
tables, then cross-checks by NAME (never by id):
 - dispatch completeness both ways (library <-> get_segment_grammar map)
 - dangling Ref names in the instruction stream
 - top-level variant + child-count + child identity per named segment
 - parser aux fidelity (template, type, instance_types) for String/Regex/
   Typed/MultiString parsers reachable by name
 - flags (optional / allow_gaps) and terminator counts at the top level
"""

import re
import sys
from pathlib import Path

from sqlfluff.core.dialects import dialect_selector

DIALECT_DIR = (
    Path(__file__).resolve().parents[2]
    / "sqlfluffrs"
    / "sqlfluffrs_dialects"
    / "src"
    / "dialect"
)

FINDINGS = []


def note(dialect, kind, detail):
    FINDINGS.append((dialect, kind, detail))
    print(f"[{kind}] {dialect}: {detail}")


class Tables:
    def __init__(self, path):
        src = path.read_text(encoding="utf-8")
        self.insts = []  # (variant, flags, fci, cc, ftermi, term_count, comment)
        m = re.search(r"pub static INSTRUCTIONS[^=]*= &\[(.*?)\n\];", src, re.S)
        for mm in re.finditer(
            r"GrammarInst \{ variant: GrammarVariant::(\w+), parse_mode: ParseMode::(\w+), "
            r"flags: GrammarFlags::from_bits\((\d+)\), first_child_idx: (\d+), "
            r"child_count: (\d+), min_times: (\d+), first_terminator_idx: (\d+), "
            r"terminator_count: (\d+), _padding: \d+ \}, // \[(\d+)\] (.*)",
            m.group(1),
        ):
            self.insts.append(
                dict(
                    variant=mm.group(1),
                    parse_mode=mm.group(2),
                    flags=int(mm.group(3)),
                    fci=int(mm.group(4)),
                    cc=int(mm.group(5)),
                    min_times=int(mm.group(6)),
                    ftermi=int(mm.group(7)),
                    term_count=int(mm.group(8)),
                    gid=int(mm.group(9)),
                    comment=mm.group(10),
                )
            )
        assert [i["gid"] for i in self.insts] == list(range(len(self.insts)))

        def arr(name):
            mm = re.search(rf"pub static {name}: &\[u32\] = &\[(.*?)\n\];", src, re.S)
            out = []
            for tok in mm.group(1).split(","):
                tok = tok.split("//")[0].strip()
                if tok:
                    out.append(int(tok))
            return out

        self.child_ids = arr("CHILD_IDS")
        self.aux = arr("AUX_DATA")
        self.aux_offsets = arr("AUX_DATA_OFFSETS")
        self.seg_type_off = arr("SEGMENT_TYPE_OFFSETS")
        self.seg_class_off = arr("SEGMENT_CLASS_OFFSETS")

        # Regex table.
        m = re.search(r"pub static REGEX_PATTERNS[^=]*= &\[(.*?)\n\];", src, re.S)
        self.regexes = []
        if m:
            for mm in re.finditer(
                r'r#"((?:[^"]|"(?!#))*)"#|Regex::new\(r#"((?:[^"]|"(?!#))*)"#\)',
                m.group(1),
            ):
                self.regexes.append(mm.group(1) or mm.group(2))

        # Strings table: only the STRINGS array region.
        m = re.search(r"pub static STRINGS[^=]*= &\[(.*?)\n\];", src, re.S)
        self.strings = []
        for mm in re.finditer(r'    "((?:[^"\\]|\\.)*)", // \[(\d+)\]', m.group(1)):
            assert int(mm.group(2)) == len(self.strings)
            self.strings.append(mm.group(1).encode().decode("unicode_escape"))

        # Dispatch map name -> gid
        self.dispatch = {}
        for mm in re.finditer(
            r'"((?:[^"\\]|\\.)*)" => Some\(RootGrammar \{ grammar_id: GrammarId\((\d+)\)',
            src,
        ):
            self.dispatch[mm.group(1)] = int(mm.group(2))

    def string(self, idx):
        return self.strings[idx]

    def children(self, gid):
        i = self.insts[gid]
        return self.child_ids[i["fci"] : i["fci"] + i["cc"]]

    def ref_name(self, gid):
        return self.string(self.aux_offsets[gid])

    def parser_aux(self, gid):
        o = self.aux_offsets[gid]
        template = self.string(self.aux[o])
        token_type = self.string(self.aux[o + 1])
        n_inst = self.aux[o + 3]
        inst = [self.string(i) for i in self.aux[o + 4 : o + 4 + n_inst]]
        return template, token_type, inst


# Map python grammar classes to expected table variants, in the same
# precedence order as build_parsers._convert_to_inst.
def py_variant(g):
    from sqlfluff.core.parser import (
        Anything,
        Nothing,
        Ref,
    )
    from sqlfluff.core.parser.grammar.anyof import AnyNumberOf, AnySetOf, OneOf
    from sqlfluff.core.parser.grammar.delimited import Delimited
    from sqlfluff.core.parser.grammar.sequence import Bracketed, Sequence
    from sqlfluff.core.parser.parsers import (
        MultiStringParser,
        RegexParser,
        StringParser,
        TypedParser,
    )

    if g.__class__ is Ref or isinstance(g, Ref):
        if g.__class__ is Ref:
            return "Ref"
    if g.__class__ is StringParser:
        return "StringParser"
    if g.__class__ is MultiStringParser:
        return "MultiStringParser"
    if g.__class__ is TypedParser:
        return "TypedParser"
    if g.__class__ is RegexParser:
        return "RegexParser"
    if isinstance(g, Bracketed):
        return "Bracketed"
    if isinstance(g, Delimited):
        return "Delimited"
    if isinstance(g, Sequence):
        return "Sequence"
    if isinstance(g, AnySetOf):
        return "AnySetOf"
    if isinstance(g, OneOf):
        return "OneOf"
    if isinstance(g, AnyNumberOf):
        return "AnyNumberOf"
    if isinstance(g, Nothing):
        return "Nothing"
    if isinstance(g, Anything):
        return "Anything"
    return None  # unknown/other (Conditional, Meta, etc.)


def check_dialect(dialect_name):
    from sqlfluff.core.parser.segments import BaseSegment

    path = DIALECT_DIR / dialect_name / "parser.rs"
    if not path.exists():
        return
    tables = Tables(path)
    dialect = dialect_selector(dialect_name)
    lib = {k.replace(" ", "_"): v for k, v in dialect._library.items()}

    # 1. Dispatch completeness both directions.
    for name in lib:
        if name not in tables.dispatch:
            note(
                dialect_name,
                "MISSING_DISPATCH",
                f"library name {name!r} not in table dispatch",
            )
    for name in tables.dispatch:
        if name not in lib:
            note(
                dialect_name,
                "EXTRA_DISPATCH",
                f"dispatch name {name!r} not in python library",
            )

    # 2. Dangling Ref names anywhere in the instruction stream.
    for inst in tables.insts:
        if inst["variant"] == "Ref":
            rname = tables.ref_name(inst["gid"])
            if rname not in tables.dispatch and inst["cc"] == 0:
                note(
                    dialect_name,
                    "DANGLING_REF",
                    f"gid {inst['gid']} Ref({rname!r}) resolves to nothing "
                    f"(runtime returns Empty; python would raise)",
                )

    # 3/4/5. Per-name shape and parser fidelity.
    for name, entry in sorted(lib.items()):
        gid = tables.dispatch.get(name)
        if gid is None:
            continue
        inst = tables.insts[gid]
        # Resolve the python grammar this maps to (class -> match_grammar).
        g = entry
        if isinstance(g, type) and issubclass(g, BaseSegment):
            g = getattr(g, "match_grammar", None)
            if g is None:
                continue
        expected = py_variant(g)
        if expected is None:
            continue
        if expected != inst["variant"]:
            note(
                dialect_name,
                "VARIANT_MISMATCH",
                f"{name}: python {type(g).__name__} -> expected {expected}, "
                f"table has {inst['variant']} (gid {gid}, {inst['comment'][:40]})",
            )
            continue
        # Child counts for container variants.
        if expected in ("Sequence", "OneOf", "AnyNumberOf", "AnySetOf"):
            elements = getattr(g, "_elements", None)
            if elements is not None:
                py_n = len(elements)
                table_n = inst["cc"]
                has_exclude = bool(inst["flags"] & (1 << 6))
                if has_exclude:
                    table_n -= 1
                if py_n != table_n:
                    note(
                        dialect_name,
                        "CHILD_COUNT_MISMATCH",
                        f"{name}: python {py_n} elements, table {table_n} (gid {gid})",
                    )
                    continue
                # Child identity for Ref/parser children.
                from sqlfluff.core.parser import Ref as PyRef

                for i, (pych, chgid) in enumerate(zip(elements, tables.children(gid))):
                    chinst = tables.insts[chgid]
                    if pych.__class__ is PyRef and chinst["variant"] == "Ref":
                        pyname = pych._ref.replace(" ", "_")
                        tname = tables.ref_name(chgid)
                        if pyname != tname:
                            note(
                                dialect_name,
                                "REF_TARGET_MISMATCH",
                                f"{name}[{i}]: python Ref({pyname!r}) but table "
                                f"Ref({tname!r}) (gid {chgid})",
                            )
        # Parse mode parity (Strict vs Greedy vs GreedyOnceStarted).
        py_mode = getattr(g, "parse_mode", None)
        if py_mode is not None:
            py_mode_name = {
                "STRICT": "Strict",
                "GREEDY": "Greedy",
                "GREEDY_ONCE_STARTED": "GreedyOnceStarted",
            }.get(py_mode.name, py_mode.name)
            if py_mode_name != inst["parse_mode"]:
                note(
                    dialect_name,
                    "PARSE_MODE_MISMATCH",
                    f"{name}: python {py_mode.name}, table {inst['parse_mode']} (gid {gid})",
                )
        # Terminator count parity.
        py_terms = getattr(g, "terminators", ()) or ()
        if len(py_terms) != inst["term_count"]:
            note(
                dialect_name,
                "TERMINATOR_COUNT_MISMATCH",
                f"{name}: python {len(py_terms)} terminators, table "
                f"{inst['term_count']} (gid {gid})",
            )
        # Optional / allow_gaps flags.
        py_optional = bool(getattr(g, "optional", False))
        t_optional = bool(inst["flags"] & 1)
        if py_optional != t_optional:
            note(
                dialect_name,
                "OPTIONAL_FLAG_MISMATCH",
                f"{name}: python optional={py_optional}, table {t_optional} (gid {gid})",
            )
        py_gaps = getattr(g, "allow_gaps", None)
        if py_gaps is not None:
            t_gaps = bool(inst["flags"] & 4)
            if bool(py_gaps) != t_gaps:
                note(
                    dialect_name,
                    "ALLOW_GAPS_MISMATCH",
                    f"{name}: python allow_gaps={py_gaps}, table {t_gaps} (gid {gid})",
                )
        # Parser aux fidelity for direct parser entries.
        if expected == "StringParser":
            template, token_type, inst_types = tables.parser_aux(gid)
            py_template = g.template
            py_types = list(g._instance_types or (g.raw_class.type,))
            if template != py_template:
                note(
                    dialect_name,
                    "TEMPLATE_MISMATCH",
                    f"{name}: python template {py_template!r}, table {template!r}",
                )
            if py_types and [token_type] != py_types[:1]:
                note(
                    dialect_name,
                    "TYPE_MISMATCH",
                    f"{name}: python type {py_types!r}, table {token_type!r}",
                )


def main(dialects=None):
    names = dialects or sorted(d.name for d in DIALECT_DIR.iterdir() if d.is_dir())
    for name in names:
        try:
            check_dialect(name)
        except BaseException as e:
            note(name, "HARNESS", f"{type(e).__name__}: {e}")
            import traceback

            traceback.print_exc()
        print(f"... {name} done", file=sys.stderr)
    kinds = {}
    for _, kind, _ in FINDINGS:
        kinds[kind] = kinds.get(kind, 0) + 1
    print(f"TOTAL findings: {len(FINDINGS)} by kind: {kinds}")


if __name__ == "__main__":
    main(sys.argv[1:] or None)
