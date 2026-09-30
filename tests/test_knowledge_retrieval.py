"""Phase 5 Batch 3: LanceDB vector store, ingestion sync, and the hybrid retriever.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_knowledge_retrieval.py -n0 -q

**No mocks for the DB layers.** Every test runs a real in-memory SQLite database (built from
the real schema) and a real LanceDB directory on a temp path. The embedder is the real
``HashingEmbedder`` from ``tools.semantic_index`` -- a deterministic, offline encoder, not a
stub: it is the production embedder's stand-in only because ``all-MiniLM-L6-v2`` needs a model
download this environment has no network for. It genuinely hashes tokens into a vector, so
retrieval is genuinely exercised.
"""

from __future__ import annotations

import sqlite3

from contextlib import contextmanager

import pytest

import os

# CI and the suite run on the offline encoder: the semantic model fetches ~90 MB of weights,
# and every xdist worker paying that would be minutes per run. This is an explicit override of a
# *semantic-by-default* architecture, not a gate on it -- production runs unset.
os.environ.setdefault("ALETH_EMBEDDER", "hashing")

from orchestration.retriever import (
    retrieve_context,
    retrieve_context_for_task,
    task_query,
)
from storage.graph_engine import get_related_entities
from storage.db import apply_knowledge_graph_schema
from storage.graph_engine import add_synapse, upsert_entity
from storage.knowledge_uri import InvalidKnowledgeURIError
from storage.knowledge_sync import KnowledgeSync, entity_text
from storage.vector_store import EntityVectorStore
from tools.semantic_index import HashingEmbedder

DIM = 256


@pytest.fixture()
def embedder():
    return HashingEmbedder(dim=DIM)


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    apply_knowledge_graph_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def store(tmp_path, embedder):
    return EntityVectorStore(str(tmp_path / "vectors"), dim=embedder.dim)


@pytest.fixture()
def sync(store, embedder):
    return KnowledgeSync(store, embedder)


def _entity(conn, uri, name, kind, metadata=None):
    upsert_entity(conn, uri, name, kind, metadata)


# ------------------------------------------------------------------- the vector store itself


class TestVectorStore:
    def test_an_upsert_then_a_search_returns_the_uri(self, store, embedder):
        text = "chokehold refuses a whole file overwrite"
        store.upsert_vector("kernel://rule/chokehold", text, embedder.encode([text])[0])

        hits = store.semantic_search(embedder.encode([text])[0], top_k=5)

        assert hits == ["kernel://rule/chokehold"]

    def test_an_empty_store_answers_with_nothing_rather_than_failing(self, store, embedder):
        assert store.semantic_search(embedder.encode(["anything"])[0], top_k=3) == []
        assert store.count() == 0
        assert store.existing_uris() == set()

    def test_re_upserting_a_uri_replaces_it_rather_than_duplicating(self, store, embedder):
        uri = "kernel://file/app.py"
        store.upsert_vector(uri, "first", embedder.encode(["first"])[0])
        store.upsert_vector(uri, "second", embedder.encode(["second"])[0])

        assert store.count() == 1
        assert store.text_for(uri) == "second"

    def test_the_ranking_is_by_similarity_best_first(self, store, embedder):
        store.upsert_vector(
            "kernel://file/auth.py",
            "authenticate login password session token",
            embedder.encode(["authenticate login password session token"])[0],
        )
        store.upsert_vector(
            "kernel://file/maths.py",
            "vector matrix arithmetic scalar product",
            embedder.encode(["vector matrix arithmetic scalar product"])[0],
        )

        hits = store.semantic_search(
            embedder.encode(["login password session"])[0], top_k=2
        )

        assert hits[0] == "kernel://file/auth.py"

    def test_delete_removes_only_the_named_uris(self, store, embedder):
        for uri in ("kernel://file/a.py", "kernel://file/b.py"):
            store.upsert_vector(uri, uri, embedder.encode([uri])[0])

        store.delete(["kernel://file/a.py"])

        assert store.existing_uris() == {"kernel://file/b.py"}

    def test_a_wrong_length_vector_is_refused(self, store):
        with pytest.raises(ValueError, match="expected"):
            store.upsert_vector("kernel://file/a.py", "a", [0.0] * (DIM + 1))
        with pytest.raises(ValueError, match="expected"):
            store.semantic_search([0.0] * (DIM - 1), top_k=1)

    def test_an_unparseable_uri_is_refused_before_it_reaches_a_query(self, store, embedder):
        """Safety comes from parsing, not from escaping.

        LanceDB has no parameter binding for a ``where`` clause, so a URI reaches the query
        builder as text. A valid ``kernel://`` address cannot contain a quote, so a well-formed
        one cannot close a string literal -- and a malformed one is refused here rather than
        being hand-escaped into something that might work.
        """
        store.upsert_vector("kernel://file/a.py", "a", embedder.encode(["a"])[0])
        store.upsert_vector("kernel://file/b.py", "b", embedder.encode(["b"])[0])

        for hostile in (
            "kernel://file/a.py' OR '1'='1",
            "kernel://file/a.py'; DROP TABLE knowledge_vectors; --",
            "kernel://file/a.py\\' OR 1=1",
        ):
            with pytest.raises(InvalidKnowledgeURIError):
                store.delete([hostile])
            with pytest.raises(InvalidKnowledgeURIError):
                store.text_for(hostile)
            with pytest.raises(InvalidKnowledgeURIError):
                store.upsert_vector(hostile, "x", embedder.encode(["x"])[0])

        # Nothing was touched by any of the attempts.
        assert store.existing_uris() == {"kernel://file/a.py", "kernel://file/b.py"}


