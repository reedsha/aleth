"""Phase 5 Batch 2: the embedded graph engine over real in-memory SQLite.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_knowledge_graph.py -n0 -q

**No mocks anywhere.** Every test drives a real ``sqlite3`` connection built from the real
schema (``storage.db.apply_knowledge_graph_schema``), with ``PRAGMA foreign_keys=ON`` so the
cascading deletes the graph depends on actually happen. A mocked connection would prove that
the engine calls ``execute`` -- not that the SQL is correct, which is the only thing worth
knowing about a query engine.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from storage.db import apply_knowledge_graph_schema
from storage.graph_engine import (
    MAX_DEPTH_LIMIT,
    add_synapse,
    get_related_entities,
    upsert_entity,
)
from storage.knowledge_uri import InvalidKnowledgeURIError

FILE_A = "kernel://file/a.py"
FILE_B = "kernel://file/b.py"
FILE_C = "kernel://file/c.py"
FILE_D = "kernel://file/d.py"


@pytest.fixture()
def conn():
    """A real in-memory database with the real schema and foreign keys enforced."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    apply_knowledge_graph_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


def _file(conn, uri, name=None):
    upsert_entity(conn, uri, name or uri.rsplit("/", 1)[-1], "file")


# ---------------------------------------------------------------- URI validation integration


class TestUriValidation:
    def test_upsert_entity_rejects_a_malformed_uri(self, conn):
        for bad in ("not-a-uri", "kernel://widget/x", "kernel://file/../escape.py", "kernel://file/"):
            with pytest.raises(InvalidKnowledgeURIError, match="."):
                upsert_entity(conn, bad, "x", "file")
        assert conn.execute("SELECT COUNT(*) FROM knowledge_entities").fetchone()[0] == 0

    def test_add_synapse_rejects_a_malformed_source_or_target(self, conn):
        _file(conn, FILE_A)
        _file(conn, FILE_B)

        with pytest.raises(InvalidKnowledgeURIError):
            add_synapse(conn, "nonsense", FILE_B, "calls")
        with pytest.raises(InvalidKnowledgeURIError):
            add_synapse(conn, FILE_A, "kernel://task/", "calls")
        assert conn.execute("SELECT COUNT(*) FROM knowledge_synapses").fetchone()[0] == 0

    def test_upsert_entity_rejects_a_mismatched_entity_type(self, conn):
        """A row whose kind and address disagree is findable by neither."""
        with pytest.raises(InvalidKnowledgeURIError) as caught:
            upsert_entity(conn, FILE_A, "a.py", "symbol")

        assert "file" in str(caught.value)
        assert "symbol" in str(caught.value)
        assert conn.execute("SELECT COUNT(*) FROM knowledge_entities").fetchone()[0] == 0

    def test_add_synapse_rejects_an_unknown_relation(self, conn):
        _file(conn, FILE_A)
        _file(conn, FILE_B)

        with pytest.raises(ValueError, match="unknown relation"):
            add_synapse(conn, FILE_A, FILE_B, "obliterates")
        assert conn.execute("SELECT COUNT(*) FROM knowledge_synapses").fetchone()[0] == 0


# ------------------------------------------------------------------------ entity & edge upserts


