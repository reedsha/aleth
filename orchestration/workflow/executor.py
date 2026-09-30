"""The execution phase: apply an approved artifact, deterministically, through MCP.

Once a human approves an artifact the task is ``in_progress`` and this module applies it.
It is deliberately dumb, and it is now also **MCP-routed**: every read and every write
crosses the Model Context Protocol boundary to a filesystem server spawned for the pass and
killed when it ends (see :mod:`tools.mcp_client`). This module holds no file handle of its
own, so the containment rule -- "nothing outside the workspace" -- lives in exactly one
place, the server, instead of being re-implemented at each call site.

**No model call, no routing decision, no second guess.** The artifact carries the exact code
to inject, so applying a plan is a splice and nothing else.

The three shapes a target takes:

* ``delete`` -- the recorded span is removed (``content`` is ignored);
* a named symbol -- ``content`` replaces exactly that node's bytes, so unrelated lines are
  never touched;
* an unnamed target (or ``insert``) -- ``content`` becomes the whole file, which is what an
  approved plan for a file that does not exist yet means.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

from pydantic import BaseModel, ConfigDict

from tools.mcp_client import MCPClient, mcp_workspace_client


class AppliedEdit(BaseModel):
    """One target's outcome, so a caller can report exactly what landed."""

    model_config = ConfigDict(extra="forbid", strict=False)

    file_path: str = ""
    symbol_name: str = ""
    operation: str = ""
    success: bool = False
    error: str = ""


class ExecutionResult(BaseModel):
    """The outcome of applying one artifact."""

    model_config = ConfigDict(extra="forbid", strict=False)

    success: bool
    error: str = ""
    plan_id: str = ""
    task_id: str = ""
    applied: List[AppliedEdit] = []


def _find_unit(source: str, file_path: str, symbol: str) -> Any:
    """The AST unit a symbol names, resolved **from text** -- no file read needed.

    ``chunk_source`` parses a string and tags each unit's byte span, which is what lets the
    executor resolve a symbol after the bytes have already arrived over MCP: the AST work is
    the orchestrator's, the file access is the server's, and neither reaches into the other.
    """
    from tools.ast_chunker import chunk_source

    units = chunk_source(source, file_path)
    for unit in units:
        if unit.qualified_name == symbol:
            return unit
    for unit in units:
        if unit.name == symbol:
            return unit
    return None


def _apply_one(target: Mapping[str, Any], *, client: MCPClient) -> AppliedEdit:
    """Apply one target through the MCP server, or return the refusal that stopped it."""
    from tools.ast_editor import splice_bytes
    from tools.ast_chunker import parse_has_errors
    from tools.execution_gate import guard_write

    file_path = str(target.get("file_path") or "")
    symbol_name = str(target.get("symbol_name") or "")
    operation = str(target.get("operation") or "replace")
    content = str(target.get("content") or "")

    def refusal(error: str) -> AppliedEdit:
        return AppliedEdit(
            file_path=file_path, symbol_name=symbol_name, operation=operation,
            success=False, error=error,
        )

    try:
        # The Artifact Gate, re-checked at the executor boundary: a write aimed at a file a
        # ``planned`` task still targets is refused here even if it reached this far.
        guard_write(file_path)
    except Exception as error:
        return refusal(str(error))

    try:
        if operation == "delete":
            source = client.read_file(file_path)
            span = list(target.get("byte_range") or [])
            if len(span) != 2:
                return refusal("a delete target needs a two-element byte_range")
            updated = splice_bytes(source, int(span[0]), int(span[1]), "")
            client.write_file(file_path, updated)
            return AppliedEdit(file_path=file_path, symbol_name=symbol_name, operation=operation, success=True)

        if symbol_name:
            source = client.read_file(file_path)
            unit = _find_unit(source, file_path, symbol_name)
            if unit is None:
                return refusal(f"no symbol {symbol_name!r} in {file_path}")
            updated = splice_bytes(source, unit.byte_start, unit.byte_end, content)
            # A splice that leaves the file unparseable is refused before it is written, so a
            # broken edit cannot reach the disk.
            if parse_has_errors(updated, file_path, unit.language):
                return refusal(f"the edit would leave {file_path} with a syntax error")
            client.write_file(file_path, updated)
            return AppliedEdit(file_path=file_path, symbol_name=symbol_name, operation=operation, success=True)

        # An unnamed target (or an insert): the artifact's content is the whole file.
        client.write_file(file_path, content)
        return AppliedEdit(file_path=file_path, symbol_name="", operation=operation, success=True)
    except Exception as error:  # an executor reports; it does not crash the run
        return refusal(str(error))


def apply_artifact(
    artifact: Mapping[str, Any],
    *,
    workspace_dir: str,
    client: Optional[MCPClient] = None,
) -> ExecutionResult:
    """Apply every target in an approved artifact. Deterministic; never raises.

    ``client`` is the live MCP session. When it is omitted one is opened for this call and
    torn down at the end, so a direct caller needs no session of its own; the run path opens
    one session for the whole artifact (see :func:`execute_approved`), which is one child
    process per execution pass rather than one per target.
    """
    if client is None:
        with mcp_workspace_client(workspace_dir) as opened:
            return apply_artifact(artifact, workspace_dir=workspace_dir, client=opened)

    plan_id = str(artifact.get("plan_id") or "")
    task_id = str(artifact.get("task_id") or "")
    applied = [_apply_one(target, client=client) for target in (artifact.get("ast_targets") or [])]

    failures = [entry for entry in applied if not entry.success]
    return ExecutionResult(
        success=not failures,
        error="" if not failures else f"{len(failures)} target(s) could not be applied",
        plan_id=plan_id,
        task_id=task_id,
        applied=applied,
    )


def execute_approved(plan_id: str, task_id: str, *, workspace_dir: str) -> ExecutionResult:
    """Load the approved artifact for a task and apply it through MCP.

    Refuses when there is no artifact (the task was never planned) or when the task is not
    ``in_progress`` (it was never approved) -- so execution cannot be triggered around the
    approval. The MCP server is spawned here and killed when the pass returns: an ephemeral
    child, not a daemon.
    """
    from storage.db import get_store

    store = get_store()
    dag = store.get_dag(plan_id)
    if dag is None:
        return ExecutionResult(success=False, error=f"No plan {plan_id!r} in the store.")
    node = dag.nodes.get(task_id)
    if node is None:
        return ExecutionResult(success=False, error=f"No task {task_id!r} in plan {plan_id!r}.")
    if node.status != "in_progress":
        return ExecutionResult(
            success=False,
            error=f"Task {task_id!r} is {node.status!r}, not 'in_progress'; approval is required.",
        )

    artifact = store.get_artifact(plan_id, task_id)
    if artifact is None:
        return ExecutionResult(
            success=False, plan_id=plan_id, task_id=task_id,
            error=f"No approved artifact for {task_id!r}.",
        )

    with mcp_workspace_client(workspace_dir) as client:
        return apply_artifact(artifact, workspace_dir=workspace_dir, client=client)