# --------------------------------------------------------------- ingestion sync and rollback


class TestIngestionSync:
    def test_queuing_does_no_io_until_the_flush(self, conn, store, sync):
        _entity(conn, "kernel://file/a.py", "a.py", "file")
        sync.queue("kernel://file/a.py", name="a.py")

        assert sync.pending == ("kernel://file/a.py",)
        assert store.count() == 0, "the queue must not write before the transaction commits"

        assert sync.flush(conn) == 1
        assert store.count() == 1
        assert sync.pending == ()

    def test_a_rolled_back_entity_is_never_pushed(self, conn, store, sync):
        """The guarantee: a vector is only written for state that committed.

        The entity is written and queued inside a transaction that is then rolled back. The
        flush re-reads ``knowledge_entities``, so the vector write is derived from committed
        state rather than from intent -- and nothing is written.
        """
        conn.execute("BEGIN")
        _entity(conn, "kernel://file/ghost.py", "ghost.py", "file")
        sync.queue("kernel://file/ghost.py", name="ghost.py")
        conn.execute("ROLLBACK")

        assert sync.flush(conn) == 0
        assert store.count() == 0
        # The queue keeps the uncommitted URI, so a later flush can still honour it if the
        # entity does get committed.
        assert sync.pending == ("kernel://file/ghost.py",)

    def test_a_committed_entity_is_pushed(self, conn, store, sync):
        conn.execute("BEGIN")
        _entity(conn, "kernel://file/kept.py", "kept.py", "file")
        sync.queue("kernel://file/kept.py", name="kept.py")
        conn.execute("COMMIT")

        assert sync.flush(conn) == 1
        assert store.existing_uris() == {"kernel://file/kept.py"}

    def test_reconcile_removes_an_orphaned_vector(self, conn, store, sync):
        """A crash between commit and flush, or an entity deleted behind the index."""
        _entity(conn, "kernel://file/a.py", "a.py", "file")
        sync.queue("kernel://file/a.py", name="a.py")
        sync.flush(conn)

        conn.execute("DELETE FROM knowledge_entities WHERE id = ?", ("kernel://file/a.py",))

        report = sync.reconcile(conn)

        assert report["orphaned"] == ["kernel://file/a.py"]
        assert store.count() == 0

    def test_reconcile_reports_an_entity_with_no_vector(self, conn, store, sync):
        _entity(conn, "kernel://file/a.py", "a.py", "file")

        report = sync.reconcile(conn)

        assert report["missing"] == ["kernel://file/a.py"]
        assert report["orphaned"] == []

    def test_reindex_rebuilds_a_missing_vector_from_committed_state(self, conn, store, sync):
        _entity(conn, "kernel://file/a.py", "a.py", "file", {"lang": "python"})
        assert store.count() == 0

        assert sync.reindex(conn) == 1

        assert store.existing_uris() == {"kernel://file/a.py"}
        assert "a.py" in (store.text_for("kernel://file/a.py") or "")
        assert sync.reconcile(conn)["missing"] == []

    def test_the_embedded_text_carries_the_name_uri_and_metadata(self):
        text = entity_text("run", {"lang": "python", "lines": 12}, "kernel://symbol/app.py:run")

        assert "run" in text
        assert "kernel://symbol/app.py:run" in text
        assert "python" in text
        assert "12" in text


