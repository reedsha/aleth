"""The vector half of the Knowledge Graph: an embedded LanceDB index of entities.

LanceDB runs **in-process** and stores its tables in a directory, so there is no daemon to
start, supervise or lose. This module owns one table: ``uri`` (the exact ``kernel://`` id),
``text_content`` (what was embedded) and ``vector`` (float32).

Why the id is the key and the text is kept: a vector row is meaningless without the entity it
belongs to, and keeping the text makes the index auditable -- a hit can be read, not just
scored. The URI is the join back to the relational graph, and it is the *same string* the
parser produced, so the two halves of the graph cannot disagree about what an entity is.

Embedding is **not** done here. This module takes vectors; a caller with an embedder supplies
them. That keeps ``storage`` free of any dependency on ``tools`` and keeps one embedder
contract in the codebase (``tools.semantic_index.Embedder``) rather than two.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence

import lancedb
import numpy as np
import pyarrow as pa

from storage.knowledge_uri import parse_uri

DEFAULT_TABLE = "knowledge_vectors"


class TextEmbedder(Protocol):
    """The contract this module needs from an embedder. Duck-typed on purpose.

    ``tools.semantic_index.Embedder`` satisfies it, and so does the offline
    ``HashingEmbedder`` the tests run on -- so a caller may supply either without ``storage``
    importing ``tools``.
    """

    @property
    def dim(self) -> int:
        """The vector length this embedder produces."""

    def encode(self, texts: Sequence[str]) -> Any:
        """A ``(len(texts), dim)`` float array."""


def _validated(uri: str) -> str:
    """The canonical URI, or a refusal.

    LanceDB has no parameter binding for a ``where`` clause, so a URI reaches the query builder
    as text. The safety does **not** come from escaping it -- hand-rolled quote and backslash
    replacement is how injection bugs are written. It comes from the address being *parsed*:
    a valid ``kernel://`` URI cannot contain a quote, a backslash, whitespace, ``?``, ``#``,
    ``;`` or ``%``, so a well-formed one cannot close a string literal. An address that fails
    validation is refused here, before any query exists.

    The returned value is ``raw_uri`` -- the exact string the parser accepted -- so the stored
    key is canonical rather than merely sanitised.
    """
    return parse_uri(uri).raw_uri


class EntityVectorStore:
    """One LanceDB table of entity vectors, keyed by ``kernel://`` URI."""

    def __init__(self, db_path: str, dim: int, table_name: str = DEFAULT_TABLE):
        if dim <= 0:
            raise ValueError("dim must be positive")
        self._path = str(db_path)
        self._dim = int(dim)
        self._table_name = table_name
        Path(self._path).mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(self._path)

    @property
    def dim(self) -> int:
        return self._dim

    def _schema(self) -> pa.Schema:
        return pa.schema([
            pa.field("uri", pa.string()),
            pa.field("text_content", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), self._dim)),
        ])

    def _table(self):
        """The table, or ``None`` when nothing has been written yet."""
        try:
            return self._db.open_table(self._table_name)
        except Exception:
            return None

    # -- writes -------------------------------------------------------------------
    def _rows(self, records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows = []
        for record in records:
            vector = np.asarray(record["vector"], dtype=np.float32).reshape(-1)
            if vector.shape != (self._dim,):
                raise ValueError(
                    f"vector for {record.get('uri')!r} has shape {vector.shape}, "
                    f"expected ({self._dim},)"
                )
            rows.append({
                "uri": _validated(record["uri"]),
                "text_content": str(record.get("text_content") or ""),
                "vector": vector.tolist(),
            })
        return rows

    def upsert_vector(self, uri: str, text_content: str, vector: Sequence[float]) -> None:
        """Insert or replace one entity's vector."""
        self.upsert_many([{"uri": uri, "text_content": text_content, "vector": vector}])

    def upsert_many(self, records: Iterable[Dict[str, Any]]) -> int:
        """Insert or replace many. Returns how many rows were written.

        Replace, not append: the URI is the identity, and a second row for one entity would
        make the index return a node twice and drift from the relational graph. The delete and
        the add are two operations rather than one ``merge_insert`` so the behaviour does not
        depend on which LanceDB upsert API a given version exposes.
        """
        rows = self._rows(records)
        if not rows:
            return 0

        table = self._table()
        if table is None:
            self._db.create_table(self._table_name, data=rows, schema=self._schema())
            return len(rows)

        predicate = " OR ".join(f"uri = '{row['uri']}'" for row in rows)
        table.delete(predicate)
        table.add(rows)
        return len(rows)

    def delete(self, uris: Iterable[str]) -> int:
        """Remove the given URIs. Returns the number asked for, so a caller can log the intent."""
        wanted = [_validated(uri) for uri in uris]
        table = self._table()
        if table is None or not wanted:
            return 0
        predicate = " OR ".join(f"uri = '{uri}'" for uri in wanted)
        table.delete(predicate)
        return len(wanted)

    # -- reads --------------------------------------------------------------------
    def semantic_search(self, query_vector: Sequence[float], top_k: int = 5) -> List[str]:
        """The URIs nearest ``query_vector``, best first.

        Cosine distance, so a vector's magnitude does not decide the ranking. An empty or
        absent table is an ordinary answer -- no entities have been indexed yet -- not an error.
        """
        if top_k < 1:
            raise ValueError("top_k must be at least 1")

        # Validated before the table is even looked at: a malformed query vector is a caller's
        # bug, and reporting it only when the index happens to be non-empty would hide it.
        vector = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        if vector.shape != (self._dim,):
            raise ValueError(f"query vector has shape {vector.shape}, expected ({self._dim},)")

        table = self._table()
        if table is None:
            return []

        rows = (
            table.search(vector.tolist())
            .distance_type("cosine")
            .limit(int(top_k))
            .to_list()
        )
        return [str(row["uri"]) for row in rows]

    def existing_uris(self) -> set:
        """Every URI the index holds. Used to reconcile it against the relational graph."""
        table = self._table()
        if table is None:
            return set()
        return {str(row["uri"]) for row in table.to_arrow().to_pylist()}

    def count(self) -> int:
        table = self._table()
        return 0 if table is None else int(table.count_rows())

    def text_for(self, uri: str) -> Optional[str]:
        """The text an entity was embedded from, or ``None`` when it is not indexed."""
        table = self._table()
        if table is None:
            return None
        rows = table.search().where(f"uri = '{_validated(uri)}'").limit(1).to_list()
        return str(rows[0]["text_content"]) if rows else None
