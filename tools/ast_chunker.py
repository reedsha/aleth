"""AST-driven structural chunking for source files (Phase 1 of the context rebuild).

This module replaces *text* slicing of source code with *structural* extraction. It
parses a file with tree-sitter (through the installed ``astchunk`` wrapper) and returns
one record per structurally complete unit -- a class, an interface, a method or a
function -- never a fixed line window and never a token-count split. The unit is the
whole node the grammar reports, so a chunk can never cut a function in half or glue two
unrelated definitions together.

Every record carries the fields a retriever and a cursor need to be exact:

* the ``filepath`` the unit came from;
* the ``byte_start``/``byte_end`` span of the node in that file (a byte range, not a
  line range -- the brief forbids line-number slicing, and a byte range is what lets a
  later phase point at the precise source); and
* the 1-based ``line_start``/``line_end`` plus 0-based columns, derived from the node's
  own points rather than counted by hand.

There is deliberately no regular expression here. Tree-sitter does the parsing; this
module only walks the tree it returns. Deciding "what is a class / a method / a
function" is a type lookup in the per-language tables below, not a pattern match, so a
grammar update changes classification the moment the parse does.

The output is a strict Pydantic model (``extra="forbid"``), matching the contract style
of ``tools/payloads.py``: a record with a field this module does not know is a boundary
error, not something to carry along quietly.

## Dependencies

The parser is ``astchunk`` (the cAST reference implementation) plus its grammar wheels:
``tree-sitter``, ``tree-sitter-python``, ``tree-sitter-java``, ``tree-sitter-c-sharp``,
``tree-sitter-typescript`` and ``pyrsistent``. **``tree-sitter`` is pinned to 0.25.2 on
purpose:** 0.26.0 builds segfault the interpreter when ``Node.start_point`` /
``end_point`` is read across a tree (reproduced deterministically on a ~2500-node
file), and 0.23.x is too old for the ABI-15 grammar wheels. 0.25.2 is the version that
parses and reads points cleanly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, ValidationError

from astchunk import ASTChunkBuilder

# The same strict configuration the rest of the backend's JSON boundaries use
# (``tools/payloads.py``). ``strict=False`` keeps Pydantic's ordinary coercions while the
# ``extra`` rule stays hard: an unknown field is refused, a missing one takes its default.
_STRICT = ConfigDict(extra="forbid", strict=False)

# A unit's semantic kind. ``method`` is a callable that lives inside a class or
# interface; ``function`` is a callable that does not. ``interface`` is a standalone
# interface declaration (Java/C#/TypeScript); Python has no equivalent, so it never
# appears there.
UnitKind = Literal["class", "interface", "method", "function"]

# The languages the installed wrapper ships grammars for, and the file extensions that
# select them. Keyed by the lower-cased suffix.
_EXTENSION_LANGUAGE: Dict[str, str] = {
    ".py": "python",
    ".java": "java",
    ".cs": "csharp",
    ".ts": "typescript",
    ".tsx": "typescript",
}


@dataclass(frozen=True)
class _LanguageSpec:
    """How one language spells the units we extract.

    ``containers`` maps a grammar node type to the kind it declares -- a
    ``class_definition`` is a ``class``, an ``interface_declaration`` is an
    ``interface``. ``callables`` maps a callable node type to its default kind; a
    callable whose kind is ``function`` is reclassified as a ``method`` when it is
    nested inside a container. ``decorated`` names the wrapper a language inserts
    around a decorated definition (Python's ``decorated_definition``), so the whole
    decorated unit is captured rather than only the bare ``def``.
    """

    containers: Dict[str, UnitKind]
    callables: Dict[str, UnitKind]
    decorated: Optional[str] = None


_LANGUAGE_SPECS: Dict[str, _LanguageSpec] = {
    "python": _LanguageSpec(
        containers={"class_definition": "class"},
        callables={"function_definition": "function"},
        decorated="decorated_definition",
    ),
    "java": _LanguageSpec(
        containers={
            "class_declaration": "class",
            "interface_declaration": "interface",
            "enum_declaration": "class",
            "record_declaration": "class",
        },
        callables={
            "method_declaration": "method",
            "constructor_declaration": "method",
        },
    ),
    "csharp": _LanguageSpec(
        containers={
            "class_declaration": "class",
            "interface_declaration": "interface",
            "struct_declaration": "class",
            "record_declaration": "class",
        },
        callables={
            "method_declaration": "method",
            "constructor_declaration": "method",
        },
    ),
    "typescript": _LanguageSpec(
        containers={
            "class_declaration": "class",
            "interface_declaration": "interface",
        },
        callables={
            "function_declaration": "function",
            "function_signature": "function",
            "method_definition": "method",
            "method_signature": "method",
        },
    ),
}


class CodeUnit(BaseModel):
    """One structurally complete unit of source code.

    ``byte_start``/``byte_end`` are the exact node span in the file (Python slice
    semantics: ``byte_end`` is exclusive), and ``content`` is exactly those bytes
    decoded -- so ``content`` and the file slice can never disagree.

    ``qualified_name`` is the full definition path (``Outer.Inner.method``), so two
    same-named symbols in different scopes are never confused by a retriever.
    """

    model_config = _STRICT

    filepath: str
    language: str
    kind: UnitKind
    name: str
    qualified_name: str
    node_type: str
    byte_start: int
    byte_end: int
    line_start: int
    line_end: int
    col_start: int
    col_end: int
    content: str


class CodeUnitEnvelope(BaseModel):
    """The JSON boundary a list of units crosses: the file it describes plus its units."""

    model_config = _STRICT

    filepath: str
    language: str
    units: List[CodeUnit]


@dataclass
class _ParserCache:
    """One tree-sitter parser per language, built by the installed AST wrapper.

    ``ASTChunkBuilder`` is the wrapper's entry point and owns the grammar selection and
    the tree-sitter version this module depends on, so the parser is taken from it
    rather than assembled here -- assembling one ourselves would be the "custom parser"
    the brief forbids. The builder's chunking knobs are irrelevant to structural
    extraction, so a placeholder budget is passed and never used.
    """

    _parsers: Dict[str, object] = field(default_factory=dict)

    def get(self, language: str) -> object:
        if language not in _LANGUAGE_SPECS:
            supported = ", ".join(sorted(_LANGUAGE_SPECS))
            raise ValueError(f"unsupported language {language!r} (supported: {supported})")
        parser = self._parsers.get(language)
        if parser is None:
            parser = ASTChunkBuilder(
                max_chunk_size=1,
                language=language,
                metadata_template="none",
            ).parser
            self._parsers[language] = parser
        return parser


_PARSERS = _ParserCache()


def detect_language(filepath: str) -> Optional[str]:
    """The language for ``filepath`` by extension, or ``None`` when we have no grammar.

    Returns ``None`` rather than raising so a caller can skip an unsupported file
    instead of failing a whole directory walk over one stray extension.
    """
    return _EXTENSION_LANGUAGE.get(os.path.splitext(filepath)[1].lower())


def _node_name(node) -> str:
    """The declared name of a definition node, or ``""`` when it is anonymous.

    Read from the grammar's ``name`` field, which is the grammar's own answer to "what
    is this called" -- not a search through the node's text.
    """
    named = node.child_by_field_name("name")
    if named is not None:
        return named.text.decode("utf-8", "replace")
    # Some callables (a TypeScript function signature, an unnamed default export) put the
    # identifier in a child rather than a ``name`` field; fall back to the first
    # identifier-shaped child the grammar reports.
    for child in node.children:
        if child.type in {"identifier", "type_identifier", "property_identifier"}:
            return child.text.decode("utf-8", "replace")
    return ""


def _record(
    source: bytes,
    filepath: str,
    language: str,
    def_node,
    range_node,
    path: Tuple[str, ...],
    kind: UnitKind,
) -> CodeUnit:
    """Build a :class:`CodeUnit` from a definition node.

    ``def_node`` supplies the semantic facts (name, kind); ``range_node`` supplies the
    span. They differ only for a decorated Python definition, where the wrapper is the
    complete unit but the ``def`` inside is what carries the name.

    ``path`` is the chain of enclosing definition names; the unit's own name is appended
    to it so the qualified name is the whole path.
    """
    name = _node_name(def_node)
    qualified = ".".join(path + (name,)) if name else ".".join(path)
    return CodeUnit(
        filepath=filepath,
        language=language,
        kind=kind,
        name=name,
        qualified_name=qualified,
        node_type=def_node.type,
        byte_start=range_node.start_byte,
        byte_end=range_node.end_byte,
        line_start=range_node.start_point.row + 1,
        line_end=range_node.end_point.row + 1,
        col_start=range_node.start_point.column,
        col_end=range_node.end_point.column,
        content=source[range_node.start_byte:range_node.end_byte].decode("utf-8", "replace"),
    )


def _walk(
    source: bytes,
    filepath: str,
    language: str,
    spec: _LanguageSpec,
    node,
    path: Tuple[str, ...],
    in_class_body: bool,
    out: List[CodeUnit],
) -> None:
    """Walk the tree under ``node``, appending every structural unit to ``out``.

    The walk is ordered as tree-sitter reports children, so units come out in source
    order without a sort. ``path`` is the enclosing definition names; ``in_class_body``
    says whether the nearest enclosing *definition* is a class, which is what decides a
    callable's kind (a closure inside a method is a function, not a method).
    """
    for child in node.children:
        _visit(source, filepath, language, spec, child, path, in_class_body, out)


def _visit(
    source: bytes,
    filepath: str,
    language: str,
    spec: _LanguageSpec,
    node,
    path: Tuple[str, ...],
    in_class_body: bool,
    out: List[CodeUnit],
) -> None:
    """Classify a single node, then recurse into the parts that can hold units."""
    node_type = node.type

    # A decorated Python definition: capture the wrapper's full span, but read the name
    # and kind from the definition it wraps so the decorators are part of the unit.
    if spec.decorated is not None and node_type == spec.decorated:
        inner = _decorated_inner(spec, node)
        if inner is None:
            return
        name = _node_name(inner)
        if inner.type in spec.containers:
            out.append(
                _record(source, filepath, language, inner, node, path, spec.containers[inner.type])
            )
            _walk(source, filepath, language, spec, inner, path + (name,), True, out)
        elif inner.type in spec.callables:
            kind = _callable_kind(spec, inner.type, in_class_body)
            out.append(_record(source, filepath, language, inner, node, path, kind))
            _walk(source, filepath, language, spec, inner, path + (name,), False, out)
        return

    if node_type in spec.containers:
        name = _node_name(node)
        out.append(
            _record(source, filepath, language, node, node, path, spec.containers[node_type])
        )
        _walk(source, filepath, language, spec, node, path + (name,), True, out)
        return

    if node_type in spec.callables:
        name = _node_name(node)
        kind = _callable_kind(spec, node_type, in_class_body)
        out.append(_record(source, filepath, language, node, node, path, kind))
        # A callable's body is not a class body, so a definition nested directly inside
        # it is a function even when the callable itself is a method.
        _walk(source, filepath, language, spec, node, path + (name,), False, out)
        return

    _walk(source, filepath, language, spec, node, path, in_class_body, out)


def _callable_kind(spec: _LanguageSpec, node_type: str, in_class_body: bool) -> UnitKind:
    """A callable's kind: ``method`` when its nearest enclosing definition is a class.

    A grammar may already say the node is a method (Java, C#, TypeScript); a language
    that spells every callable the same way (Python) is classified from its context.
    """
    kind = spec.callables[node_type]
    if kind == "function" and in_class_body:
        return "method"
    return kind


def _decorated_inner(spec: _LanguageSpec, node):
    """The definition a decorated wrapper encloses, or ``None`` if it holds none."""
    for child in node.children:
        if child.type in spec.containers or child.type in spec.callables:
            return child
    return None


def chunk_source(source: str, filepath: str, language: Optional[str] = None) -> List[CodeUnit]:
    """Extract every structural unit from ``source``, tagged with ``filepath``.

    ``language`` defaults to the one ``filepath``'s extension selects. The result is in
    source order, and each unit's byte span is exact for its ``filepath``.
    """
    resolved = language or detect_language(filepath)
    if resolved is None:
        raise ValueError(f"cannot detect a language for {filepath!r}; pass one explicitly")
    spec = _LANGUAGE_SPECS.get(resolved)
    if spec is None:
        supported = ", ".join(sorted(_LANGUAGE_SPECS))
        raise ValueError(f"unsupported language {resolved!r} (supported: {supported})")

    source_bytes = source.encode("utf-8")
    tree = _PARSERS.get(resolved).parse(source_bytes)
    units: List[CodeUnit] = []
    _walk(source_bytes, filepath, resolved, spec, tree.root_node, (), False, units)
    return units


def chunk_file(filepath: str) -> List[CodeUnit]:
    """Extract the structural units from a file on disk."""
    language = detect_language(filepath)
    if language is None:
        raise ValueError(f"cannot detect a language for {filepath!r}")
    with open(filepath, "rb") as handle:
        source = handle.read().decode("utf-8")
    return chunk_source(source, filepath, language)


def parse_has_errors(source: str, filepath: str, language: Optional[str] = None) -> bool:
    """Whether tree-sitter recovered from an error while parsing ``source``.

    Tree-sitter is error-tolerant: a malformed document still yields a tree, with the
    broken span marked as an ERROR (or MISSING) node. That is exactly what an edit that
    broke the file looks like, so this is the check a byte-range edit uses to refuse a
    splice before it is written.
    """
    resolved = language or detect_language(filepath)
    if resolved is None:
        raise ValueError(f"cannot detect a language for {filepath!r}; pass one explicitly")
    tree = _PARSERS.get(resolved).parse(source.encode("utf-8"))
    return bool(tree.root_node.has_error)


def units_to_json(filepath: str, language: str, units: List[CodeUnit]) -> str:
    """Serialise units to the strict JSON envelope (``CodeUnitEnvelope``)."""
    envelope = CodeUnitEnvelope(filepath=filepath, language=language, units=units)
    return envelope.model_dump_json(indent=2)


def units_from_json(text: str) -> CodeUnitEnvelope:
    """Parse and validate a JSON envelope, raising ``ValidationError`` on any drift."""
    return CodeUnitEnvelope.model_validate_json(text)