class TestUpserts:
    def test_an_entity_is_inserted_then_updated_in_place(self, conn):
        upsert_entity(conn, FILE_A, "a.py", "file", {"lang": "python"})
        row = conn.execute("SELECT * FROM knowledge_entities").fetchone()
        assert row["id"] == FILE_A
        assert row["name"] == "a.py"
        assert row["type"] == "file"
        assert json.loads(row["metadata_json"]) == {"lang": "python"}

        upsert_entity(conn, FILE_A, "renamed.py", "file", {"lang": "python", "lines": 12})

        rows = conn.execute("SELECT * FROM knowledge_entities").fetchall()
        assert len(rows) == 1, "an upsert must not add a second row for one URI"
        assert rows[0]["name"] == "renamed.py"
        assert json.loads(rows[0]["metadata_json"]) == {"lang": "python", "lines": 12}

    def test_metadata_defaults_to_an_empty_object(self, conn):
        upsert_entity(conn, FILE_A, "a.py", "file")
        row = conn.execute("SELECT metadata_json FROM knowledge_entities").fetchone()
        assert row["metadata_json"] == "{}"
        assert json.loads(row["metadata_json"]) == {}

    def test_every_entity_kind_is_storable(self, conn):
        upsert_entity(conn, "kernel://symbol/a.py:alpha", "alpha", "symbol")
        upsert_entity(conn, "kernel://rule/chokehold", "chokehold", "rule")
        upsert_entity(conn, "kernel://task/task-104", "task-104", "task")
        upsert_entity(conn, FILE_A, "a.py", "file")

        kinds = {row["type"] for row in conn.execute("SELECT type FROM knowledge_entities")}
        assert kinds == {"symbol", "rule", "task", "file"}

    def test_a_duplicate_edge_updates_its_weight(self, conn):
        _file(conn, FILE_A)
        _file(conn, FILE_B)

        add_synapse(conn, FILE_A, FILE_B, "calls", 1.0)
        add_synapse(conn, FILE_A, FILE_B, "calls", 0.25)

        rows = conn.execute("SELECT weight FROM knowledge_synapses").fetchall()
        assert len(rows) == 1, "the (source, target, relation) triple is the identity"
        assert rows[0]["weight"] == pytest.approx(0.25)

    def test_the_same_pair_may_carry_two_relations(self, conn):
        _file(conn, FILE_A)
        _file(conn, FILE_B)

        add_synapse(conn, FILE_A, FILE_B, "calls", 1.0)
        add_synapse(conn, FILE_A, FILE_B, "modifies", 2.0)

        relations = {
            row["relation_type"]
            for row in conn.execute("SELECT relation_type FROM knowledge_synapses")
        }
        assert relations == {"calls", "modifies"}


# ------------------------------------------------------------------ cascading foreign keys


class TestCascadingDeletes:
    def test_deleting_an_entity_removes_its_synapses(self, conn):
        _file(conn, FILE_A)
        _file(conn, FILE_B)
        add_synapse(conn, FILE_A, FILE_B, "calls")
        assert conn.execute("SELECT COUNT(*) FROM knowledge_synapses").fetchone()[0] == 1

        conn.execute("DELETE FROM knowledge_entities WHERE id = ?", (FILE_A,))

        assert conn.execute("SELECT COUNT(*) FROM knowledge_synapses").fetchone()[0] == 0

    def test_deleting_a_target_removes_inbound_synapses_too(self, conn):
        _file(conn, FILE_A)
        _file(conn, FILE_B)
        add_synapse(conn, FILE_A, FILE_B, "calls")

        conn.execute("DELETE FROM knowledge_entities WHERE id = ?", (FILE_B,))

        assert conn.execute("SELECT COUNT(*) FROM knowledge_synapses").fetchone()[0] == 0

    def test_a_synapse_to_a_missing_entity_is_refused(self, conn):
        _file(conn, FILE_A)
        with pytest.raises(sqlite3.IntegrityError):
            add_synapse(conn, FILE_A, FILE_B, "calls")


# ---------------------------------------------------------------------------- multi-hop depth


def _chain(conn):
    """A -> B -> C -> D, one edge per hop."""
    for uri in (FILE_A, FILE_B, FILE_C, FILE_D):
        _file(conn, uri)
    add_synapse(conn, FILE_A, FILE_B, "calls")
    add_synapse(conn, FILE_B, FILE_C, "calls")
    add_synapse(conn, FILE_C, FILE_D, "calls")


