"""A scripted planner for the swarm tests, importable by a child process.

It exists because a worker runs in another interpreter: a closure or a mock cannot cross the
boundary, so the scripted decision travels as the reference ``tests.stub_planner:scripted`` in the
role descriptor. The child imports it and calls it for real -- the MCP transport, the session and
the database are all genuine; only the model's *answer* is canned, which is the only part that
cannot be real offline.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, List, Optional


def _artifact_for(task_id: str) -> str:
    """The artifact JSON a scripted model returns for a node.

    The assessment fields are mandatory on ``ImplementationPlanArtifact``, so a scripted planner that
    omitted them would be refused by validation -- which is the Phase 7 contract working, not a
    fixture quirk.
    """
    return json.dumps({
        "plan_id": os.environ.get("STUB_PLAN_ID", "PLAN"),
        "task_id": task_id,
        "summary": f"Scripted plan for {task_id}",
        "estimated_impact": "one file",
        "complexity_score": 2,
        "required_capabilities": [],
        "ast_targets": [{
            "file_path": f"{task_id}.py",
            "symbol_name": "",
            "byte_range": [0, 0],
            "operation": "insert",
            "content": f"# generated for {task_id}\n",
        }],
    })


def _tool_names(tools: Optional[List[Any]]) -> List[str]:
    """The names in the provider-facing tool schemas the loop handed the model."""
    names = []
    for tool in (tools or []):
        if isinstance(tool, dict):
            names.append(str((tool.get("function") or {}).get("name") or ""))
        else:
            names.append(str(getattr(tool, "name", "") or ""))
    return sorted(name for name in names if name)


def scripted(*, system: str = "", messages: Optional[List[Any]] = None,
             tools: Optional[List[Any]] = None, model: str = "", user: str = "", **_kwargs: Any) -> Any:
    """Answer with a canned artifact for whichever node is being planned.

    Works as both completer shapes: the agent loop calls it with ``messages``, the single-shot
    path with ``user``. The node id is recovered from the prompt text, so the same function serves
    every node in the graph.

    When ``STUB_PROMPT_LOG`` names a file, the prompt it was handed is appended there. That is how
    a test proves a human's rejection actually reached the *child's* prompt -- the parent cannot
    see across the process boundary, so the evidence has to be written from inside it.

    ``STUB_TOOLS_LOG`` does the same for the tool set: one ``"<count>:<names>"`` line per call, so
    a test can prove the context diet reached the child -- a node that declared no capabilities is
    offered no tools at all.
    """
    text = str(user or "")
    if not text and messages:
        text = "\n".join(str(message.get("content", "")) for message in messages)

    log_path = os.environ.get("STUB_PROMPT_LOG")
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"---PROMPT---\n{text}\n")
        except OSError:
            pass

    tools_log = os.environ.get("STUB_TOOLS_LOG")
    if tools_log:
        names = _tool_names(tools)
        try:
            with open(tools_log, "a", encoding="utf-8") as handle:
                handle.write(f"{len(names)}:{','.join(names)}\n")
        except OSError:
            pass

    task_id = "task-unknown"
    for line in text.splitlines():
        if line.startswith("task_id:"):
            task_id = line.split(":", 1)[1].strip()
            break
    return SimpleNamespace(text=_artifact_for(task_id), model=model or "stub", tool_calls=[])