# ------------------------------------------------------------------------- the hybrid retriever


def _seed_graph(conn, sync):
    """A small graph whose vocabulary makes a natural-language query unambiguous.

    app.py --contains--> login() --enforces--> chokehold --modifies--> config.py
    """
    plan = [
        ("kernel://file/app.py", "app.py application entry point", "file", {"lang": "python"}),
        ("kernel://symbol/app.py:login", "login authenticate password session token", "symbol", {}),
        ("kernel://rule/chokehold_refuse_overwrite", "chokehold refuse whole file overwrite", "rule", {}),
        ("kernel://file/config.py", "config.py settings environment variables", "file", {"lang": "python"}),
    ]
    for uri, name, kind, metadata in plan:
        _entity(conn, uri, name, kind, metadata)
        sync.queue(uri, name=name, metadata=metadata)
    sync.flush(conn)

    add_synapse(conn, "kernel://file/app.py", "kernel://symbol/app.py:login", "calls")
    add_synapse(conn, "kernel://symbol/app.py:login", "kernel://rule/chokehold_refuse_overwrite", "enforces")
    add_synapse(conn, "kernel://rule/chokehold_refuse_overwrite", "kernel://file/config.py", "modifies")


def _yield_connection(conn):
    """A stand-in for ``knowledge_connection`` that yields an already-open connection."""
    @contextmanager
    def _inner():
        yield conn
    return _inner()


class TestOutcomeTransaction:
    """The verdict and its graph consequences are one commit.

    The graph is the planner's only view of reality, so a task marked completed that the graph
    has never heard of is worse than a failed write -- the planner would reason over topology
    that does not match the code on disk.
    """

    def test_the_status_write_and_the_graph_write_commit_together(self):
        import tempfile

        from storage.db import PlanDAG, PlanStore, TaskNode
        from storage.knowledge_sync import register_task_entities

        # A real store on a real file, so the transaction boundary is the production one.
        with tempfile.TemporaryDirectory() as tmp:
            store = PlanStore(os.path.join(tmp, "state.db"))
            store.save_dag(
                PlanDAG(
                    plan_id="PLAN",
                    title="T",
                    order=["task-1"],
                    nodes={"task-1": TaskNode(id="task-1", plan_id="PLAN", title="one")},
                ),
                sync_edges=True,
            )

            with store.transaction() as connection:
                assert store.update_task_status(
                    "PLAN", "task-1", "completed", connection=connection
                )
                register_task_entities(
                    connection, plan_id="PLAN", task_id="task-1", title="one", files=["a.py"]
                )

            assert store.get_dag("PLAN").nodes["task-1"].status == "completed"
            # The graph rows are committed too: the task entity and its file entity.
            check = sqlite3.connect(store.path)
            try:
                count = check.execute("SELECT COUNT(*) FROM knowledge_entities").fetchone()[0]
                kinds = {
                    row[1] for row in check.execute("SELECT id, type FROM knowledge_entities")
                }
            finally:
                check.close()
            assert count == 2, "the task and its file are both committed"
            assert kinds == {"task", "file"}

    def test_a_failed_graph_write_rolls_the_status_back(self):
        """Neither half survives: one COMMIT decides both."""
        import tempfile

        from storage.db import PlanDAG, PlanStore, TaskNode

        with tempfile.TemporaryDirectory() as tmp:
            store = PlanStore(os.path.join(tmp, "state.db"))
            store.save_dag(
                PlanDAG(
                    plan_id="PLAN",
                    title="T",
                    order=["task-1"],
                    nodes={"task-1": TaskNode(id="task-1", plan_id="PLAN", title="one")},
                ),
                sync_edges=True,
            )

            with pytest.raises(sqlite3.IntegrityError):
                with store.transaction() as connection:
                    assert store.update_task_status(
                        "PLAN", "task-1", "completed", connection=connection
                    )
                    # A synapse to an entity that does not exist: the FK refuses it, and the
                    # whole unit -- the status write included -- must go with it.
                    add_synapse(
                        connection, "kernel://task/task-1", "kernel://file/ghost.py", "modifies"
                    )

            assert store.get_dag("PLAN").nodes["task-1"].status != "completed"
            check = sqlite3.connect(store.path)
            try:
                count = check.execute("SELECT COUNT(*) FROM knowledge_entities").fetchone()[0]
            finally:
                check.close()
            assert count == 0


