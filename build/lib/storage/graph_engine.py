"""The embedded graph engine: transactional upserts and multi-hop traversal over SQLite.

There is no graph database here and no daemon. The Knowledge Graph is two relational tables
(``storage/db.py``) and the traversal is a **recursive CTE** executed by the same SQLite
connection that owns the rest of the engine's state. That is the whole design: the graph is
part of the state kernel, so a synapse write and a plan write can be one transaction, and
there is nothing extra to run, supervise or lose.

Two invariants this module enforces rather than assumes:

* **Every address is validated.** A URI is a primary key, so a malformed one is not a
  formatting problem -- it is a second entity for something that already exists. Both
  ``upsert_entity`` and ``add_synapse`` parse their arguments and raise
  :class:`~storage.knowledge_uri.InvalidKnowledgeURIError` rather than storing a bad key.
* **The declared kind must match the address.** ``upsert_entity`` refuses a ``type`` argument
  that disagrees with the entity type inside the URI. A row whose ``type`` and ``id`` disagree
  is findable by neither the queries that filter on kind nor the ones that filter on id.

Every function operates on a **caller's** ``sqlite3.Connection``: this module never opens one,
never commits, and never closes. The caller owns the transaction, which is what lets a graph
write join a larger unit of work.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from storage.knowledge_uri import InvalidKnowledgeURIError, parse_uri

# The four relation kinds the schema's CHECK constraint allows. Checked here too so the caller
# gets a sentence rather than an IntegrityError; the constraint remains the backstop.
RELATION_TYPES = frozenset({"calls", "modifies", "enforces", "invalidates"})

# Traversal is bounded by depth, and the depth is bounded by this. A recursive CTE with no
# ceiling is a way to turn a small graph into a very long query.
MAX_DEPTH_LIMIT = 32

# The recursive traversal, run **both ways**.
#
# A dependency graph that only looks downstream is useless to a planner: if it changes a core
# utility it must also see every higher-level module that calls it, or it flies blind into the
# upstream breakage. So there are two edge-tracking CTEs -- ``Forward`` anchors on
# ``source_id = seed`` and walks out, ``Backward`` anchors on ``target_id = seed`` and walks in
# -- and the result is their union. The caller gets what the seed depends on *and* what depends
# on the seed, each with its hop distance.
#
# ``edges_visited`` carries the chain of **edges** walked so far, each wrapped in ``|``
# delimiters, and each CTE tests the next edge against its own chain:
#
# * **Exactness.** The guard is a substring test, so it has to be delimiter-anchored:
#   ``|kernel://file/a.py|`` is not a substring of ``|kernel://file/a.pyc|``, whereas the bare
#   URIs collide and would prune a real edge into ``a.py`` as "already visited".
# * **Termination in both directions.** Each CTE admits an edge at most once, so each walk is
#   bounded by the edge count of a finite graph. A cycle therefore cannot recurse forever in
#   either direction, which is what makes the two-way walk safe.
#
# An edge can be found by *both* walks, at different depths (the hop that closes a ring is one
# step outbound and nine steps inbound). The final SELECT groups by the edge and keeps
# ``MIN(depth)``, so one relation is one row at its nearest distance, whichever direction found
# it first.
#
# Parameters, in order: seed, depth, seed, depth -- one pair per CTE.
_TRAVERSE_SQL = """
WITH RECURSIVE
Forward AS (
    SELECT
        s.source_id,
        s.target_id,
        s.relation_type,
        s.weight,
        1 AS depth,
        '|' || s.source_id || '->' || s.target_id || '|' AS edges_visited
    FROM knowledge_synapses s
    WHERE s.source_id = ?

    UNION ALL

    SELECT
        s.source_id,
        s.target_id,
        s.relation_type,
        s.weight,
        f.depth + 1,
        f.edges_visited || s.source_id || '->' || s.target_id || '|'
    FROM knowledge_synapses s
    JOIN Forward f ON s.source_id = f.target_id
    WHERE f.depth < ?
      AND f.edges_visited NOT LIKE '%|' || s.source_id || '->' || s.target_id || '|%'
),
Backward AS (
    SELECT
        s.source_id,
        s.target_id,
        s.relation_type,
        s.weight,
        1 AS depth,
        '|' || s.source_id || '->' || s.target_id || '|' AS edges_visited
    FROM knowledge_synapses s
    WHERE s.target_id = ?

    UNION ALL

    SELECT
        s.source_id,
        s.target_id,
        s.relation_type,
        s.weight,
        b.depth + 1,
        b.edges_visited || s.source_id || '->' || s.target_id || '|'
    FROM knowledge_synapses s
    JOIN Backward b ON s.target_id = b.source_id
    WHERE b.depth < ?
      AND b.edges_visited NOT LIKE '%|' || s.source_id || '->' || s.target_id || '|%'
)
SELECT
    source_id,
    target_id,
    relation_type,
    MAX(weight) AS weight,
    MIN(depth) AS depth
