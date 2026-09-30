"""Keeping the two halves of the Knowledge Graph in step: SQLite and LanceDB.

The relational graph and the vector index are two stores with no shared transaction. SQLite
cannot enlist LanceDB, so the honest question is not "how do we make them atomic" but "what
must be true so that a rollback cannot leave an orphan vector".

The answer here is an ordering rule, enforced rather than documented:

1. **Buffer, don't write.** :meth:`KnowledgeSync.queue` records what an entity should be
   embedded from and does **no I/O**. The caller is mid-transaction at that point.
2. **Gate the write on the commit.** :meth:`KnowledgeSync.flush` re-reads
   ``knowledge_entities`` and pushes only the URIs that are actually **there**. A rolled-back
   entity is not in the table, so its vector is never written -- the rollback is honoured
   because the vector write is derived from committed state, not from intent.
3. **Reconcile the remainder.** A crash between the commit and the flush leaves the opposite
   problem (an entity with no vector). :meth:`KnowledgeSync.reconcile` removes index rows whose
   entity is gone and reports entities that are still missing one, so the drift is visible
   rather than silent.

Nothing here opens or commits a transaction: the caller owns it. That is what lets a graph
write and a vector write be sequenced around one commit instead of two.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from storage.vector_store import EntityVectorStore, TextEmbedder


def entity_text(name: str, metadata: Optional[Dict[str, Any]] = None, uri: str = "") -> str:
    """The text an entity is embedded from: its name, its URI and its metadata.

    The URI is included because it carries the kind and the path (``kernel://symbol/app.py:run``),
    which is real signal -- a query mentioning ``app.py`` should reach the symbol inside it. The
    metadata is flattened rather than dumped as JSON so the tokens are words, not punctuation.
    """
    parts: List[str] = [str(name or "").strip()]
    if uri:
        parts.append(str(uri))
    if metadata:
        parts.extend(f"{key} {value}" for key, value in sorted(metadata.items()))
    return " ".join(part for part in parts if part).strip()


def register_artifact_entities(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    task_id: str,
    title: str,
    artifact: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Record the entities an applied artifact touched, and the edges between them.

    This is the live ingestion hook: the graph is populated by work that actually landed, not
    by a separate indexing pass that can fall behind it. A task entity, one entity per target
    file, and a ``modifies`` edge from the task to each file -- which is the relation a later
    query needs to answer "what touched this file".

    Every write goes through :func:`storage.graph_engine.upsert_entity` and
    :func:`~storage.graph_engine.add_synapse`, so the addresses are validated by the same
    parser the retriever queries with. The caller owns the transaction; this returns the URIs
    it registered, in insertion order, so the caller can queue them for the vector index.
    """
    from storage.graph_engine import add_synapse, upsert_entity
    from storage.knowledge_uri import make_file_uri, make_task_uri

    registered: List[str] = []
    task_uri = make_task_uri(str(task_id))
    upsert_entity(conn, task_uri, str(title or task_id), "task", {"plan_id": str(plan_id)})
    registered.append(task_uri)

    seen: set = set()
    for target in ((artifact or {}).get("ast_targets") or []):
        path = str(target.get("file_path") or "").strip()
        if not path:
            continue
        try:
            file_uri = make_file_uri(path)
        except Exception:
            # A target naming a path the URI scheme refuses (a traversal, an absolute path) is
            # skipped rather than aborting the ingestion: the artifact applied, and the graph
            # should record what it can rather than nothing.
            continue
        if file_uri not in seen:
            upsert_entity(conn, file_uri, path, "file", {})
            registered.append(file_uri)
            seen.add(file_uri)
        add_synapse(conn, task_uri, file_uri, "modifies")
    return registered


def register_task_entities(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    task_id: str,
    title: str,
    files: Optional[List[str]] = None,
) -> List[str]:
    """Record a task and its declared deliverables, without an artifact.

    Used on the planning half, so a task is in the graph from the moment it is proposed rather
    than only once something has been applied to it.
    """
    return register_artifact_entities(
        conn,
        plan_id=plan_id,
        task_id=task_id,
        title=title,
        artifact={"ast_targets": [{"file_path": name} for name in (files or [])]},
    )


class KnowledgeSync:
    """Buffers vector writes for queued entities and applies them after a commit."""

    def __init__(self, store: EntityVectorStore, embedder: TextEmbedder):
        self._store = store
        self._embedder = embedder
        self._pending: Dict[str, str] = {}

    # -- buffering ----------------------------------------------------------------
    def queue(self, uri: str, text: Optional[str] = None, *, name: str = "",
              metadata: Optional[Dict[str, Any]] = None) -> None:
        """Record what an entity should be embedded from. Performs no I/O.

        Re-queueing the same URI replaces the pending text: the last write in a transaction is
        the one that will be committed, so it is the one that should be embedded.
        """
        resolved = text if text is not None else entity_text(name, metadata, uri)
        self._pending[str(uri)] = resolved

    @property
    def pending(self) -> Tuple[str, ...]:
        """The URIs waiting to be pushed, in queue order."""
        return tuple(self._pending)

    def clear(self) -> None:
        self._pending.clear()

    # -- applying -----------------------------------------------------------------
    def flush(self, conn: sqlite3.Connection) -> int:
        """Embed and push every pending entity that the database actually holds.

        Returns how many vectors were written. Pending URIs that are not in
        ``knowledge_entities`` -- the rolled-back ones -- are dropped, which is the whole
        guarantee: a vector is only ever written for state that committed.
        """
        if not self._pending:
            return 0

        candidates = list(self._pending)
        placeholders = ",".join("?" for _ in candidates)
        present = {
            str(row["id"])
            for row in conn.execute(
                f"SELECT id FROM knowledge_entities WHERE id IN ({placeholders})", candidates
            ).fetchall()
        }

        committed = {uri: text for uri, text in self._pending.items() if uri in present}
        self._pending = {uri: text for uri, text in self._pending.items() if uri not in present}
        if not committed:
            return 0

        uris = list(committed)
        vectors = self._embedder.encode([committed[uri] for uri in uris])
        records = [
            {"uri": uri, "text_content": committed[uri], "vector": vectors[index]}
            for index, uri in enumerate(uris)
        ]
        return self._store.upsert_many(records)

    def reconcile(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """Report and repair drift between the two stores.

        Returns ``{"orphaned": [...], "missing": [...]}``: ``orphaned`` were removed from the
        index because their entity no longer exists, ``missing`` are entities with no vector
        (a crash between commit and flush, or an index that was never built).
        """
        indexed = self._store.existing_uris()
        known = {str(row["id"]) for row in conn.execute("SELECT id FROM knowledge_entities").fetchall()}

        orphaned = sorted(indexed - known)
        if orphaned:
            self._store.delete(orphaned)

        return {"orphaned": orphaned, "missing": sorted(known - indexed)}

    def reindex(self, conn: sqlite3.Connection, uris: Optional[List[str]] = None) -> int:
        """Rebuild vectors from committed state, for a set of URIs or the whole graph.

        The recovery path for ``reconcile``'s ``missing`` list: it reads the entities back out
        of SQLite, so it cannot invent an entity that is not there.
        """
        if uris is None:
            rows = conn.execute(
                "SELECT id, name, metadata_json FROM knowledge_entities"
            ).fetchall()
        else:
            if not uris:
                return 0
            placeholders = ",".join("?" for _ in uris)
            rows = conn.execute(
                f"SELECT id, name, metadata_json FROM knowledge_entities WHERE id IN ({placeholders})",
                [str(uri) for uri in uris],
            ).fetchall()

        payload = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            payload.append({
                "uri": str(row["id"]),
                "text_content": entity_text(row["name"], metadata, str(row["id"])),
            })
        if not payload:
            return 0

        vectors = self._embedder.encode([record["text_content"] for record in payload])
        records = [
            {**record, "vector": vectors[index]} for index, record in enumerate(payload)
        ]
        return self._store.upsert_many(records)
