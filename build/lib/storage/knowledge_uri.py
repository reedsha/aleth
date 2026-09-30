"""Strict, deterministic addressing for the Knowledge Graph: the ``kernel://`` URI scheme.

Every entity in the graph is named by exactly one URI, and that URI is its primary key. A
primary key that can be spelled two ways is two entities, and a graph holding quiet duplicates
is worse than one that refuses a bad address -- so the rules here are narrow and the parser
**raises** rather than coercing. Nothing is inferred and nothing is matched loosely.

Four shapes, and no others::

    kernel://symbol/<file_path>:<symbol_name>   kernel://symbol/tools/ast_tools.py:edit_ast_node
    kernel://rule/<rule_id>                     kernel://rule/chokehold_refuse_overwrite
    kernel://task/<task_id>                     kernel://task/task-104
    kernel://file/<file_path>                   kernel://file/orchestration/runner.py

Why the validation is this strict:

* a URI reaches a **file**, so a ``..`` segment or an absolute path in one is a traversal, not
  a cosmetic detail -- both are refused, in the factories and in the parser, by the same check;
* backslashes are normalised to ``/`` so the same file addresses identically on Windows and
  POSIX. A URI that differed by platform would silently fork the graph;
* the scheme, the entity type and every component are validated explicitly. ``?``, ``#``, a
  port-shaped authority and an unknown kind are all refusals.

**A hard system limitation, not only a security feature.** ``'`` and ``"`` are refused in a
path component. That is required -- the vector store interpolates a URI into a LanceDB ``where``
clause, which has no parameter binding, and validation is what makes that safe rather than
hand-escaped -- but it has a consequence worth stating plainly: **a workspace file whose name
contains a quote cannot be addressed by the Knowledge Graph at all.** Unix permits such names,
so this is an operational constraint of the agent workspace, and it is a deliberate trade:
refusing a strange name costs one unaddressable file, while permitting it would mean escaping
user text into SQL, which is how injection bugs are written. Callers must tolerate the refusal
-- :func:`storage.knowledge_sync.register_artifact_entities` skips such a path and still records
its siblings, so a badly named file cannot block a task's completion.

Standard library only: ``dataclasses``, ``re``, ``urllib.parse``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Optional
from urllib.parse import urlparse

SCHEME: Final[str] = "kernel"
PREFIX: Final[str] = "kernel://"

ENTITY_SYMBOL: Final[str] = "symbol"
ENTITY_RULE: Final[str] = "rule"
ENTITY_TASK: Final[str] = "task"
ENTITY_FILE: Final[str] = "file"

# The four kinds a URI may name. The database's CHECK constraint lists the same four, so the
# parser and the schema cannot drift about what an entity is.
ENTITY_TYPES: Final[frozenset] = frozenset({ENTITY_SYMBOL, ENTITY_RULE, ENTITY_TASK, ENTITY_FILE})

# A symbol name: an identifier, optionally dotted for a qualified name (``Greeter.bye``). The
# same shape ``tools/ast_editor`` resolves against, so a URI can name a node the editor can find.
_SYMBOL_RE: Final = r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"

# A rule or task id: exactly one path segment. ``task-104``, ``chokehold_refuse_overwrite``.
_ID_RE: Final = r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$"

# Characters that cannot appear in a path component: whitespace (it would not survive a
# shell or a copy-paste), the three that ``urlparse`` treats as structure, ``;`` (a parameter
# separator in some parsers), ``%`` (a percent-escape, which is a second spelling of one path
# *and* a decoded traversal waiting to happen: ``%2e%2e`` is ``..`` to whoever unquotes it
# later), and the quote characters.
#
# The quotes matter most, and the cost is real: the vector store interpolates a URI into a
# LanceDB ``where`` clause, which has no parameter binding, and it relies on *this* validation
# instead of hand-escaping. A path that could contain ``'`` would close the string literal and
# the store's safety would be a claim rather than a property. The price is that a file whose
# name contains a quote cannot be addressed -- see the module docstring.
_FORBIDDEN_PATH_CHARS: Final = "?#;%'\""


class InvalidKnowledgeURIError(ValueError):
    """A string is not a well-formed ``kernel://`` address."""


@dataclass(frozen=True)
class KnowledgeURI:
    """A parsed address. Frozen: a URI is a key, and a mutated key is a lost entity."""

    scheme: str
    entity_type: str
    path: str
    symbol_name: Optional[str]
    raw_uri: str

    @property
    def is_symbol(self) -> bool:
        return self.entity_type == ENTITY_SYMBOL