FROM (
    SELECT source_id, target_id, relation_type, weight, depth FROM Forward
    UNION ALL
    SELECT source_id, target_id, relation_type, weight, depth FROM Backward
)
GROUP BY source_id, target_id, relation_type
ORDER BY depth ASC, weight DESC;
"""


def upsert_entity(
    conn: sqlite3.Connection,
    uri: str,
    name: str,
    entity_type: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Insert or update one entity, keyed by its ``kernel://`` URI.

    ``uri`` is parsed first, so a malformed address raises
    :class:`InvalidKnowledgeURIError` and nothing is written. The parsed entity type must equal
    ``entity_type``: a row whose kind and address disagree is invisible to both ways of
    querying it.
    """
    parsed = parse_uri(uri)
    declared = str(entity_type or "").strip()
    if parsed.entity_type != declared:
        raise InvalidKnowledgeURIError(
            f"the URI names a {parsed.entity_type!r} but the entity_type argument is "
            f"{declared!r}: {uri!r}"
        )

    payload = json.dumps(metadata if metadata is not None else {})
    conn.execute(
        """
        INSERT INTO knowledge_entities (id, name, type, metadata_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            type = excluded.type,
            metadata_json = excluded.metadata_json
        """,
        (parsed.raw_uri, str(name or ""), declared, payload),
    )


def add_synapse(
    conn: sqlite3.Connection,
    source_uri: str,
    target_uri: str,
    relation_type: str,
    weight: float = 1.0,
) -> None:
    """Insert or update one directed edge.

    Both endpoints are parsed, so a malformed address is refused before the write. The triple
    ``(source, target, relation)`` is the identity, so asserting the same relation twice
    updates its weight rather than adding a duplicate edge.
    """
    source = parse_uri(source_uri)
    target = parse_uri(target_uri)

    relation = str(relation_type or "").strip()
    if relation not in RELATION_TYPES:
        raise ValueError(
            f"unknown relation {relation_type!r}; expected one of {sorted(RELATION_TYPES)}"
        )

    conn.execute(
        """
        INSERT INTO knowledge_synapses (source_id, target_id, relation_type, weight)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source_id, target_id, relation_type) DO UPDATE SET
            weight = excluded.weight
        """,
        (source.raw_uri, target.raw_uri, relation, float(weight)),
    )


def get_related_entities(
    conn: sqlite3.Connection,
    seed_uri: str,
    max_depth: int = 2,
) -> List[Dict[str, Any]]:
    """Every edge reachable from ``seed_uri`` within ``max_depth`` hops, in either direction.

    Returns one dictionary per edge -- ``source_id``, ``target_id``, ``relation_type``,
    ``weight``, ``depth`` -- ordered by depth then by descending weight, so the nearest and
    strongest relations are read first.

    **Both directions are walked.** An edge whose ``source_id`` is the seed is what the seed
    depends on; an edge whose ``target_id`` is the seed is what depends on the seed. A planner
    that could only see one of those would change a shared utility without learning who calls
    it. An edge reachable both ways is reported once.

    ``depth`` is the hop count from the seed in the direction it was found: ``1`` is directly
    attached, ``2`` is one hop beyond that. ``max_depth`` must be a positive integer; it is
    also capped at :data:`MAX_DEPTH_LIMIT`, because a recursive CTE with no ceiling turns a
    small graph into a very long query.
    """
    seed = parse_uri(seed_uri)

    # ``bool`` is an ``int`` in Python, and ``True`` is not a depth. Rejected explicitly rather
    # than silently treated as 1.
    if isinstance(max_depth, bool) or not isinstance(max_depth, int):
        raise ValueError(f"max_depth must be an integer, not {type(max_depth).__name__}")
    if max_depth < 1:
        raise ValueError(f"max_depth must be at least 1, not {max_depth}")
    depth = min(max_depth, MAX_DEPTH_LIMIT)

    rows = conn.execute(_TRAVERSE_SQL, (seed.raw_uri, depth, seed.raw_uri, depth)).fetchall()
    return [
        {
            "source_id": row["source_id"],
            "target_id": row["target_id"],
            "relation_type": row["relation_type"],
            "weight": float(row["weight"]),
            "depth": int(row["depth"]),
        }
        for row in rows
    ]