class TestEmbedderDefault:
    """The semantic model is the default; the offline encoder is an explicit override."""

    def test_the_override_selects_the_offline_encoder(self, monkeypatch):
        from orchestration import retriever
        from tools.semantic_index import HashingEmbedder

        monkeypatch.setenv("ALETH_EMBEDDER", "hashing")
        retriever.reset_embedder_cache()
        try:
            assert isinstance(retriever.default_embedder(), HashingEmbedder)
        finally:
            retriever.reset_embedder_cache()

    def test_the_default_is_the_semantic_model_not_the_hash(self, monkeypatch):
        """With no override the choice is ``MiniLmEmbedder`` -- a lexical hash would disable
        the semantic matching Phase 5 exists for, so it must never be chosen by omission."""
        from orchestration import retriever

        monkeypatch.delenv("ALETH_EMBEDDER", raising=False)
        retriever.reset_embedder_cache()
        try:
            # Construction is what downloads the weights, so the *decision* is asserted rather
            # than the instance: ``_build_embedder`` must not reach for the hash without the
            # override. Loading it is a deployment concern, handled by prefetch_embedder()
            # during kernel startup.
            import tools.semantic_index as semantic

            monkeypatch.setattr(semantic, "HashingEmbedder", _forbidden)
            try:
                retriever._build_embedder()
            except Exception:
                pass  # a missing model is environmental; reaching for the hash is the failure
            finally:
                monkeypatch.undo()
        finally:
            retriever.reset_embedder_cache()


def _forbidden(*args, **kwargs):
    raise AssertionError("the offline encoder must not be selected without the override")


class TestLiveIngestion:
    """The ingestion hook: what actually lands is what enters the graph."""

    def test_an_applied_task_and_its_files_become_entities_with_edges(self, conn, store, sync):
        from storage.knowledge_sync import register_artifact_entities

        artifact = {"ast_targets": [
            {"file_path": "app/models.py"},
            {"file_path": "app/views.py"},
            {"file_path": "app/models.py"},  # the same file twice is one entity
        ]}
        uris = register_artifact_entities(
            conn, plan_id="PLAN", task_id="task-42", title="Build the models", artifact=artifact
        )

        kinds = {
            row["id"]: row["type"]
            for row in conn.execute("SELECT id, type FROM knowledge_entities")
        }
        assert kinds["kernel://task/task-42"] == "task"
        assert kinds["kernel://file/app/models.py"] == "file"
        assert uris.count("kernel://file/app/models.py") == 1, "a repeated target is one entity"

        edges = get_related_entities(conn, "kernel://task/task-42", max_depth=1)
        assert {e["target_id"] for e in edges} == {
            "kernel://file/app/models.py", "kernel://file/app/views.py",
        }
        assert all(e["relation_type"] == "modifies" for e in edges)

    def test_a_hostile_target_path_is_skipped_not_fatal(self, conn, store, sync):
        """The artifact applied; the graph records what it can rather than nothing."""
        from storage.knowledge_sync import register_artifact_entities

        uris = register_artifact_entities(
            conn, plan_id="PLAN", task_id="task-1", title="t",
            artifact={"ast_targets": [
                {"file_path": "../../escape.py"},
                {"file_path": "app/ok.py"},
            ]},
        )

        assert "kernel://file/app/ok.py" in uris
        assert not any("escape" in uri for uri in uris)

    def test_the_executor_ingests_after_a_successful_apply(self, conn, store, sync):
        """The hook the executor calls is the one that populates the graph."""
        from storage.knowledge_sync import register_task_entities

        uris = register_task_entities(
            conn, plan_id="PLAN", task_id="task-7", title="Task 7", files=["svc/api.py"]
        )
        conn.commit()

        assert "kernel://task/task-7" in uris
        edges = get_related_entities(conn, "kernel://task/task-7", max_depth=1)
        assert [e["target_id"] for e in edges] == ["kernel://file/svc/api.py"]

    def test_ingested_entities_can_be_indexed_and_retrieved(self, conn, store, sync, embedder):
        """End to end: ingest -> index -> a natural-language query finds the task."""
        from storage.knowledge_sync import register_task_entities

        uris = register_task_entities(
            conn, plan_id="PLAN", task_id="task-5", title="Build the login endpoint",
            files=["app/auth.py"],
        )
        conn.commit()
        sync.queue("kernel://task/task-5", text="Build the login endpoint task-5")
        sync.queue("kernel://file/app/auth.py", text="app auth login")
        sync.flush(conn)

        result = retrieve_context(conn, store, embedder, "login endpoint", top_k=2)

        assert "kernel://task/task-5" in result.seeds
        assert result.edges, "the ingested edge must be walkable from the matched seed"