def normalise_path(file_path: str) -> str:
    """The one spelling of a workspace-relative path a URI may hold.

    Backslashes become slashes, so a Windows caller and a POSIX caller address the same file
    identically. An absolute path, a ``..`` segment, an empty segment, whitespace and the
    structural characters ``?`` and ``#`` are all refused: this string is used to reach a file,
    so a traversal in it is a security boundary, not a formatting preference.
    """
    text = str(file_path or "").strip().replace("\\", "/")
    if not text:
        raise InvalidKnowledgeURIError("a path is required")
    if text.startswith("/"):
        raise InvalidKnowledgeURIError(
            f"a path must be workspace-relative, not absolute: {file_path!r}"
        )
    segments = text.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise InvalidKnowledgeURIError(
            f"a path may not contain empty, '.' or '..' segments: {file_path!r}"
        )
    if any(character.isspace() for character in text) or any(
        character in text for character in _FORBIDDEN_PATH_CHARS
    ):
        raise InvalidKnowledgeURIError(
            f"a path may not contain whitespace or any of {_FORBIDDEN_PATH_CHARS!r}: {file_path!r}"
        )
    return text


def _require_symbol(symbol_name: str) -> str:
    symbol = str(symbol_name or "").strip()
    import re

    if not re.match(_SYMBOL_RE, symbol):
        raise InvalidKnowledgeURIError(f"not a symbol name: {symbol_name!r}")
    return symbol


def _require_id(identifier: str, kind: str) -> str:
    import re

    value = str(identifier or "").strip()
    if not re.match(_ID_RE, value):
        raise InvalidKnowledgeURIError(f"not a {kind} id: {identifier!r}")
    return value


def make_symbol_uri(file_path: str, symbol_name: str) -> str:
    """``kernel://symbol/<file_path>:<symbol_name>`` for a node the AST editor can resolve."""
    return f"{PREFIX}{ENTITY_SYMBOL}/{normalise_path(file_path)}:{_require_symbol(symbol_name)}"


def make_rule_uri(rule_id: str) -> str:
    """``kernel://rule/<rule_id>`` for an invariant the engine enforces."""
    return f"{PREFIX}{ENTITY_RULE}/{_require_id(rule_id, 'rule')}"


def make_task_uri(task_id: str) -> str:
    """``kernel://task/<task_id>`` for a milestone in the plan DAG."""
    return f"{PREFIX}{ENTITY_TASK}/{_require_id(task_id, 'task')}"


def make_file_uri(file_path: str) -> str:
    """``kernel://file/<file_path>`` for a workspace file."""
    return f"{PREFIX}{ENTITY_FILE}/{normalise_path(file_path)}"


def parse_uri(uri_string: str) -> KnowledgeURI:
    """Parse a ``kernel://`` address, or raise :class:`InvalidKnowledgeURIError`.

    The scheme is checked against the *raw* string before ``urlparse`` sees it, because
    ``urlparse`` lowercases a scheme: ``KERNEL://`` would otherwise be silently accepted as the
    same scheme, and two spellings of one key is exactly what this module exists to prevent.
    """
    raw = str(uri_string or "")
    if not raw.startswith(PREFIX):
        raise InvalidKnowledgeURIError(f"a URI must start with {PREFIX!r}: {uri_string!r}")

    parsed = urlparse(raw)
    if parsed.scheme != SCHEME:
        raise InvalidKnowledgeURIError(f"unexpected scheme {parsed.scheme!r}: {uri_string!r}")
    if parsed.params or parsed.query or parsed.fragment:
        raise InvalidKnowledgeURIError(
            f"a URI may not carry parameters, a query or a fragment: {uri_string!r}"
        )

    entity_type = parsed.netloc
    if entity_type not in ENTITY_TYPES:
        raise InvalidKnowledgeURIError(
            f"unknown entity type {entity_type!r}; expected one of {sorted(ENTITY_TYPES)}"
        )

    body = parsed.path
    if not body.startswith("/"):
        raise InvalidKnowledgeURIError(f"a URI needs a path after the entity type: {uri_string!r}")
    body = body[1:]
    if not body:
        raise InvalidKnowledgeURIError(f"a URI needs a path after the entity type: {uri_string!r}")

    if entity_type == ENTITY_SYMBOL:
        path_text, separator, symbol = body.rpartition(":")
        if not separator or not path_text or not symbol:
            raise InvalidKnowledgeURIError(
                f"a symbol URI must be <file_path>:<symbol_name>: {uri_string!r}"
            )
        return KnowledgeURI(
            scheme=SCHEME,
            entity_type=ENTITY_SYMBOL,
            path=normalise_path(path_text),
            symbol_name=_require_symbol(symbol),
            raw_uri=raw,
        )

    if entity_type in (ENTITY_RULE, ENTITY_TASK):
        # Exactly one segment: a rule or task id is a name, not a path.
        if "/" in body:
            raise InvalidKnowledgeURIError(
                f"a {entity_type} URI names one id, not a path: {uri_string!r}"
            )
        return KnowledgeURI(
            scheme=SCHEME,
            entity_type=entity_type,
            path=_require_id(body, entity_type),
            symbol_name=None,
            raw_uri=raw,
        )

    return KnowledgeURI(
        scheme=SCHEME,
        entity_type=ENTITY_FILE,
        path=normalise_path(body),
        symbol_name=None,
        raw_uri=raw,
    )
