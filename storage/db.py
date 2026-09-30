"""SQLite-backed plan state: the single source of truth for the task DAG.

This module replaces the dual-sync machine. ``plan.json`` is gone, and the markdown
plan is a *projection* rendered from this store for humans -- the engine never reads it
back to decide state.

Design:

* **Strict models.** :class:`TaskNode` and :class:`PlanDAG` are Pydantic models with
  ``extra="forbid"``. Every representation of state is one of these, or the legacy plan
  dictionary projected from one for the existing UI/workflow contract.
* **One writer, one transaction.** Every mutation runs inside a single SQLite
  transaction, so a reader observes the whole change or none of it.
* **WAL.** The database runs in write-ahead-log mode, so readers never block the writer.
* **No file I/O.** This module owns the database and nothing else; the paths, the
  one-time import of a pre-existing plan, and the markdown projection live in
  ``tools.plan_state``.

The tables are ``plans`` (one row per plan), ``tasks`` (one row per node, keyed by
``(plan_id, id)`` so ids like ``task-1`` are unique *within* a plan),
``task_dependencies`` (the blocker edges, so a topological query is a join rather than a
scan of a JSON blob) and ``artifacts`` (the ImplementationPlanArtifact a ``planned`` task is
halted on).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict

_STRICT = ConfigDict(extra="forbid", strict=False)

from storage.telemetry import TELEMETRY_DDL

# The database file name, beside the markdown projection in the plan directory.
DB_FILENAME = "aleth_state.db"

# The five marks a milestone can carry. A ``Literal`` on purpose: the store must not
# accept a status the workflow cannot reason about. ``planned`` is the Artifact Gate's
# state -- System 2 has produced an ImplementationPlanArtifact and execution is halted
# until a human approves it.
TaskStatus = Literal["pending", "planned", "in_progress", "completed", "failed"]


class SubStep(BaseModel):
    """A nested checkbox folded into its parent task (never a node of its own)."""

    model_config = _STRICT

    id: str = ""
    title: str = ""
    status: TaskStatus = "pending"
    tag: Optional[str] = None
    details: List[str] = []
    files: List[str] = []
    behavioral_log: List[str] = []


class TaskNode(BaseModel):
    """One milestone: the unit of work, with its blockers and its AST targets.

    The first block is the DAG schema; the second is the projection surface the existing
    UI and workflow read off the plan dictionary (a section title, a tag, detail notes,
    deliverables, a behavioural ledger, nested sub-steps and the document order). It is
    carried on the node so a projection needs no second source.
    """

    model_config = _STRICT

    # --- DAG identity and scheduling ---
    id: str
    plan_id: str
    title: str = ""
    description: str = ""
    status: TaskStatus = "pending"
    # Ids of the nodes that must complete before this one is eligible.
    dependencies: List[str] = []
    # The symbols/files this task is expected to touch (populated in a later phase).
    ast_targets: List[str] = []
    assigned_agent: Optional[str] = None
    # The tools this node's work needs, declared **when the node is created** (Phase 7.4). The
    # Architect decides here; the worker never does. A node born without a declaration is born
    # without tools, which is what makes an empty list a real statement rather than a gap.
    required_capabilities: List[str] = []

    # --- projection surface ---
    section: str = ""
    section_id: str = ""
    tag: Optional[str] = None
    details: List[str] = []
    files: List[str] = []
    behavioral_log: List[str] = []
    sub_steps: List[SubStep] = []
    order_index: int = 0


class StateSummary(BaseModel):
    """The standing Global State Summary, true for every task."""

    model_config = _STRICT

    title: str = ""
    bullets: List[str] = []


class PlanDAG(BaseModel):
    """A whole plan: its metadata plus its nodes, keyed by id, in document order."""

    model_config = _STRICT

    plan_id: str
    goal: str = ""
    nodes: Dict[str, TaskNode] = {}
    created_at: float = 0.0
    updated_at: float = 0.0

    # --- plan-level projection surface ---
    version: str = "1.0"
    plan_file: str = "PLAN.md"
    title: str = ""
    state_summary: Optional[StateSummary] = None
    # Node ids in document order (``nodes`` is a dict, whose order we do not rely on
    # across a round trip).
    order: List[str] = []

    def ordered_nodes(self) -> List[TaskNode]:
        """Every node in document order, tolerating an id missing from ``nodes``."""
        seen = [node_id for node_id in self.order if node_id in self.nodes]
        extras = [node_id for node_id in self.nodes if node_id not in seen]
        return [self.nodes[node_id] for node_id in seen + extras]


def plan_id_for(plan_file: str) -> str:
    """The stable id of the plan a filename names (``PLAN.md`` -> ``PLAN``)."""
    stem = os.path.splitext(os.path.basename(plan_file or ""))[0]
    return stem.strip() or "PLAN"


# --------------------------------------------------------------- dict <-> DAG projection
#
# The rest of the app still speaks the legacy plan dictionary (``PlanPayload``). These
# two functions are the only bridge between that contract and the DAG, so there is one
# place that knows how the two correspond.


def _task_payload(node: TaskNode) -> Dict[str, Any]:
    """A task in the key order the legacy contract (and the event wire) expects."""
    return {
        "id": node.id,
        "section": node.section,
        "title": node.title,
        "status": node.status,
        "tag": node.tag,
        "details": list(node.details),
        "files": list(node.files),
        "behavioral_log": list(node.behavioral_log),
        # The real blocker edges, so the DAG the UI draws is the DAG the store holds. Carried
        # in the projection rather than fetched separately: a save round-trips it, and the
        # compiled core ignores a key it does not model.
        "dependencies": list(node.dependencies),
        "sub_steps": [sub.model_dump() for sub in node.sub_steps],
    }


def compute_metrics(nodes: List[TaskNode]) -> Dict[str, Any]:
    """The progress counters, matching the compiled core's formula exactly.

    ``progress_percent`` is an integer rounded half-to-even, which is what the old
    ``prepare_plan_state`` produced and what the progress meter was written against.
    """
    total = len(nodes)
    completed = sum(1 for node in nodes if node.status == "completed")
    in_progress = sum(1 for node in nodes if node.status == "in_progress")
    failed = sum(1 for node in nodes if node.status == "failed")
    pending = total - completed - in_progress - failed
    percent = round(completed / total * 100) if total else 0
    return {
        "total_tasks": total,
        "completed_tasks": completed,
        "in_progress_tasks": in_progress,
        "failed_tasks": failed,
        "pending_tasks": pending,
        "progress_percent": percent,
    }


def dag_to_plan_dict(dag: PlanDAG) -> Dict[str, Any]:
    """Project a DAG onto the legacy plan dictionary.

    Sections are re-derived by grouping consecutive nodes that share a ``section_id``,
    in document order, so the projection is stable across a save/load round trip.
    """
    nodes = dag.ordered_nodes()
    sections: List[Dict[str, Any]] = []
    for node in nodes:
        section_id = node.section_id or f"sec-{len(sections) + 1}"
        if not sections or sections[-1]["id"] != section_id:
            sections.append({"id": section_id, "title": node.section, "tasks": []})
        sections[-1]["tasks"].append(_task_payload(node))
    return {
        "version": dag.version,
        "plan_file": dag.plan_file,
        "title": dag.title,
        "state_summary": dag.state_summary.model_dump() if dag.state_summary else None,
        "updated_at": dag.updated_at,
        "sections": sections,
        "steps": [_task_payload(node) for node in nodes],
        "metrics": compute_metrics(nodes),
    }


def _node_from_payload(task: Dict[str, Any], plan_id: str, order_index: int) -> TaskNode:
    """One task dictionary -> a node, preserving DAG fields if the caller carried them."""
    return TaskNode(
        id=str(task.get("id", "")),
        plan_id=plan_id,
        title=str(task.get("title", "")),
        description=str(task.get("description", "")),
        status=task.get("status") or "pending",
        # Structured topology, never parsed from prose: ``dependencies`` is the only source
        # of a blocker edge. Nothing here reads the markdown -- it is a read-only projection.
        dependencies=[str(dep) for dep in (task.get("dependencies") or []) if str(dep).strip()],
        ast_targets=[str(a) for a in (task.get("ast_targets") or [])],
        assigned_agent=task.get("assigned_agent"),
        # Declared by whoever authored the milestone, carried through the plan dictionary and the
        # compiled core (both preserve it), so the node is born knowing what it needs.
        required_capabilities=[
            str(name) for name in (task.get("required_capabilities") or []) if str(name).strip()
        ],
        section=str(task.get("section", "")),
        section_id=str(task.get("section_id", "")),
        tag=task.get("tag"),
        details=[str(d) for d in (task.get("details") or [])],
        files=[str(f) for f in (task.get("files") or [])],
        behavioral_log=[str(b) for b in (task.get("behavioral_log") or [])],
        sub_steps=[SubStep.model_validate(sub) for sub in (task.get("sub_steps") or [])],
        order_index=order_index,
    )


def plan_dict_to_dag(
    plan: Dict[str, Any],
    plan_id: str,
    *,
    created_at: Optional[float] = None,
) -> PlanDAG:
    """Build a DAG from a legacy plan dictionary.

    Tasks are taken from ``sections`` when it carries them and from the flat ``steps``
    view otherwise (the compiled core regroups a flat plan before this runs, but the
    fallback keeps a hand-built dictionary from silently becoming an empty plan).
    """
    now = time.time()
    order: List[str] = []
    nodes: Dict[str, TaskNode] = {}

    sections = plan.get("sections") or []
    if sections:
        for index, section in enumerate(sections):
            section_title = str(section.get("title", "General"))
            section_id = str(section.get("id", f"sec-{index + 1}"))
            for task in section.get("tasks") or []:
                node = _node_from_payload(task, plan_id, len(order))
                node.section = section_title or node.section
                node.section_id = section_id or node.section_id
                if node.id and node.id not in nodes:
                    nodes[node.id] = node
                    order.append(node.id)
    else:
        for task in plan.get("steps") or []:
            node = _node_from_payload(task, plan_id, len(order))
            if node.id and node.id not in nodes:
                nodes[node.id] = node
                order.append(node.id)

    summary = plan.get("state_summary")
    return PlanDAG(
        plan_id=plan_id,
        goal=str(plan.get("goal", "")),
        nodes=nodes,
        created_at=now if created_at is None else created_at,
        updated_at=float(plan.get("updated_at") or now),
        version=str(plan.get("version", "1.0")),
        plan_file=str(plan.get("plan_file", "PLAN.md")),
        title=str(plan.get("title", "")),
        state_summary=StateSummary.model_validate(summary) if summary else None,
        order=order,
    )


def merge_dag_fields(new_dag: PlanDAG, existing: Optional[PlanDAG]) -> PlanDAG:
    """Carry DAG-only fields forward from ``existing`` when ``new_dag`` does not set them.

    The legacy dictionary has no ``goal``/``dependencies``/``ast_targets``/
    ``assigned_agent``/``description``, so a save driven by it arrives with those empty.
    Preserving the stored values keeps a later phase's scheduling metadata from being
    erased by an ordinary status write.
    """
    if existing is None:
        return new_dag
    if not new_dag.goal:
        new_dag.goal = existing.goal
    new_dag.created_at = existing.created_at or new_dag.created_at
    for node_id, node in new_dag.nodes.items():
        previous = existing.nodes.get(node_id)
        if previous is None:
            continue
        if not node.description:
            node.description = previous.description
        if not node.dependencies:
            node.dependencies = list(previous.dependencies)
        if not node.ast_targets:
            node.ast_targets = list(previous.ast_targets)
        if node.assigned_agent is None:
            node.assigned_agent = previous.assigned_agent
        # The same rule for the declared tool set: the projection does not carry it, so an
        # ordinary save arrives with it empty and must not erase the birth declaration.
        if not node.required_capabilities:
            node.required_capabilities = list(previous.required_capabilities)
    return new_dag


# --------------------------------------------------------------------------- the store


def _dumps(values: List[Any]) -> str:
    """A JSON array for a text column (compact, ASCII-safe)."""
    return json.dumps(list(values), ensure_ascii=True)


def _loads_list(text: Optional[str]) -> List[Any]:
    """Read a JSON array column back, tolerating NULL/blank."""
    if not text:
        return []
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _loads_summary(text: Optional[str]) -> Optional[StateSummary]:
    """Read the JSON summary column back, tolerating NULL/blank/corrupt."""
    if not text:
        return None
    try:
        return StateSummary.model_validate(json.loads(text))
    except (TypeError, ValueError):
        return None


# The knowledge-graph half of the schema, as one string so it has exactly one definition.
# ``PlanStore`` appends it to the base script, and ``apply_knowledge_graph_schema`` runs it
# alone on a caller's connection -- which is what lets a test build an in-memory database from
# the real DDL instead of a copy that silently drifts from it.
KNOWLEDGE_GRAPH_DDL = """
                CREATE TABLE IF NOT EXISTS knowledge_entities (
                    id            TEXT PRIMARY KEY,
                    name          TEXT NOT NULL,
                    type          TEXT NOT NULL
                                  CHECK (type IN ('symbol','rule','task','file')),
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS knowledge_synapses (
                    source_id     TEXT NOT NULL,
                    target_id     TEXT NOT NULL,
                    relation_type TEXT NOT NULL
                                  CHECK (relation_type IN ('calls','modifies','enforces','invalidates')),
                    weight        REAL NOT NULL DEFAULT 1.0,
                    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (source_id, target_id, relation_type),
                    FOREIGN KEY (source_id) REFERENCES knowledge_entities(id) ON DELETE CASCADE,
                    FOREIGN KEY (target_id) REFERENCES knowledge_entities(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_synapses_source ON knowledge_synapses(source_id);
                CREATE INDEX IF NOT EXISTS idx_synapses_target ON knowledge_synapses(target_id);
"""


def apply_knowledge_graph_schema(connection: sqlite3.Connection) -> None:
    """Create the knowledge-graph tables on a caller's connection.

    The same DDL ``PlanStore`` applies, so a test can build an in-memory database with the
    real schema. The caller owns the connection and its pragmas -- ``PRAGMA foreign_keys=ON``
    in particular, without which the cascading deletes the graph relies on never fire.
    """
    connection.executescript(KNOWLEDGE_GRAPH_DDL)


# The skill registry (Phase 7.5): procedural playbooks injected into a node's system prompt.
# ``target_capabilities`` is a JSON array matched against a node's declaration by a JSON1 set
# intersection (``orchestration.retriever.skills_for_capabilities``) -- one query, and SQLite
# makes the match rather than Python reading the whole table.
SKILLS_DDL = """
                CREATE TABLE IF NOT EXISTS skills (
                    id                  TEXT PRIMARY KEY,
                    name                TEXT NOT NULL,
                    target_capabilities TEXT NOT NULL DEFAULT '[]',
                    markdown_content    TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_skills_capabilities ON skills(id);
"""

# The built-ins a fresh database starts with. A skill is *data*: the registry is a table, so a
# playbook can be added or edited without touching Python -- which is the point of the phase.
DEFAULT_SKILLS = (
    (
        "TDD_Execution_Skill",
        "Test-Driven Execution",
        ("exec", "fs"),
        "## Test-Driven Execution\n\n"
        "You have the shell and the filesystem. Work test-first, in this order:\n\n"
        "1. **Write the test before the code.** Create the test file for the deliverable with "
        "`create_file` (it refuses an existing path) and make it fail for the right reason.\n"
        "2. **Run it.** Execute the suite in the shell and read the failure. A suite that cannot "
        "be collected is not a failing test -- fix the collection error before anything else.\n"
        "3. **Implement the smallest change that passes.** Prefer `edit_ast_node` to rewriting a "
        "file: replace exactly the symbol you mean and leave the rest untouched.\n"
        "4. **Re-run the test.** An exit code of 0 is the only evidence of success. Never report "
        "a task as done from a run that did not finish, and never report a run you did not make.\n"
        "5. **Record the receipt.** The engine refuses to mark a task that ran commands as "
        "completed unless its test receipt exited 0. If the suite cannot run, say so plainly -- "
        "an unverified task is refused, not accepted.\n",
    ),
)


def apply_skills_schema(connection: sqlite3.Connection, *, seed: bool = True) -> None:
    """Create the skill registry on a caller's connection, optionally seeding the built-ins.

    Mirrors :func:`apply_knowledge_graph_schema`: a test can build the real schema on an
    in-memory database instead of a copy that drifts from it.
    """
    connection.executescript(SKILLS_DDL)
    if not seed:
        return
    for skill_id, name, capabilities, content in DEFAULT_SKILLS:
        connection.execute(
            "INSERT OR IGNORE INTO skills (id, name, target_capabilities, markdown_content)"
            " VALUES (?, ?, ?, ?)",
            (skill_id, name, json.dumps(list(capabilities)), content),
        )


class PlanStore:
    """The SQLite state kernel: one plan DAG per ``plan_id``.

    Every method opens its own short-lived connection, so the store is safe to share
    across the workflow thread, the webview thread and the tests. WAL is a property of
    the database file, set once when the schema is created.
    """

    def __init__(self, path: str):
        self._path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._ensure_schema()

    @property
    def path(self) -> str:
        return self._path

    # -- connection / schema -----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        """A short-lived connection that is always closed.

        ``with sqlite3.connect(...)`` ends a *transaction*, not the connection, so a
        leaked handle keeps the database file locked (and blocks a temp-dir cleanup on
        Windows). This closes it in every path.
        """
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id       TEXT PRIMARY KEY,
                    plan_file     TEXT NOT NULL DEFAULT 'PLAN.md',
                    title         TEXT NOT NULL DEFAULT '',
                    goal          TEXT NOT NULL DEFAULT '',
                    version       TEXT NOT NULL DEFAULT '1.0',
                    state_summary TEXT,
                    created_at    REAL NOT NULL,
                    updated_at    REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    plan_id        TEXT NOT NULL,
                    id             TEXT NOT NULL,
                    title          TEXT NOT NULL DEFAULT '',
                    description    TEXT NOT NULL DEFAULT '',
                    status         TEXT NOT NULL DEFAULT 'pending'
                                   CHECK (status IN ('pending','planned','in_progress','completed','failed')),
                    section        TEXT NOT NULL DEFAULT '',
                    section_id     TEXT NOT NULL DEFAULT '',
                    tag            TEXT,
                    assigned_agent TEXT,
                    ast_targets    TEXT NOT NULL DEFAULT '[]',
                    -- Declared by the Architect at creation (Phase 7.4). Present in the CREATE
                    -- text for a fresh database; ``_MIGRATIONS`` adds it to an older one.
                    required_capabilities TEXT NOT NULL DEFAULT '[]',
                    -- Phase 7.5: the engine's verification receipt. 1 only when the task ran
                    -- commands and produced a test receipt that exited 0.
                    verified       INTEGER NOT NULL DEFAULT 0,
                    details        TEXT NOT NULL DEFAULT '[]',
                    files          TEXT NOT NULL DEFAULT '[]',
                    behavioral_log TEXT NOT NULL DEFAULT '[]',
                    sub_steps      TEXT NOT NULL DEFAULT '[]',
                    order_index    INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (plan_id, id),
                    FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_order ON tasks(plan_id, order_index);

                CREATE TABLE IF NOT EXISTS task_dependencies (
                    plan_id    TEXT NOT NULL,
                    task_id    TEXT NOT NULL,
                    depends_on TEXT NOT NULL,
                    PRIMARY KEY (plan_id, task_id, depends_on),
                    FOREIGN KEY (plan_id, task_id) REFERENCES tasks(plan_id, id) ON DELETE CASCADE,
                    FOREIGN KEY (plan_id, depends_on) REFERENCES tasks(plan_id, id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_deps_task ON task_dependencies(plan_id, task_id);

                CREATE TABLE IF NOT EXISTS artifacts (
                    plan_id    TEXT NOT NULL,
                    task_id    TEXT NOT NULL,
                    payload    TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (plan_id, task_id),
                    FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
                );

                """
                + KNOWLEDGE_GRAPH_DDL
                + SKILLS_DDL
                + TELEMETRY_DDL
            )
            self._migrate(connection)
            self._seed_skills(connection)

    # -- migrations ---------------------------------------------------------------
    # ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already exists, so a column
    # added to the CREATE text never reaches a database created before it. A guarded ``ALTER``
    # is the whole mechanism: two columns do not warrant a migration framework, and this is the
    # first one the project has needed.
    _MIGRATIONS = (
        ("rejection_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("rejection_feedback", "TEXT NOT NULL DEFAULT '[]'"),
        # Phase 7: what the Architect says the work costs, and what it needs to do it.
        ("complexity_score", "INTEGER NOT NULL DEFAULT 1"),
        ("required_capabilities", "TEXT NOT NULL DEFAULT '[]'"),
        # Phase 9: how many times the *machine* failed on this node. Kept apart from
        # ``rejection_attempts`` on purpose -- a human's critique and an OOM are different events
        # with different budgets, and conflating them bricks a branch for a transient fault.
        ("system_failures", "INTEGER NOT NULL DEFAULT 0"),
        # Phase 7.5: the engine's verification receipt for this node.
        ("verified", "INTEGER NOT NULL DEFAULT 0"),
    )

    def _migrate(self, connection: sqlite3.Connection) -> None:
        """Add any missing column, once. Idempotent: the guard is ``PRAGMA table_info``.

        Without the guard the second startup raises ``duplicate column name``, and this runs on
        every store construction -- so the check is not a nicety, it is what makes the migration
        safe to re-run.
        """
        present = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}
        for column, declaration in self._MIGRATIONS:
            if column in present:
                continue
            connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} {declaration}")

    def _seed_skills(self, connection: sqlite3.Connection) -> None:
        """Insert the built-in skills, once. Idempotent: ``INSERT OR IGNORE`` on the primary key.

        A skill is *data*, not a Python constant read at import time: the registry is a table, so
        a playbook can be added or edited without a code change -- which is the whole point of
        Phase 7.5. The seed only supplies the built-ins a fresh database starts with.
        """
        for skill_id, name, capabilities, content in DEFAULT_SKILLS:
            connection.execute(
                "INSERT OR IGNORE INTO skills (id, name, target_capabilities, markdown_content)"
                " VALUES (?, ?, ?, ?)",
                (skill_id, name, _dumps(list(capabilities)), content),
            )

    # -- reads -------------------------------------------------------------------
    def has_plan(self, plan_id: str) -> bool:
        """Whether the store already holds this plan."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return row is not None

    def list_plan_ids(self) -> List[str]:
        """Every plan id the store holds."""
        with self._connection() as connection:
            rows = connection.execute("SELECT plan_id FROM plans ORDER BY plan_id").fetchall()
        return [row["plan_id"] for row in rows]

    def get_dag(self, plan_id: str) -> Optional[PlanDAG]:
        """The plan's DAG, or ``None`` when it is not stored."""
        with self._connection() as connection:
            plan_row = connection.execute(
                "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if plan_row is None:
                return None
            task_rows = connection.execute(
                "SELECT * FROM tasks WHERE plan_id = ? ORDER BY order_index", (plan_id,)
            ).fetchall()
            dep_rows = connection.execute(
                "SELECT task_id, depends_on FROM task_dependencies WHERE plan_id = ?",
                (plan_id,),
            ).fetchall()

        dependencies: Dict[str, List[str]] = {}
        for row in dep_rows:
            dependencies.setdefault(row["task_id"], []).append(row["depends_on"])

        order: List[str] = []
        nodes: Dict[str, TaskNode] = {}
        for row in task_rows:
            node_id = row["id"]
            order.append(node_id)
            nodes[node_id] = TaskNode(
                id=node_id,
                plan_id=plan_id,
                title=row["title"],
                description=row["description"],
                status=row["status"],
                dependencies=dependencies.get(node_id, []),
                ast_targets=[str(a) for a in _loads_list(row["ast_targets"])],
                assigned_agent=row["assigned_agent"],
                required_capabilities=[
                    str(name) for name in _loads_list(row["required_capabilities"])
                ],
                section=row["section"],
                section_id=row["section_id"],
                tag=row["tag"],
                details=[str(d) for d in _loads_list(row["details"])],
                files=[str(f) for f in _loads_list(row["files"])],
                behavioral_log=[str(b) for b in _loads_list(row["behavioral_log"])],
                sub_steps=[SubStep.model_validate(sub) for sub in _loads_list(row["sub_steps"])],
                order_index=int(row["order_index"]),
            )

        return PlanDAG(
            plan_id=plan_id,
            goal=plan_row["goal"],
            nodes=nodes,
            created_at=float(plan_row["created_at"]),
            updated_at=float(plan_row["updated_at"]),
            version=plan_row["version"],
            plan_file=plan_row["plan_file"],
            title=plan_row["title"],
            state_summary=_loads_summary(plan_row["state_summary"]),
            order=order,
        )

    # -- artifacts ---------------------------------------------------------------
    def save_artifact(self, plan_id: str, task_id: str, payload: Dict[str, Any]) -> None:
        """Store the ImplementationPlanArtifact a ``planned`` task is halted on.

        Persisted rather than held in memory: the approval may arrive from a different
        request, minutes later, and the executor must be able to read back the exact plan
        a human approved.
        """
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (plan_id, task_id, payload, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(plan_id, task_id) DO UPDATE SET
                    payload = excluded.payload, created_at = excluded.created_at
                """,
                (plan_id, task_id, json.dumps(payload, ensure_ascii=True), time.time()),
            )

    def get_artifact(self, plan_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        """The stored artifact for a task, or ``None`` when it was never planned."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM artifacts WHERE plan_id = ? AND task_id = ?",
                (plan_id, task_id),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["payload"])
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    # -- writes ------------------------------------------------------------------
    def save_dag(self, dag: PlanDAG, *, sync_edges: bool = False) -> PlanDAG:
        """Persist a whole DAG atomically: the plan row, its tasks, and its edges.

        One transaction, so a reader never sees a plan whose tasks and edges disagree.

        ``sync_edges`` is off by default, and that is the point: ``task_dependencies`` is the
        source of truth for topology, not a cache of a field on the node. A status write has
        no opinion about edges, and reconciling the table from whatever the caller's nodes
        happened to carry would let a stale projection erase an edge an explicit
        ``add_task_dependency`` had already committed -- under concurrent writers, silently.
        Reconciliation happens only when it is asked for: the first write of a plan, where
        the incoming document *is* the definition of the graph. Deleting a task cascades its
        edges through the foreign key, so no dangling row survives either way.
        """
        dag.updated_at = time.time()
        known = set(dag.nodes)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO plans
                        (plan_id, plan_file, title, goal, version, state_summary, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(plan_id) DO UPDATE SET
                        plan_file = excluded.plan_file,
                        title = excluded.title,
                        goal = excluded.goal,
                        version = excluded.version,
                        state_summary = excluded.state_summary,
                        updated_at = excluded.updated_at
                    """,
                    (
                        dag.plan_id,
                        dag.plan_file,
                        dag.title,
                        dag.goal,
                        dag.version,
                        json.dumps(dag.state_summary.model_dump()) if dag.state_summary else None,
                        dag.created_at or dag.updated_at,
                        dag.updated_at,
                    ),
                )
                # Replace the task set: delete the rows that are gone, upsert the rest.
                existing = {
                    row["id"]
                    for row in connection.execute(
                        "SELECT id FROM tasks WHERE plan_id = ?", (dag.plan_id,)
                    ).fetchall()
                }
                for stale in existing - known:
                    connection.execute(
                        "DELETE FROM tasks WHERE plan_id = ? AND id = ?", (dag.plan_id, stale)
                    )
                for index, node in enumerate(dag.ordered_nodes()):
                    connection.execute(
                        """
                        INSERT INTO tasks
                            (plan_id, id, title, description, status, section, section_id, tag,
                             assigned_agent, ast_targets, details, files, behavioral_log, sub_steps,
                             required_capabilities, order_index)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(plan_id, id) DO UPDATE SET
                            title = excluded.title,
                            description = excluded.description,
                            status = excluded.status,
                            section = excluded.section,
                            section_id = excluded.section_id,
                            tag = excluded.tag,
                            assigned_agent = excluded.assigned_agent,
                            ast_targets = excluded.ast_targets,
                            details = excluded.details,
                            files = excluded.files,
                            behavioral_log = excluded.behavioral_log,
                            sub_steps = excluded.sub_steps,
                            required_capabilities = excluded.required_capabilities,
                            order_index = excluded.order_index
                        """,
                        (
                            dag.plan_id,
                            node.id,
                            node.title,
                            node.description,
                            node.status,
                            node.section,
                            node.section_id,
                            node.tag,
                            node.assigned_agent,
                            _dumps(node.ast_targets),
                            _dumps(node.details),
                            _dumps(node.files),
                            _dumps(node.behavioral_log),
                            _dumps([sub.model_dump() for sub in node.sub_steps]),
                            _dumps(node.required_capabilities),
                            index,
                        ),
                    )
                    node.order_index = index
                # Only on creation/import (see the docstring): the document defines the graph
                # then, and never again.
                if sync_edges:
                    connection.execute(
                        "DELETE FROM task_dependencies WHERE plan_id = ?", (dag.plan_id,)
                    )
                    for node in dag.ordered_nodes():
                        for blocker in node.dependencies:
                            if blocker in known and blocker != node.id:
                                connection.execute(
                                    """
                                    INSERT OR IGNORE INTO task_dependencies (plan_id, task_id, depends_on)
                                    VALUES (?, ?, ?)
                                    """,
                                    (dag.plan_id, node.id, blocker),
                                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dag

    def delete_plan(self, plan_id: str) -> None:
        """Remove a plan and (by cascade) its tasks and edges."""
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM artifacts WHERE plan_id = ?", (plan_id,))
                connection.execute("DELETE FROM task_dependencies WHERE plan_id = ?", (plan_id,))
                connection.execute("DELETE FROM tasks WHERE plan_id = ?", (plan_id,))
                connection.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def add_task_dependency(self, plan_id: str, task_id: str, depends_on: str) -> bool:
        """Add one blocker edge: ``task_id`` now depends on ``depends_on``.

        A straight INSERT into ``task_dependencies``, because that table *is* the topology.
        Writing it through a node blob and hoping a later save reproduces it is the
        split-brain this replaced: two representations of one edge, one of which wins by
        accident of write order. Unknown ids and a self-edge are refused; a duplicate is a
        no-op.
        """
        dag = self.get_dag(plan_id)
        if dag is None:
            return False
        child, parent = str(task_id), str(depends_on)
        if child not in dag.nodes or parent not in dag.nodes or child == parent:
            return False
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO task_dependencies (plan_id, task_id, depends_on)"
                " VALUES (?, ?, ?)",
                (plan_id, child, parent),
            )
        return True

    def remove_task_dependency(self, plan_id: str, task_id: str, depends_on: str) -> bool:
        """Remove one blocker edge. A missing edge is a no-op."""
        with self._lock, self._connection() as connection:
            connection.execute(
                "DELETE FROM task_dependencies"
                " WHERE plan_id = ? AND task_id = ? AND depends_on = ?",
                (plan_id, str(task_id), str(depends_on)),
            )
        return True

    def blocked_task_ids(self, plan_id: str) -> List[str]:
        """Ids of tasks whose blockers are not all ``completed`` -- the ineligible set.

        One join, so eligibility is decided by the database rather than re-derived by a
        caller from a projection. This is the single answer to "is this node allowed to
        run?", and every scheduler reads it instead of deciding for itself.
        """
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT td.task_id AS task_id
                FROM task_dependencies td
                JOIN tasks parent
                  ON parent.plan_id = td.plan_id AND parent.id = td.depends_on
                WHERE td.plan_id = ? AND parent.status != 'completed'
                """,
                (plan_id,),
            ).fetchall()
        return [row["task_id"] for row in rows]

    def is_task_eligible(self, plan_id: str, task_id: str) -> bool:
        """Whether every blocker of ``task_id`` is ``completed``. No blockers -> eligible."""
        return str(task_id) not in set(self.blocked_task_ids(plan_id))

    def incomplete_blockers(self, plan_id: str, task_id: str) -> List[str]:
        """The blockers of ``task_id`` that are not yet ``completed``, for a refusal message."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT parent.id AS id
                FROM task_dependencies td
                JOIN tasks parent
                  ON parent.plan_id = td.plan_id AND parent.id = td.depends_on
                WHERE td.plan_id = ? AND td.task_id = ? AND parent.status != 'completed'
                """,
                (plan_id, str(task_id)),
            ).fetchall()
        return [row["id"] for row in rows]

    @contextmanager
    def transaction(self):
        """One write transaction on one connection, for callers that must link several writes.

        A single status write already runs in its own transaction, but the graph write that
        records what a task touched must be *durably linked* to it: if the two commit
        separately, a crash between them leaves a completed task the graph has never heard of,
        and the planner then reasons over topology that does not match the code on disk. This
        yields the connection so a caller can do both writes and let one COMMIT decide.
        """
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def update_task_status(
        self,
        plan_id: str,
        task_id: str,
        status: str,
        detail_note: Optional[str] = None,
        files: Optional[List[str]] = None,
        connection: Optional[sqlite3.Connection] = None,
        verified: Optional[bool] = None,
    ) -> bool:
        """Set one task's status. Returns False if it is unknown.

        With no ``connection`` this opens its own transaction, which is the ordinary path. When
        one is supplied the write joins **the caller's** transaction: no BEGIN, no COMMIT, no
        ROLLBACK here -- the caller owns the boundary, so several writes can be made to
        succeed or fail together. The caller emits the ``task_state_updated`` event, and only
        after the commit, so the UI is never told about a change the database does not hold.

        ``verified`` writes the engine's verification receipt in the *same* statement as the
        status, so a task cannot be marked ``completed`` without its receipt or the reverse.
        ``None`` leaves the column alone.
        """
        if connection is not None:
            return self._write_task_status(
                connection, plan_id, task_id, status, detail_note, files, verified
            )

        with self._lock, self._connection() as own_connection:
            own_connection.execute("BEGIN IMMEDIATE")
            try:
                written = self._write_task_status(
                    own_connection, plan_id, task_id, status, detail_note, files, verified
                )
                own_connection.execute("COMMIT")
            except Exception:
                own_connection.execute("ROLLBACK")
                raise
        return written

    @staticmethod
    def _write_task_status(
        connection: sqlite3.Connection,
        plan_id: str,
        task_id: str,
        status: str,
        detail_note: Optional[str],
        files: Optional[List[str]],
        verified: Optional[bool] = None,
    ) -> bool:
        """The write itself, on a connection whose transaction the caller controls."""
        row = connection.execute(
            "SELECT details, files FROM tasks WHERE plan_id = ? AND id = ?",
            (plan_id, task_id),
        ).fetchone()
        if row is None:
            return False
        details = [str(d) for d in _loads_list(row["details"])]
        if detail_note:
            details.append(str(detail_note))
        merged_files = [str(f) for f in _loads_list(row["files"])]
        for name in files or []:
            if str(name) not in merged_files:
                merged_files.append(str(name))
        if verified is None:
            connection.execute(
                """
                UPDATE tasks SET status = ?, details = ?, files = ?
                WHERE plan_id = ? AND id = ?
                """,
                (status, _dumps(details), _dumps(merged_files), plan_id, task_id),
            )
        else:
            connection.execute(
                """
                UPDATE tasks SET status = ?, details = ?, files = ?, verified = ?
                WHERE plan_id = ? AND id = ?
                """,
                (status, _dumps(details), _dumps(merged_files), 1 if verified else 0, plan_id, task_id),
            )
        return True

    def verified_state(self, plan_id: str, task_id: str) -> bool:
        """Whether the engine holds a verification receipt for this task."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT verified FROM tasks WHERE plan_id = ? AND id = ?", (plan_id, task_id)
            ).fetchone()
        return bool(row is not None and int(row["verified"] or 0))

    # -- rejection state ----------------------------------------------------------
    # The swarm's retry bookkeeping, persisted. It lives on the *node* -- not on the artifact,
    # which a re-plan replaces, and not in ``TaskNode``, which is projected into the markdown
    # document and the UI payload and has no business carrying orchestration counters.
    def record_rejection(self, plan_id: str, task_id: str, feedback: str = "") -> bool:
        """Append a human critique, increment the attempt count, return the node to ``pending``.

        The critique is **appended**, not replaced: a node rejected twice carries both notes, so
        the next attempt is planned against everything the reviewer said rather than only the last
        remark. The count and the notes move in one transaction, so a crash cannot leave a critique
        recorded without the count that bounds it, or the reverse.
        """
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT rejection_attempts, rejection_feedback FROM tasks"
                    " WHERE plan_id = ? AND id = ?",
                    (plan_id, task_id),
                ).fetchone()
                if row is None:
                    connection.execute("ROLLBACK")
                    return False
                notes = [str(note) for note in _loads_list(row["rejection_feedback"])]
                note = str(feedback or "").strip()
                if note:
                    notes.append(note)
                connection.execute(
                    "UPDATE tasks SET status = 'pending', rejection_attempts = ?,"
                    " rejection_feedback = ? WHERE plan_id = ? AND id = ?",
                    (int(row["rejection_attempts"] or 0) + 1, _dumps(notes), plan_id, task_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return True

    def rejection_state(self, plan_id: str, task_id: str) -> Dict[str, Any]:
        """``{"attempts": int, "feedback": [...]}`` for a node; zeros when it is unknown."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT rejection_attempts, rejection_feedback FROM tasks"
                " WHERE plan_id = ? AND id = ?",
                (plan_id, task_id),
            ).fetchone()
        if row is None:
            return {"attempts": 0, "feedback": []}
        return {
            "attempts": int(row["rejection_attempts"] or 0),
            "feedback": [str(note) for note in _loads_list(row["rejection_feedback"])],
        }

    def record_system_failure(self, plan_id: str, task_id: str) -> int:
        """Increment a node's machine-failure count. Returns the new count, or 0 when unknown.

        Separate from :meth:`record_rejection` because the two failures are not the same event. A
        rejection is a human saying the *work* is wrong; a system failure is the machine dropping a
        pipe, timing out or dying. They have separate budgets, and the caller decides whether the
        node returns to the queue or is finally abandoned -- this only counts.
        """
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT system_failures FROM tasks WHERE plan_id = ? AND id = ?",
                    (plan_id, task_id),
                ).fetchone()
                if row is None:
                    connection.execute("ROLLBACK")
                    return 0
                failures = int(row["system_failures"] or 0) + 1
                connection.execute(
                    "UPDATE tasks SET system_failures = ? WHERE plan_id = ? AND id = ?",
                    (failures, plan_id, task_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return failures

    def system_failure_state(self, plan_id: str, task_id: str) -> Dict[str, Any]:
        """``{"failures": int}`` for a node; zero when it is unknown."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT system_failures FROM tasks WHERE plan_id = ? AND id = ?",
                (plan_id, task_id),
            ).fetchone()
        return {"failures": 0 if row is None else int(row["system_failures"] or 0)}

    def record_assessment(
        self,
        plan_id: str,
        task_id: str,
        complexity_score: int,
        required_capabilities: Optional[List[str]] = None,
    ) -> bool:
        """Write the Architect's assessment of a task onto its node. Phase 7's router reads it.

        On the node rather than only in the artifact: an artifact is replaced by a re-plan, and the
        assessment is a property of the *work*, not of one proposed payload.
        """
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET complexity_score = ?, required_capabilities = ?"
                " WHERE plan_id = ? AND id = ?",
                (
                    int(complexity_score),
                    _dumps([str(name) for name in (required_capabilities or [])]),
                    plan_id,
                    task_id,
                ),
            )
            return cursor.rowcount > 0

    def assessment_state(self, plan_id: str, task_id: str) -> Dict[str, Any]:
        """``{"complexity_score": int, "required_capabilities": [...]}`` for a node."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT complexity_score, required_capabilities FROM tasks"
                " WHERE plan_id = ? AND id = ?",
                (plan_id, task_id),
            ).fetchone()
        if row is None:
            return {"complexity_score": 1, "required_capabilities": []}
        return {
            "complexity_score": int(row["complexity_score"] or 1),
            "required_capabilities": [
                str(name) for name in _loads_list(row["required_capabilities"])
            ],
        }


    def render_plan_markdown(self, plan_id: str) -> str:
        """The human-readable markdown projection of a stored plan (read-only export).

        This is the only way markdown is produced now: it is rendered *from* the store
        for display and never parsed back to decide state. The compiler is the compiled
        core's, so the projection is byte-for-byte what the app has always emitted.
        """
        dag = self.get_dag(plan_id)
        if dag is None:
            return ""
        from tools.plan_parser import compile_plan_json_to_markdown

        return compile_plan_json_to_markdown(dag_to_plan_dict(dag))

    def topological_order(self, plan_id: str) -> List[str]:
        """Node ids in dependency order (blockers first).

        A node with an unsatisfied blocker is placed after it; a cycle is broken by
        falling back to document order for whatever the sort could not place, so a
        malformed graph still yields every node exactly once.
        """
        dag = self.get_dag(plan_id)
        if dag is None:
            return []
        indegree = {node_id: 0 for node_id in dag.nodes}
        for node in dag.nodes.values():
            for blocker in node.dependencies:
                if blocker in dag.nodes and blocker != node.id:
                    indegree[node.id] += 1
        ready = [node_id for node_id in dag.order if indegree.get(node_id, 0) == 0]
        ordered: List[str] = []
        while ready:
            node_id = ready.pop(0)
            ordered.append(node_id)
            for other in dag.order:
                if node_id in dag.nodes[other].dependencies:
                    indegree[other] -= 1
                    if indegree[other] == 0:
                        ready.append(other)
        for node_id in dag.order:
            if node_id not in ordered:
                ordered.append(node_id)
        return ordered


# ------------------------------------------------------------------ process-wide access

_STORES: Dict[str, PlanStore] = {}
_STORES_LOCK = threading.Lock()


def default_db_path() -> str:
    """The database path beside the active plan (imported lazily to avoid a cycle)."""
    from tools.workspace import get_plan_dir

    return os.path.join(get_plan_dir(), DB_FILENAME)


def get_store(path: Optional[str] = None) -> PlanStore:
    """The cached store for ``path`` (the active plan directory by default)."""
    resolved = os.path.abspath(path or default_db_path())
    with _STORES_LOCK:
        store = _STORES.get(resolved)
        if store is None:
            store = PlanStore(resolved)
            _STORES[resolved] = store
        return store


def reset_stores() -> None:
    """Drop the cached stores (the test suite points the plan dir at a new temp dir)."""
    with _STORES_LOCK:
        _STORES.clear()
