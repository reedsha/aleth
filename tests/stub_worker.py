"""Worker stubs that fail across the process boundary, importable by a child process.

A worker runs in another interpreter, so a test cannot hand the pool a closure or a mock -- the
callable has to travel as a reference (``tests.stub_worker:machine_fault``) and be imported and
called for real in the child. That is the point of these stubs: the swarm's failure handling is
what is under test, and the only honest way to reach it is to have a real worker process fail.

Two stubs, one per failure class the swarm segregates:

* :func:`machine_fault` raises a broken pipe -- the *environment* failed. The swarm must count it
  against ``system_failures`` and re-dispatch without a human, up to ``max_system_retries``.
* :func:`work_fault` raises a contract violation -- the *work* failed. The swarm must fail the node
  at once, because a retry would reproduce it.

Each records its invocation to ``$STUB_WORKER_LOG`` so a test can prove how many times the node was
actually dispatched, which is the evidence the parent cannot otherwise see across the boundary.
"""

from __future__ import annotations

import os


def _record(task_id: str) -> None:
    """Append one line per dispatch. Best effort: a missing log must not change the outcome."""
    path = os.environ.get("STUB_WORKER_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{task_id}\n")
    except OSError:
        pass


def machine_fault(task_id: str, role_descriptor: dict, db_path: str) -> object:
    """The environment dropped a pipe. ``BrokenPipeError`` is in ``SYSTEM_FAILURE_TYPES``."""
    _record(task_id)
    raise BrokenPipeError("the MCP server's pipe closed mid-read")


def work_fault(task_id: str, role_descriptor: dict, db_path: str) -> object:
    """The work violated its contract. ``AttributeError`` is deliberately *not* a system failure."""
    _record(task_id)
    raise AttributeError("the planner returned an off-contract payload")
