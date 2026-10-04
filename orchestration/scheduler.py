"""The DAG scheduler: which nodes may run right now.

Phase 6 replaces a linear ``while uncompleted_tasks`` walk with a swarm, and the swarm needs one
thing from the state kernel before it can do anything: **the set of nodes that are executable at
this instant**. That is all this module answers.

A node is executable when two things are true:

1. it is ``pending`` -- not already done, not halted awaiting approval (``planned``), not already
   being worked on (``in_progress``), not failed and waiting for attention;
2. every node it depends on is ``completed``.

Both conditions are decided in **one query**, by the database. Deriving eligibility in Python
would mean reading the whole graph, rebuilding the dependency map and hoping the two agree; the
SQL asks the store the question it already holds the answer to, and ``task_dependencies`` is the
single source of truth for the edges (Phase 3.5).

A node with **no** dependencies satisfies the second condition vacuously, which is what makes a
fresh plan start: every root is executable immediately, and they are all returned together so the
swarm can run them concurrently. That is the whole point -- the old loop took them one at a time.

Ordering is by ``order_index``, the document order, so a dispatch is reproducible: two runs of the
same plan select the same nodes in the same sequence, and a test can assert on it.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

# The ceiling the parallel dispatcher enforces. Declared here because it belongs to the
# scheduler's contract -- how many nodes a tick may dispatch, and how many times a rejected node
# may come back -- rather than to whichever executor happens to read it.
DEFAULT_MAX_WORKERS = 4
# Retries, not attempts: a budget of 1 means the node runs, is rejected once, and runs once more.
DEFAULT_MAX_RETRIES = 1
# Machine failures get their own, larger budget. A broken pipe or a timeout is the environment
# misbehaving, not the work being wrong, and it must not consume a human's rejection budget -- nor
# brick a branch on the first transient fault.
DEFAULT_MAX_SYSTEM_RETRIES = 3

# The executable set, as one statement.
#
# ``NOT EXISTS`` rather than a join: a node with three blockers has three rows in a join, and a
# node with none would vanish from an inner join entirely. The correlated subquery asks "is there
# a blocker that is not complete", which is false for a node with no blockers at all -- exactly
# the semantics a DAG's roots need.
_EXECUTABLE_SQL = """
SELECT
    t.id,
    t.title,
    t.section,
    t.section_id,
    t.tag,
    t.files,
    t.order_index,
    -- Phase 7/9: the dispatcher routes a node from these, and the critique rides along for the
    -- retry prompt. Hydrated here, in the one query that already reads the node, so routing and
    -- feedback cost no extra round trip per node -- a scalar SELECT in the dispatch loop is the
    -- N+1 this exists to prevent.
    t.complexity_score,
    t.rejection_attempts,
    t.rejection_feedback,
    -- Phase 7.4: what the node says it needs. Read here so the worker can be handed exactly the
    -- tools this node asked for, without a second query in the dispatch loop.
    t.required_capabilities,
    COALESCE((
        SELECT group_concat(td.depends_on)
        FROM task_dependencies td
        WHERE td.plan_id = t.plan_id AND td.task_id = t.id
    ), '') AS blockers
FROM tasks t
WHERE t.plan_id = ?
  AND t.status = 'pending'
  -- A node a human has rejected past its retry budget is still ``pending`` -- the state kernel is
  -- not told a lie to work around a retry policy -- but it vanishes from the dispatch pool. It is
  -- dead without being mislabelled.
  --
  -- ``<=`` because ``max_retries`` means *retries*: 0 attempts runs, the first rejection is the
  -- retry, and the rejection past the budget blocks. ``<`` would make a budget of 1 refuse the very
  -- retry it names, and bending the constant to compensate would be an off-by-one hidden in config.
  AND t.rejection_attempts <= ?
  -- The machine-failure budget, enforced by the same query that admits nodes (``system_failures``
  -- is otherwise only checked by the handler whose write may have failed). ``<`` because the count
  -- is a *count of failures already had*: at ``max_system_retries`` the budget is spent, and only
  -- a crash between the count and the status flip can leave such a node ``pending`` -- where it
  -- must not be re-dispatched.
  AND t.system_failures < ?
  AND NOT EXISTS (
      SELECT 1
      FROM task_dependencies td
      JOIN tasks parent
        ON parent.plan_id = td.plan_id AND parent.id = td.depends_on
      WHERE td.plan_id = t.plan_id
        AND td.task_id = t.id
        AND parent.status != 'completed'
  )
