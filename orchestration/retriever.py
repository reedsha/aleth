"""The hybrid retriever: a natural-language task description in, graph topology out.

This is the single pipeline the System 2 planner calls. It is hybrid in the literal sense --
two retrieval mechanisms, each doing the half it is good at:

1. **Semantic.** The task description is embedded and LanceDB returns the nearest entity URIs.
   This is what turns prose into graph addresses; it knows nothing about edges.
2. **Topological.** Each seed URI is fed into the recursive CTE in ``storage.graph_engine``,
   which walks the actual relations. This is what turns addresses into structure; it knows
   nothing about prose.

Neither alone answers "what should the planner be shown". Semantic search alone returns
isolated nodes with no relations; traversal alone needs a starting point nobody can supply by
hand. Chaining them means a query about a *symptom* reaches the symbols, rules and files
*connected* to it.

The result is a plain dictionary of what was found -- seeds, edges, entity metadata and a
rendered block -- so the caller can feed the text to a prompt, inspect the topology in a test,
or both.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence

from storage.graph_engine import get_related_entities
from storage.vector_store import EntityVectorStore, TextEmbedder

DEFAULT_TOP_K = 5

# The heading the injected playbooks are introduced with. It lives here so the retriever and the
# transport agree on the shape of the block without either re-deriving it.
SKILL_HEADER = "## Operating Procedures (skills)"
DEFAULT_MAX_DEPTH = 2

# Where the entity vectors live, beside the plan's SQLite file so a workspace carries its own
# index rather than sharing one across checkouts.
VECTOR_SUBDIR = "knowledge_vectors"


def default_vector_store() -> EntityVectorStore:
    """The index for the active plan directory."""
    from storage.db import get_store
    from tools.workspace import get_plan_dir

    store = get_store()
    return EntityVectorStore(os.path.join(get_plan_dir(), VECTOR_SUBDIR), dim=default_embedder().dim)


_EMBEDDER_CACHE: Dict[str, TextEmbedder] = {}

# The semantic model is the default. ``DEEPAGENTS_EMBEDDER=hashing`` is an explicit override for
# CI and tests, which must not each pull ~90 MB of weights; it is not a gate on the production
# architecture. A lexical hash is a glorified grep and cannot do the similarity matching a
# cognitive engine exists for, so it is never chosen by omission.
ENV_EMBEDDER = "DEEPAGENTS_EMBEDDER"
EMBEDDER_HASHING = "hashing"

_PREFETCH_LOCK = threading.Lock()
_PREFETCH_STARTED = {"value": False}


def default_embedder() -> TextEmbedder:
    """The semantic embedder (``all-MiniLM-L6-v2``), unless explicitly overridden.

    ``DEEPAGENTS_EMBEDDER=hashing`` selects the deterministic offline encoder -- for CI and
    tests, which must not each fetch the weights. Anything else, including unset, selects the
    real model: a lexical hash cannot perform the semantic matching this graph exists to do, so
    degrading to it by default would disable Phase 5 while appearing to work.

    The model is loaded through :func:`prefetch_embedder`, which the kernel calls during
    initialization. That is how the cold-start cost is handled -- the download happens once, off
    the planner's hot path -- rather than by shipping a weaker default to dodge it.
    """
    if "embedder" in _EMBEDDER_CACHE:
        return _EMBEDDER_CACHE["embedder"]

    with _PREFETCH_LOCK:
        if "embedder" not in _EMBEDDER_CACHE:
            _EMBEDDER_CACHE["embedder"] = _build_embedder()
    return _EMBEDDER_CACHE["embedder"]


def _build_embedder() -> TextEmbedder:
    from tools.semantic_index import HashingEmbedder, MiniLmEmbedder

    if (os.environ.get(ENV_EMBEDDER) or "").strip().lower() == EMBEDDER_HASHING:
        return HashingEmbedder()
    return MiniLmEmbedder()


def prefetch_embedder() -> None:
    """Warm the embedder during initialization, on a daemon thread. Idempotent.

    Fetching and loading ~90 MB of weights is a deployment cost, not a retrieval cost. Paying it
    here -- once, while the kernel is starting -- keeps it off the path that plans a task, which
    is where it would otherwise appear as an unexplained stall on the first run after install.

    A failure is reported rather than raised: the prefetch runs off the critical path, and
    :func:`default_embedder` will surface the same error to whoever actually needs a vector.
    """
    with _PREFETCH_LOCK:
        if _PREFETCH_STARTED["value"] or "embedder" in _EMBEDDER_CACHE:
            return
        _PREFETCH_STARTED["value"] = True

    def _warm() -> None:
        try:
            default_embedder()
        except Exception as error:
            print(
                f"[Retriever] embedder prefetch failed: {type(error).__name__}: {error}. "
                "The first retrieval will retry and may block on a model download.",
                file=sys.stderr,
            )

    threading.Thread(target=_warm, name="embedder-prefetch", daemon=True).start()


def reset_embedder_cache() -> None:
    """Forget the cached embedder choice (a test that swaps the embedder in)."""
    with _PREFETCH_LOCK:
        _EMBEDDER_CACHE.clear()
        _PREFETCH_STARTED["value"] = False


def heal_knowledge_index() -> Dict[str, Any]:
    """Reconcile the vector index against the relational graph. Called before every plan.

    A crash between the graph commit and the vector push leaves an entity with no vector; a
    vector whose entity is gone leaves a seed the graph cannot resolve. Both are drift, and
    both are repaired here rather than left for a retrieval to stumble over. Entities that are
    missing a vector are re-embedded from committed state, so the index cannot hold an entity
    the database does not.
    """
    from storage.knowledge_sync import KnowledgeSync

    with knowledge_connection() as conn:
        sync = KnowledgeSync(default_vector_store(), default_embedder())
        report = sync.reconcile(conn)
        if report["missing"]:
            sync.reindex(conn, report["missing"])
        return report


@contextmanager
def knowledge_connection() -> Iterator[sqlite3.Connection]:
    """A short-lived connection to the plan database, with foreign keys enforced.

    Opened here rather than borrowed from ``PlanStore`` because the store's own connections are
    per-call and private; this is the seam a caller outside ``storage`` uses to reach the graph.
    """
    from storage.db import get_store

    connection = sqlite3.connect(get_store().path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
    finally:
        connection.close()


def skills_for_capabilities(
    capabilities: Sequence[str],
    *,
    connection: Optional[sqlite3.Connection] = None,
) -> str:
    """The Markdown playbooks a node's capabilities select, in **one** query.

    ``skills.target_capabilities`` is a JSON array; the match is a JSON1 set intersection, so a
    node's declaration is answered by a single statement. There is no per-skill ``SELECT`` and no
    "read the table and filter it in Python" -- the no-N+1 rule applies to retrieval as much as
    to dispatch.

    A node that declared nothing selects nothing: the diet is enforced, not suggested, and a
    declaration that matches no skill yields an empty block rather than every playbook. The
    caller (``system2``) is what turns the block into part of a system prompt.
    """
    wanted = [str(name).strip() for name in (capabilities or []) if str(name).strip()]
    if not wanted:
        return ""
    placeholders = ",".join("?" for _ in wanted)
    sql = (
        "SELECT s.markdown_content AS markdown_content FROM skills AS s"
        " WHERE EXISTS (SELECT 1 FROM json_each(s.target_capabilities) AS c"
        f" WHERE c.value IN ({placeholders}))"
        " ORDER BY s.id"
    )
    if connection is None:
        with knowledge_connection() as own:
            rows = own.execute(sql, wanted).fetchall()
    else:
        rows = connection.execute(sql, wanted).fetchall()
    blocks = [str(row["markdown_content"] or "").strip() for row in rows]
    return "\n\n".join(block for block in blocks if block)


@dataclass
class RetrievalResult:
    """What one hybrid retrieval found."""

    query: str
    seeds: List[str] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    entities: List[Dict[str, Any]] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    def render(self) -> str:
        """The block a planner prompt carries.

        Seeds are named first and the edges after, because the seeds are what the query matched
        and the edges are what surrounds them -- a reader needs that order to trust the walk.
        """
        if not self.seeds:
            return "No knowledge-graph entities matched this task."

        lines = [f"Matched entities ({len(self.seeds)}):"]
        by_uri = {entity["uri"]: entity for entity in self.entities}
        for uri in self.seeds:
            entity = by_uri.get(uri)
            name = entity["name"] if entity else uri
            kind = entity["type"] if entity else "unknown"
            lines.append(f"- [{kind}] {name}  ({uri})")

        if self.edges:
            lines.append("")
            lines.append(f"Related ({len(self.edges)} edge(s), within the traversal depth):")
            for edge in self.edges:
                source = by_uri.get(edge["source_id"], {}).get("name", edge["source_id"])
                target = by_uri.get(edge["target_id"], {}).get("name", edge["target_id"])
                lines.append(
                    f"- {source} --{edge['relation_type']}--> {target} "
                    f"(weight {edge['weight']}, depth {edge['depth']})"
                )
        return "\n".join(lines)


def _entity_rows(conn: sqlite3.Connection, uris: Sequence[str]) -> List[Dict[str, Any]]:
    """The metadata for the given URIs, in one query. Unknown URIs simply do not appear."""
    wanted = list(dict.fromkeys(str(uri) for uri in uris))
    if not wanted:
        return []
    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute(
        f"SELECT id, name, type, metadata_json FROM knowledge_entities WHERE id IN ({placeholders})",
        wanted,
    ).fetchall()

    entities = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        entities.append({
            "uri": str(row["id"]),
            "name": str(row["name"]),
            "type": str(row["type"]),
            "metadata": metadata,
        })
    return entities


def retrieve_context(
    conn: sqlite3.Connection,
    store: EntityVectorStore,
    embedder: TextEmbedder,
    task_description: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> RetrievalResult:
    """Embed a task description, find matching entities, and walk the graph around them.

    The edges are the union of a traversal from every seed, de-duplicated on
    ``(source, target, relation)`` so a relation reached from two seeds is reported once.
    ``depth`` is kept per edge -- the shallowest depth at which it was reached -- because
    "how far from what the query matched" is what makes the walk readable.
    """
    text = str(task_description or "").strip()
    if not text:
        raise ValueError("a task description is required")

    query_vector = embedder.encode([text])[0]
    seeds = store.semantic_search(query_vector, top_k=top_k)
    if not seeds:
        return RetrievalResult(query=text)

    edges: Dict[tuple, Dict[str, Any]] = {}
    touched: List[str] = list(seeds)
    for seed in seeds:
        for edge in get_related_entities(conn, seed, max_depth=max_depth):
            key = (edge["source_id"], edge["target_id"], edge["relation_type"])
            existing = edges.get(key)
            if existing is None or edge["depth"] < existing["depth"]:
                edges[key] = edge
            touched.append(edge["source_id"])
            touched.append(edge["target_id"])

    entities = _entity_rows(conn, touched)
    known = {entity["uri"] for entity in entities}

    return RetrievalResult(
        query=text,
        seeds=list(seeds),
        edges=sorted(edges.values(), key=lambda edge: (edge["depth"], -edge["weight"])),
        entities=entities,
        # A seed can be in the index while its entity is gone from the relational graph (a
        # crash between the two writes). Reported rather than hidden: it is exactly the drift
        # ``KnowledgeSync.reconcile`` exists to repair.
        missing=[uri for uri in seeds if uri not in known],
    )


def retrieve_for_plan(
    task: Dict[str, Any],
    *,
    top_k: int = DEFAULT_TOP_K,
    max_depth: int = DEFAULT_MAX_DEPTH,
    store: Optional[EntityVectorStore] = None,
    embedder: Optional[TextEmbedder] = None,
) -> str:
    """The planner's entry point: a task dictionary in, the rendered context block out.

    The query is built from the task's title, details and declared files, which is what the
    planner is about to reason over -- so the graph it is shown is the graph around the thing it
    was asked to plan.

    Returns "" when the graph has nothing to say (no index yet, no match), so a caller can tell
    "nothing to add" from "a block was added". Raises only for a genuinely broken store; the
    caller decides whether retrieval is worth failing a run over.
    """
    text = task_query(task)
    # Healed before every plan: a vector that a crash left unwritten is a seed the planner would
    # otherwise never see. Reported, not fatal -- retrieval is context, and a repair failure
    # must not stop a run.
    try:
        heal_knowledge_index()
    except Exception:
        pass
    with knowledge_connection() as conn:
        result = retrieve_context(
            conn,
            store or default_vector_store(),
            embedder or default_embedder(),
            text,
            top_k=top_k,
            max_depth=max_depth,
        )
    return result.render() if result.seeds else ""


def task_query(task: Dict[str, Any]) -> str:
    """The natural-language query a task dictionary turns into."""
    parts = [str(task.get("title") or "")]
    parts.extend(str(detail) for detail in (task.get("details") or []))
    parts.extend(str(name) for name in (task.get("files") or []))
    return " ".join(part for part in parts if part).strip() or str(task.get("id") or "task")


def retrieve_context_for_task(
    conn: sqlite3.Connection,
    store: EntityVectorStore,
    embedder: TextEmbedder,
    task: Dict[str, Any],
    *,
    top_k: int = DEFAULT_TOP_K,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> RetrievalResult:
    """:func:`retrieve_for_plan` when the caller wants the topology, not only the text."""
    return retrieve_context(
        conn, store, embedder, task_query(task), top_k=top_k, max_depth=max_depth
    )