class TestDepthControl:
    def test_depth_one_returns_only_the_direct_edge(self, conn):
        _chain(conn)

        edges = get_related_entities(conn, FILE_A, max_depth=1)

        assert [(e["source_id"], e["target_id"]) for e in edges] == [(FILE_A, FILE_B)]
        assert edges[0]["depth"] == 1

    def test_depth_two_returns_the_next_hop_and_excludes_the_third(self, conn):
        _chain(conn)

        edges = get_related_entities(conn, FILE_A, max_depth=2)
        pairs = {(e["source_id"], e["target_id"]) for e in edges}

        assert pairs == {(FILE_A, FILE_B), (FILE_B, FILE_C)}
        assert FILE_D not in {e["target_id"] for e in edges}

    def test_the_whole_chain_is_reachable_at_a_sufficient_depth(self, conn):
        _chain(conn)

        edges = get_related_entities(conn, FILE_A, max_depth=3)

        assert {e["target_id"] for e in edges} == {FILE_B, FILE_C, FILE_D}
        assert max(e["depth"] for e in edges) == 3

    def test_results_are_ordered_by_depth_then_descending_weight(self, conn):
        for uri in (FILE_A, FILE_B, FILE_C):
            _file(conn, uri)
        add_synapse(conn, FILE_A, FILE_B, "calls", 0.1)
        add_synapse(conn, FILE_A, FILE_C, "calls", 0.9)
        add_synapse(conn, FILE_B, FILE_C, "calls", 1.0)

        edges = get_related_entities(conn, FILE_A, max_depth=2)

        assert [e["depth"] for e in edges] == sorted(e["depth"] for e in edges)
        direct = [e for e in edges if e["depth"] == 1]
        assert [e["weight"] for e in direct] == [pytest.approx(0.9), pytest.approx(0.1)]

    def test_a_seed_with_no_edges_returns_nothing(self, conn):
        _file(conn, FILE_A)
        assert get_related_entities(conn, FILE_A, max_depth=5) == []

    def test_the_relation_type_and_weight_are_reported(self, conn):
        _file(conn, FILE_A)
        _file(conn, FILE_B)
        add_synapse(conn, FILE_A, FILE_B, "enforces", 0.5)

        edge = get_related_entities(conn, FILE_A, max_depth=1)[0]

        assert edge == {
            "source_id": FILE_A,
            "target_id": FILE_B,
            "relation_type": "enforces",
            "weight": pytest.approx(0.5),
            "depth": 1,
        }

    def test_an_invalid_seed_is_refused(self, conn):
        with pytest.raises(InvalidKnowledgeURIError):
            get_related_entities(conn, "kernel://widget/x", max_depth=1)

    def test_max_depth_is_bounded(self, conn):
        _chain(conn)

        for bad in (0, -1):
            with pytest.raises(ValueError, match="at least 1"):
                get_related_entities(conn, FILE_A, max_depth=bad)
        for bad in ("2", 2.5, None, True):
            with pytest.raises(ValueError, match="must be an integer"):
                get_related_entities(conn, FILE_A, max_depth=bad)

    def test_max_depth_is_capped_at_the_limit(self, conn):
        _chain(conn)
        # Asking for more than the ceiling is clamped, not refused: the answer is still the
        # whole reachable graph, and a runaway depth is what the ceiling exists to prevent.
        edges = get_related_entities(conn, FILE_A, max_depth=MAX_DEPTH_LIMIT + 100)
        assert {e["target_id"] for e in edges} == {FILE_B, FILE_C, FILE_D}


# ------------------------------------------------------------------ cycles and graph loops