ORDER BY t.order_index ASC, t.id ASC
"""


def resolve_plan_id(plan_id: Optional[str] = None) -> str:
    """The plan to schedule, defaulting to the active one."""
    from storage.db import plan_id_for
    from tools.workspace import get_active_plan_filename

    return plan_id or plan_id_for(get_active_plan_filename())


def _decode_list(raw: Any) -> List[str]:
    """A JSON-list column (``files``, ``rejection_feedback``) as a list of strings.

    A malformed or absent value is an empty list, not a crash: a dispatch query must not fail
    because one column was written badly.
    """
    import json

    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(name) for name in value] if isinstance(value, list) else []


def get_executable_nodes(
    connection: sqlite3.Connection,
    plan_id: Optional[str] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_system_retries: int = DEFAULT_MAX_SYSTEM_RETRIES,
) -> List[Dict[str, Any]]:
    """Every pending node whose blockers are all completed, in document order.

    Returns one dictionary per node: ``id``, ``title``, ``section``, ``section_id``, ``tag``,
    ``files``, the ``blockers`` it waited on, and the routing state -- ``complexity_score``,
    ``rejection_attempts``, ``rejection_feedback`` and ``required_capabilities`` -- read in this
    same query so the caller can route the node, build its retry prompt and scope its tools without
    a second read. An unknown plan, or a plan with nothing runnable, is an empty list -- "nothing to
    dispatch" is an ordinary answer, not an error.

    ``max_retries`` bounds re-dispatch after a human rejection: a node whose ``rejection_attempts``
    has reached it is excluded, so the dispatch pool cannot spin on output a reviewer has already
    refused. It remains ``pending`` in the table -- the swarm's policy is the swarm's, and the
    canonical graph state is not rewritten to express it.

    The caller owns the connection. This reads and nothing else: dispatching is the swarm's job,
    and the human approval gate still stands between a plan and its execution.
    """
    resolved = resolve_plan_id(plan_id)
    rows = connection.execute(
        _EXECUTABLE_SQL,
        (resolved, int(max_retries), int(max_system_retries)),
    ).fetchall()

    nodes: List[Dict[str, Any]] = []
    for row in rows:
        blockers = [name for name in str(row["blockers"] or "").split(",") if name]
        nodes.append({
            "id": str(row["id"]),
            "title": str(row["title"] or ""),
            "section": str(row["section"] or ""),
            "section_id": str(row["section_id"] or ""),
            "tag": row["tag"],
            "files": _decode_list(row["files"]),
            "order_index": int(row["order_index"] or 0),
            "blockers": blockers,
            # Left as stored, ``None`` included: an unusable assessment is the router's problem to
            # handle (it routes such a node to the top tier and says so), not this query's to hide.
            "complexity_score": row["complexity_score"],
            "rejection_attempts": row["rejection_attempts"],
            "rejection_feedback": _decode_list(row["rejection_feedback"]),
            # An empty list is a real declaration -- "this node needs no tools" -- and is passed
            # through as one rather than treated as absent.
            "required_capabilities": _decode_list(row["required_capabilities"]),
        })
    return nodes


def get_executable_node_ids(
    connection: sqlite3.Connection,
    plan_id: Optional[str] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_system_retries: int = DEFAULT_MAX_SYSTEM_RETRIES,
) -> List[str]:
    """:func:`get_executable_nodes` reduced to the ids, for a caller that only dispatches."""
    return [
        node["id"]
        for node in get_executable_nodes(
            connection, plan_id, max_retries, max_system_retries
        )
    ]


def is_executable(
    connection: sqlite3.Connection,
    task_id: str,
    plan_id: Optional[str] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_system_retries: int = DEFAULT_MAX_SYSTEM_RETRIES,
) -> bool:
    """Whether one node is in the executable set right now.

    A convenience for a caller holding a node rather than the whole set; it asks the same query,
    so it cannot disagree with :func:`get_executable_nodes` about eligibility.
    """
    return str(task_id) in set(
        get_executable_node_ids(connection, plan_id, max_retries, max_system_retries)
    )