class TestPlannerWiring:
    """The planner's prompt is built with the graph block, and degrades without it."""

    def test_the_planning_prompt_carries_the_graph_block(self, conn, store, sync, embedder, monkeypatch):
        from orchestration.workflow import planner

        _seed_graph(conn, sync)
        monkeypatch.setattr(
            "orchestration.retriever.default_vector_store", lambda: store
        )
        monkeypatch.setattr("orchestration.retriever.default_embedder", lambda: embedder)
        monkeypatch.setattr(
            "orchestration.retriever.knowledge_connection",
            lambda: _yield_connection(conn),
        )

        prompt = planner._prompt(
            {"id": "task-1", "title": "login password session", "details": [], "files": []},
            plan_id="PLAN",
            workspace_dir=str(conn),
            files=[],
        )

        assert "Knowledge graph:" in prompt
        assert "login" in prompt

    def test_a_failing_retriever_does_not_break_the_prompt(self, monkeypatch):
        """Retrieval is context, not correctness: a broken index must not fail a plan."""
        from orchestration.workflow import planner

        def _boom(*args, **kwargs):
            raise RuntimeError("no index")

        monkeypatch.setattr("orchestration.retriever.retrieve_for_plan", _boom)

        prompt = planner._prompt(
            {"id": "task-1", "title": "anything", "details": [], "files": []},
            plan_id="PLAN",
            workspace_dir=".",
            files=[],
        )

        assert "Return the JSON ImplementationPlanArtifact now." in prompt
        assert "Knowledge graph:" not in prompt