class TestBidirectionalTraversal:
    """The walk must report both what a node depends on and what depends on it.

    A forward-only graph is useless to a planner: changing a shared utility without learning
    who calls it is how a refactor breaks the system.
    """

    def test_an_inbound_edge_is_returned(self, conn):
        """Seeded at the *callee*, the caller must come back."""
        _file(conn, FILE_A)
        _file(conn, FILE_B)
        add_synapse(conn, FILE_A, FILE_B, "calls")

        edges = get_related_entities(conn, FILE_B, max_depth=1)

        assert [(e["source_id"], e["target_id"]) for e in edges] == [(FILE_A, FILE_B)]

    def test_both_directions_are_reported_together(self, conn):
        """A -> B -> C seeded at B returns the edge out *and* the edge in."""
        for uri in (FILE_A, FILE_B, FILE_C):
            _file(conn, uri)
        add_synapse(conn, FILE_A, FILE_B, "calls")
        add_synapse(conn, FILE_B, FILE_C, "calls")

        edges = get_related_entities(conn, FILE_B, max_depth=1)
        pairs = {(e["source_id"], e["target_id"]) for e in edges}

        assert pairs == {(FILE_B, FILE_C), (FILE_A, FILE_B)}

    def test_the_upstream_walk_is_bounded_by_depth(self, conn):
        """A -> B -> C -> D seeded at D: the whole chain is upstream of it."""
        _chain(conn)

        shallow = get_related_entities(conn, FILE_D, max_depth=1)
        deep = get_related_entities(conn, FILE_D, max_depth=3)

        assert {(e["source_id"], e["target_id"]) for e in shallow} == {(FILE_C, FILE_D)}
        assert {(e["source_id"], e["target_id"]) for e in deep} == {
            (FILE_A, FILE_B), (FILE_B, FILE_C), (FILE_C, FILE_D),
        }

    def test_a_cycle_terminates_in_both_directions(self, conn):
        """The ring is reported once, from either end, without looping."""
        for uri in (FILE_A, FILE_B, FILE_C):
            _file(conn, uri)
        add_synapse(conn, FILE_A, FILE_B, "calls")
        add_synapse(conn, FILE_B, FILE_C, "calls")
        add_synapse(conn, FILE_C, FILE_A, "calls")

        from_a = get_related_entities(conn, FILE_A, max_depth=5)
        from_c = get_related_entities(conn, FILE_C, max_depth=5)

        ring = {(FILE_A, FILE_B), (FILE_B, FILE_C), (FILE_C, FILE_A)}
        assert {(e["source_id"], e["target_id"]) for e in from_a} == ring
        # Seeded anywhere on the ring the same three edges come back, because the backward walk
        # covers what the forward walk cannot reach.
        assert {(e["source_id"], e["target_id"]) for e in from_c} == ring
        assert len(from_a) == 3
        assert len(from_c) == 3

    def test_an_edge_reachable_both_ways_is_reported_once(self, conn):
        """A <-> B: each direction is a distinct edge, and neither is duplicated."""
        _file(conn, FILE_A)
        _file(conn, FILE_B)
        add_synapse(conn, FILE_A, FILE_B, "calls")
        add_synapse(conn, FILE_B, FILE_A, "calls")

        edges = get_related_entities(conn, FILE_A, max_depth=5)

        assert len(edges) == 2
        assert {(e["source_id"], e["target_id"]) for e in edges} == {
            (FILE_A, FILE_B), (FILE_B, FILE_A),
        }

    def test_a_self_loop_is_reported_once_from_either_end(self, conn):
        _file(conn, FILE_A)
        add_synapse(conn, FILE_A, FILE_A, "modifies")

        edges = get_related_entities(conn, FILE_A, max_depth=5)

        assert len(edges) == 1
        assert (edges[0]["source_id"], edges[0]["target_id"]) == (FILE_A, FILE_A)