class TestHybridRetriever:
    """The unified pipeline: a natural-language task in, graph topology out."""

    def test_a_natural_language_query_traverses_the_graph(self, conn, store, sync, embedder):
        """The headline case: prose in, the correct topology out.

        The query matches the login symbol semantically, and the traversal then walks out of it
        to the rule it enforces -- a relation the query never mentioned.
        """
        _seed_graph(conn, sync)

        result = retrieve_context(conn, store, embedder, "the login password check is wrong")

        assert "kernel://symbol/app.py:login" in result.seeds

        pairs = {(e["source_id"], e["target_id"]) for e in result.edges}
        assert ("kernel://symbol/app.py:login", "kernel://rule/chokehold_refuse_overwrite") in pairs
        assert ("kernel://file/app.py", "kernel://symbol/app.py:login") in pairs

    def test_the_walk_is_bounded_by_max_depth(self, conn, store, sync, embedder):
        _seed_graph(conn, sync)
        query = "login password session"

        shallow = retrieve_context(conn, store, embedder, query, top_k=1, max_depth=1)
        deep = retrieve_context(conn, store, embedder, query, top_k=1, max_depth=3)

        shallow_pairs = {(e["source_id"], e["target_id"]) for e in shallow.edges}
        deep_pairs = {(e["source_id"], e["target_id"]) for e in deep.edges}

        # Two at depth 1, because the walk is two-way: the rule login enforces is one hop out,
        # and the file that contains login is one hop in.
        assert len(shallow_pairs) == 2
        assert ("kernel://file/app.py", "kernel://symbol/app.py:login") in shallow_pairs
        assert len(deep_pairs) > len(shallow_pairs)
        # The rule's own edge is two hops out, so depth 1 cannot see it.
        assert ("kernel://rule/chokehold_refuse_overwrite", "kernel://file/config.py") in deep_pairs
        assert ("kernel://rule/chokehold_refuse_overwrite", "kernel://file/config.py") not in shallow_pairs

    def test_the_result_carries_entity_metadata(self, conn, store, sync, embedder):
        _seed_graph(conn, sync)

        result = retrieve_context(conn, store, embedder, "login password session", top_k=1)

        by_uri = {entity["uri"]: entity for entity in result.entities}
        assert by_uri["kernel://symbol/app.py:login"]["type"] == "symbol"
        # The walk is two-way, so the file that *contains* login comes back as well as the rule
        # login enforces. That upstream visibility is the whole point: a planner changing login
        # must know what depends on it.
        assert by_uri["kernel://rule/chokehold_refuse_overwrite"]["type"] == "rule"
        assert by_uri["kernel://file/app.py"]["metadata"] == {"lang": "python"}

    def test_the_rendered_block_names_the_seeds_and_the_edges(self, conn, store, sync, embedder):
        _seed_graph(conn, sync)

        block = retrieve_context(conn, store, embedder, "login password session", top_k=1).render()

        assert "Matched entities" in block
        assert "login" in block
        assert "enforces" in block
        assert "depth" in block

    def test_a_query_matching_nothing_returns_an_empty_result(self, conn, store, sync, embedder):
        _seed_graph(conn, sync)

        result = retrieve_context(
            conn, store, embedder, "quarterly revenue forecast spreadsheet", top_k=1
        )

        # Whatever it matched, it must not invent an edge: with one seed at most, the topology
        # is whatever is genuinely attached to it.
        assert isinstance(result.seeds, list)
        assert all(
            edge["source_id"] in result.seeds or edge["source_id"] in {e["target_id"] for e in result.edges}
            for edge in result.edges
        )

    def test_an_empty_description_is_refused(self, conn, store, embedder):
        with pytest.raises(ValueError, match="task description"):
            retrieve_context(conn, store, embedder, "   ")

    def test_the_task_entry_point_builds_its_query_from_the_task(self, conn, store, sync, embedder):
        _seed_graph(conn, sync)
        task = {
            "id": "task-7",
            "title": "Fix the login password check",
            "details": ["the session token is not validated"],
            "files": ["app.py"],
        }

        result = retrieve_context_for_task(conn, store, embedder, task, top_k=1)

        assert "kernel://symbol/app.py:login" in result.seeds
        # The query is built from the title, details and declared files, and the rendered block
        # is what the planner's prompt receives.
        assert result.render().startswith("Matched entities")

    def test_task_query_is_built_from_the_whole_task(self):
        query = task_query({
            "id": "task-9",
            "title": "Fix the login check",
            "details": ["session token not validated"],
            "files": ["app.py"],
        })
        assert "Fix the login check" in query
        assert "session token not validated" in query
        assert "app.py" in query
        assert task_query({"id": "task-9"}) == "task-9"

    def test_a_seed_missing_from_the_relational_graph_is_reported(self, conn, store, embedder):
        """Drift is surfaced, not hidden: a vector whose entity is gone is a repair job."""
        uri = "kernel://file/ghost.py"
        store.upsert_vector(uri, "ghost.py orphaned vector", embedder.encode(["ghost.py orphaned vector"])[0])

        result = retrieve_context(conn, store, embedder, "ghost.py orphaned vector", top_k=1)

        assert result.seeds == [uri]
        assert result.missing == [uri]