class TestCycleTolerance:
    def test_a_three_node_cycle_reports_all_three_edges(self, conn):
        """A -> B -> C -> A, queried deeper than the ring is long, reports the whole ring.

        The guard tracks *edges*, so the hop that closes the ring onto the seed is a new edge
        and is followed. Under the previous node-tracking form the seed was in the walked path
        from the first hop, so ``C -> A`` was pruned and the topology was reported wrong.
        """
        for uri in (FILE_A, FILE_B, FILE_C):
            _file(conn, uri)
        add_synapse(conn, FILE_A, FILE_B, "calls")
        add_synapse(conn, FILE_B, FILE_C, "calls")
        add_synapse(conn, FILE_C, FILE_A, "calls")

        edges = get_related_entities(conn, FILE_A, max_depth=5)

        pairs = {(e["source_id"], e["target_id"]) for e in edges}
        assert pairs == {
            (FILE_A, FILE_B),
            (FILE_B, FILE_C),
            (FILE_C, FILE_A),
        }
        assert len(edges) == 3, "each edge of the ring is walked exactly once"
        # The closing edge is the one the node-tracking guard used to lose.
        assert (FILE_C, FILE_A) in pairs
        # Depth is the *nearest* distance, and the walk is two-way: the closing hop is one step
        # inbound from A, so nothing sits three hops out any more.
        assert max(e["depth"] for e in edges) == 2

    def test_a_substring_collision_does_not_prune_a_real_edge(self, conn):
        """``a.py`` is a substring of ``a.pyc``, and the guard must not confuse the two.

        With a path built by plain concatenation the check is a substring test against the bare
        URIs, so an edge into ``a.py`` is pruned as "already visited" whenever ``a.pyc`` is in
        the walked chain -- a reachable node silently disappears. The delimited edge form
        (``|source->target|``) makes the check exact: ``|.../a.py|`` is not a substring of
        ``|.../a.pyc|``.
        """
        x = "kernel://file/x.py"
        p = "kernel://file/a.py"
        pc = "kernel://file/a.pyc"
        for uri in (x, p, pc):
            _file(conn, uri)
        add_synapse(conn, x, pc, "calls")
        add_synapse(conn, pc, p, "calls")

        edges = get_related_entities(conn, x, max_depth=5)
        pairs = {(e["source_id"], e["target_id"]) for e in edges}

        assert (pc, p) in pairs, "a.pyc in the chain must not prune an edge into a.py"
        assert pairs == {(x, pc), (pc, p)}

    def test_a_self_loop_is_walked_once(self, conn):
        _file(conn, FILE_A)
        add_synapse(conn, FILE_A, FILE_A, "modifies")

        edges = get_related_entities(conn, FILE_A, max_depth=5)

        assert [(e["source_id"], e["target_id"]) for e in edges] == [(FILE_A, FILE_A)]

    def test_a_diamond_does_not_duplicate_edges(self, conn):
        """A -> B -> D and A -> C -> D: D is reached twice, but each edge is one row."""
        for uri in (FILE_A, FILE_B, FILE_C, FILE_D):
            _file(conn, uri)
        add_synapse(conn, FILE_A, FILE_B, "calls")
        add_synapse(conn, FILE_A, FILE_C, "calls")
        add_synapse(conn, FILE_B, FILE_D, "calls")
        add_synapse(conn, FILE_C, FILE_D, "calls")

        edges = get_related_entities(conn, FILE_A, max_depth=3)

        pairs = [(e["source_id"], e["target_id"]) for e in edges]
        assert len(pairs) == len(set(pairs)), "SELECT DISTINCT must collapse a re-reached edge"
        assert set(pairs) == {
            (FILE_A, FILE_B), (FILE_A, FILE_C), (FILE_B, FILE_D), (FILE_C, FILE_D),
        }

    def test_a_long_cycle_reports_the_whole_ring(self, conn):
        """Ten nodes in a ring, asked for far more depth than the ring is long.

        All ten edges come back, including the one that closes the ring, and the query returns
        promptly rather than recursing until it aborts: the edge guard admits each edge at most
        once, so the walk is bounded by the edge count.
        """
        uris = [f"kernel://file/ring{i}.py" for i in range(10)]
        for uri in uris:
            _file(conn, uri)
        for index, uri in enumerate(uris):
            add_synapse(conn, uri, uris[(index + 1) % len(uris)], "calls")

        edges = get_related_entities(conn, uris[0], max_depth=MAX_DEPTH_LIMIT)

        assert len(edges) == len(uris)
        assert {e["source_id"] for e in edges} == set(uris)
        # The hop back onto the seed is present, which is what makes the topology a ring.
        assert (uris[-1], uris[0]) in {(e["source_id"], e["target_id"]) for e in edges}
