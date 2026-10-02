"""The MCP boundary: an ephemeral stdio server, and the executor routed through it.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_mcp.py -n0 -q

These are the Phase 4 proofs. They run a real child process over a real pipe -- no mocking of
the transport -- because the thing being tested is the transport and the containment rule it
enforces.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from pathlib import Path

from storage.db import PlanDAG, TaskNode, get_store, reset_stores
from tools import execution_gate, workspace
from tools.mcp_client import (
    MCPClient,
    MCPError,
    default_command,
    default_exec_command,
    mcp_workspace_client,
)

SOURCE = "def alpha():\n    return 1\n"
REPLACEMENT = "def alpha():\n    return 2\n"

IS_WINDOWS = sys.platform == "win32"

# Shapes that only *mean* a traversal on Windows. On POSIX a backslash is an ordinary filename
# character and a drive letter is a relative name, so neither can leave the root -- asserting a
# refusal there would be asserting the wrong thing.
WINDOWS_TRAVERSAL_SHAPES = ("..\\..\\outside.txt", "..\\..\\secret.txt", "C:\\Windows\\win.ini")

# Every command now runs in a Docker container. These tests run on a machine with no Docker
# daemon, so the runtime is the in-repo double (``tests/fake_docker.py``): the argv, the child
# process and the output plumbing are all real, and only the container itself is emulated.
FAKE_DOCKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_docker.py")
FAKE_DOCKER_ENV = {"ALETH_DOCKER_BIN": f"{sys.executable} {FAKE_DOCKER}"}


class MCPServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcp_fs_")
        self._orig_plan_dir = workspace.get_plan_dir()
        self._orig_project = workspace.get_project_dir()
        self._orig_plan = workspace.get_active_plan_filename()
        workspace.PLAN_DIR = self.tmp
        workspace.PROJECT_DIR = self.tmp
        workspace.ACTIVE_PLAN_FILE = "PLAN.md"
        reset_stores()
        # The exec server runs its commands in a container; point it at the double so a test
        # exercises the real path without a daemon. The child inherits this at spawn time.
        env_patcher = mock.patch.dict(os.environ, FAKE_DOCKER_ENV)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        self.addCleanup(self._restore)

    def _restore(self):
        workspace.PLAN_DIR = self._orig_plan_dir
        workspace.PROJECT_DIR = self._orig_project
        workspace.ACTIVE_PLAN_FILE = self._orig_plan
        reset_stores()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    def _read(self, name):
        with open(os.path.join(self.tmp, name), "r", encoding="utf-8", newline="") as handle:
            return handle.read()


class MCPClientTests(MCPServerTestCase):
    def test_the_client_handshakes_and_lists_the_filesystem_tools(self):
        with mcp_workspace_client(self.tmp) as client:
            self.assertEqual(client.server_info.get("serverInfo", {}).get("name"),
                             "aleth-filesystem")
            names = [tool["name"] for tool in client.list_tools()]

        self.assertEqual(sorted(names), ["create_file", "read_file", "write_file"])

    def test_a_write_then_read_round_trips_through_the_server(self):
        """The first MCP-driven file mutation: bytes cross a real pipe to a real child."""
        with mcp_workspace_client(self.tmp) as client:
            client.write_file("pkg/mod.py", "value = 1\n")
            self.assertEqual(client.read_file("pkg/mod.py"), "value = 1\n")

        # The server created the parent directory and the bytes are on disk.
        self.assertEqual(self._read(os.path.join("pkg", "mod.py")), "value = 1\n")

    def test_the_child_is_killed_when_the_session_closes(self):
        """Ephemeral: the server lives for the pass and not one moment longer."""
        with mcp_workspace_client(self.tmp) as client:
            process = client._process
            self.assertIsNone(process.poll())

        self.assertIsNotNone(process.poll(), "the MCP child outlived its session")

    def test_the_server_refuses_every_traversal_shape(self):
        """Containment is the resolved-path check, so no shape of string gets through."""
        self._write("inside.py", "ok = True\n")
        with mcp_workspace_client(self.tmp) as client:
            candidates = ["../outside.txt", "/etc/passwd",
                          os.path.join(self.tmp, "..", "outside.txt")]
            if IS_WINDOWS:
                candidates.append(WINDOWS_TRAVERSAL_SHAPES[0])
            for candidate in candidates:
                with self.assertRaises(MCPError, msg=candidate) as caught:
                    client.read_file(candidate)
                self.assertIn("Path traversal denied", str(caught.exception))

            # A path that stays inside is still readable -- the guard is containment, not a
            # blanket refusal.
            self.assertEqual(client.read_file("inside.py"), "ok = True\n")

    def test_a_traversal_write_is_refused_and_writes_nothing(self):
        with mcp_workspace_client(self.tmp) as client:
            with self.assertRaises(MCPError):
                client.write_file("../escaped.py", "boom = True\n")

        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.tmp), "escaped.py")))

    def test_a_missing_file_is_reported_as_a_tool_error(self):
        with mcp_workspace_client(self.tmp) as client:
            with self.assertRaises(MCPError):
                client.read_file("nope.py")


class MCPExecutorTests(MCPServerTestCase):
    """The executor is MCP-routed: it holds no file handle of its own."""

    def _planned_artifact(self, *, file_path="mod.py", symbol="alpha", content=REPLACEMENT):
        store = get_store()
        store.save_dag(PlanDAG(
            plan_id="PLAN",
            title="Gate",
            order=["task-1"],
            nodes={
                "task-1": TaskNode(
                    id="task-1", plan_id="PLAN", title="Change alpha",
                    section="1. S", section_id="sec-1", files=[file_path], order_index=0,
                ),
            },
        ))
        execution_gate.plan_artifact(
            plan_id="PLAN",
            task_id="task-1",
            summary="Change alpha",
            ast_targets=[execution_gate.ASTTarget(
                file_path=file_path, symbol_name=symbol, byte_range=[0, 0],
                operation="replace", content=content,
            )],
            estimated_impact="one function",
        )
        execution_gate.approve_artifact("task-1", "PLAN")

    def test_the_executor_applies_an_artifact_through_the_mcp_server(self):
        from orchestration.workflow import executor

        self._write("mod.py", SOURCE)
        self._planned_artifact()

        result = executor.execute_approved("PLAN", "task-1", workspace_dir=self.tmp)

        self.assertTrue(result.success, result.error)
        # Exactly the node's bytes changed, and the change crossed the MCP boundary.
        self.assertIn("return 2", self._read("mod.py"))
        self.assertNotIn("return 1", self._read("mod.py"))

    def test_the_executor_refuses_a_target_outside_the_workspace(self):
        """The server's containment is what the executor relies on; a traversal cannot land."""
        from orchestration.workflow import executor

        self._write("mod.py", SOURCE)
        # A plan that names a file outside the workspace -- the shape a hostile artifact takes.
        self._planned_artifact(file_path="../escaped.py", symbol="", content="boom = True\n")

        result = executor.execute_approved("PLAN", "task-1", workspace_dir=self.tmp)

        self.assertFalse(result.success)
        self.assertIn("Path traversal denied", result.applied[0].error)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.tmp), "escaped.py")))

    def test_the_executor_opens_and_closes_its_own_session(self):
        """``apply_artifact`` without a session still routes through a server, then tears it down."""
        from orchestration.workflow import executor

        self._write("mod.py", SOURCE)
        result = executor.apply_artifact(
            {
                "plan_id": "PLAN",
                "task_id": "task-1",
                "ast_targets": [{
                    "file_path": "mod.py", "symbol_name": "", "byte_range": [0, 0],
                    "operation": "insert", "content": "written = True\n",
                }],
            },
            workspace_dir=self.tmp,
        )

        self.assertTrue(result.success, result.error)
        self.assertEqual(self._read("mod.py"), "written = True\n")


class MCPStallTests(MCPServerTestCase):
    """A stalled child is killed and reaped, not left holding the pipe."""

    def _stalling_server(self):
        path = os.path.join(self.tmp, "stalling_server.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("import sys, time\nsys.stdin.readline()\ntime.sleep(60)\n")
        return path

    def test_a_stalled_read_kills_and_reaps_the_child(self):
        import sys

        client = MCPClient([sys.executable, self._stalling_server()], cwd=self.tmp, timeout=1.0)
        client.start()
        process = client._process

        with self.assertRaises(MCPError) as caught:
            client.initialize()

        self.assertIn("was killed", str(caught.exception))
        # Killed AND reaped: no zombie, and nothing left holding the pipe.
        self.assertIsNotNone(process.poll())
        self.assertIsNone(client._process)

    def test_a_call_after_the_stall_fails_fast_rather_than_hanging(self):
        import sys

        client = MCPClient([sys.executable, self._stalling_server()], cwd=self.tmp, timeout=1.0)
        client.start()
        with self.assertRaises(MCPError):
            client.initialize()

        # The session is closed, so a follow-up is an immediate error, not another wait.
        with self.assertRaises(MCPError):
            client.read_file("anything.py")


class MCPExecServerTests(MCPServerTestCase):
    """The exec server: cwd forced to the workspace, and no argument may leave it.

    Each command is run in a container whose only bind mount is the workspace root and whose
    working directory is that mount, so "cwd is the root" is enforced by the container rather
    than by the host. The runtime is the in-repo double (see ``FAKE_DOCKER_ENV``), so these
    tests still drive the real argv, child process and output plumbing.
    """

    def _session(self):
        return mcp_workspace_client(self.tmp, command=default_exec_command(self.tmp))

    def _script(self, name, body):
        self._write(name, body)
        return name

    def test_it_advertises_the_full_and_restricted_shells(self):
        with self._session() as client:
            names = sorted(tool["name"] for tool in client.list_tools())
        self.assertEqual(names, ["execute_command", "execute_restricted_command"])

    def test_the_command_runs_under_the_container_contract(self):
        """The flags the payload is launched under are the isolation, and they cross the pipe."""
        log = os.path.join(self.tmp, "docker.log")
        with mock.patch.dict(os.environ, {**FAKE_DOCKER_ENV, "FAKE_DOCKER_LOG": log}):
            with self._session() as client:
                client.call_tool("execute_command", {"command": "echo hello"})

        # The runtime is asked about the image first, so the log holds more than the run.
        with open(log, "r", encoding="utf-8") as handle:
            calls = [json.loads(line) for line in handle if line.strip()]
        runs = [call for call in calls if call and call[0] == "run"]
        self.assertEqual(len(runs), 1, f"expected one run, got {calls!r}")
        argv = runs[0]
        self.assertIn("--network=none", argv)
        self.assertIn("--user", argv)
        self.assertEqual(
            argv[argv.index("--mount") + 1],
            f"type=bind,source={Path(self.tmp).resolve()},target=/workspace",
        )
        self.assertEqual(argv[argv.index("--workdir") + 1], "/workspace")

    def test_a_command_runs_with_cwd_forced_to_the_root(self):
        script = self._script("where.py", "import os\nprint(os.getcwd())\n")

        with self._session() as client:
            output = client.call_tool("execute_command", {"command": f"python {script}"})

        self.assertIn("[Exit Code: 0]", output)
        self.assertIn(str(Path(self.tmp).resolve()), output)

    def test_the_output_shape_matches_the_legacy_shell(self):
        """``[Exit Code: N]`` first line, so a caller's success check does not drift."""
        with self._session() as client:
            output = client.call_tool("execute_command", {"command": "echo hello"})

        self.assertTrue(output.startswith("[Exit Code: "), output)
        self.assertIn("hello", output)

    def test_a_nonzero_exit_is_reported_in_the_header(self):
        script = self._script("boom.py", "import sys\nsys.exit(3)\n")

        with self._session() as client:
            output = client.call_tool("execute_command", {"command": f"python {script}"})

        self.assertTrue(output.startswith("[Exit Code: 3]"), output)

    def test_every_escaping_argument_shape_is_refused(self):
        with self._session() as client:
            candidates = ["cat ../../secret.txt", "cat /etc/passwd"]
            if IS_WINDOWS:
                # A backslash and a drive letter only escape on Windows; on POSIX both are
                # ordinary relative names that cannot leave the root.
                candidates += ["cat ..\\..\\secret.txt", "cat C:\\Windows\\win.ini"]
            for candidate in candidates:
                with self.assertRaises(MCPError, msg=candidate) as caught:
                    client.call_tool("execute_command", {"command": candidate})
                self.assertIn("Path traversal denied", str(caught.exception))

    def test_a_command_inside_the_workspace_is_allowed(self):
        script = self._script("inside.py", "print('inside')\n")

        with self._session() as client:
            output = client.call_tool("execute_command", {"command": f"python {script}"})

        self.assertIn("inside", output)


class MCPToolBindingTests(MCPServerTestCase):
    """The server's schemas become the agent's tools; the call is a dumb route back.

    No static catalog is involved: the tools are read from ``tools/list`` at bind time, so
    adding a tool to a server adds it to the agent with no Python change.
    """

    def test_a_json_schema_becomes_a_model_with_the_right_types_and_required_set(self):
        from tools.mcp_tools import json_schema_to_pydantic

        model = json_schema_to_pydantic("write_file", {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "where"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        })

        instance = model(path="a.py", content="x = 1\n")
        self.assertEqual(instance.path, "a.py")
        self.assertEqual(instance.content, "x = 1\n")
        with self.assertRaises(Exception):
            model(content="missing path")

    def test_an_optional_field_is_not_required(self):
        from tools.mcp_tools import json_schema_to_pydantic

        model = json_schema_to_pydantic("execute_command", {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
            },
            "required": ["command"],
        })

        instance = model(command="pytest")
        self.assertIsNone(instance.timeout_seconds)

    def test_binding_a_filesystem_server_yields_its_two_tools(self):
        from tools.mcp_tools import bind_mcp_tools

        with mcp_workspace_client(self.tmp) as client:
            tools = bind_mcp_tools(client)

        self.assertEqual(sorted(tool.name for tool in tools), ["create_file", "read_file", "write_file"])
        self.assertTrue(all(tool.description for tool in tools))

    def test_a_bound_tool_routes_the_call_to_the_server(self):
        """The model's arguments go to ``tools/call`` verbatim; the result comes back."""
        from tools.mcp_tools import bind_mcp_tools

        with mcp_workspace_client(self.tmp) as client:
            tools = {tool.name: tool for tool in bind_mcp_tools(client)}
            tools["write_file"].invoke({"path": "routed.py", "content": "routed = True\n"})
            result = tools["read_file"].invoke({"path": "routed.py"})

        self.assertEqual(result, "routed = True\n")
        self.assertEqual(self._read("routed.py"), "routed = True\n")

    def test_binding_two_servers_with_a_prefix_keeps_them_distinct(self):
        from tools.mcp_tools import bind_mcp_tools

        with mcp_workspace_client(self.tmp) as fs_client, \
                mcp_workspace_client(self.tmp, command=default_exec_command(self.tmp)) as exec_client:
            fs_tools = bind_mcp_tools(fs_client, prefix="fs_")
            exec_tools = bind_mcp_tools(exec_client, prefix="exec_")

        self.assertEqual(sorted(t.name for t in fs_tools), ["fs_create_file", "fs_read_file", "fs_write_file"])
        self.assertEqual(
            sorted(t.name for t in exec_tools),
            ["exec_execute_command", "exec_execute_restricted_command"],
        )

    def test_the_provider_schema_is_derived_from_the_mcp_schema(self):
        """MCP JSON Schema -> pydantic -> the provider's function schema, end to end."""
        from tools.mcp_tools import bind_mcp_tools, openai_tool_schemas

        with mcp_workspace_client(self.tmp) as client:
            schemas = openai_tool_schemas(bind_mcp_tools(client))

        by_name = {schema["function"]["name"]: schema for schema in schemas}
        self.assertIn("write_file", by_name)
        parameters = by_name["write_file"]["function"]["parameters"]
        self.assertEqual(sorted(parameters["properties"]), ["content", "path"])
        self.assertEqual(sorted(parameters["required"]), ["content", "path"])
        self.assertEqual(parameters["properties"]["path"]["type"], "string")

    def test_an_exec_server_binds_with_its_optional_timeout(self):
        from tools.mcp_tools import bind_mcp_tools, openai_tool_schemas

        with mcp_workspace_client(self.tmp, command=default_exec_command(self.tmp)) as client:
            schemas = openai_tool_schemas(bind_mcp_tools(client))

        parameters = schemas[0]["function"]["parameters"]
        self.assertEqual(parameters["required"], ["command"])
        self.assertIn("timeout_seconds", parameters["properties"])


class MCPSessionLifecycleTests(MCPServerTestCase):
    """The run-scoped session: one spawn per run, unconditional teardown."""

    def test_a_session_starts_both_servers_and_binds_their_tools(self):
        from orchestration.mcp_session import MCPSessionContext

        with MCPSessionContext(self.tmp) as session:
            self.assertTrue(session.is_live("fs"))
            self.assertTrue(session.is_live("exec"))
            self.assertEqual(session.failures, {})
            # Both roles bind the filesystem and the exec server's tools, read live.
            self.assertEqual(
                sorted(session.tool_names("architect")),
                [
                    "append_to_file", "create_file", "edit_ast_node", "execute_command",
                    "execute_restricted_command", "list_symbols", "read_file",
                ],
            )
            self.assertEqual(sorted(session.tool_names("coder")), sorted(session.tool_names("architect")))

    def test_the_run_io_helpers_go_through_the_servers(self):
        from orchestration.mcp_session import MCPSessionContext

        with MCPSessionContext(self.tmp) as session:
            session.write_file("scoped.py", "scoped = True\n")
            self.assertEqual(session.read_file("scoped.py"), "scoped = True\n")
            output = session.execute("echo scoped")

        self.assertIn("scoped", output)
        self.assertEqual(self._read("scoped.py"), "scoped = True\n")

    def test_every_child_is_reaped_on_a_clean_exit(self):
        from orchestration.mcp_session import MCPSessionContext

        with MCPSessionContext(self.tmp) as session:
            processes = [session.client(name)._process for name in ("fs", "exec")]

        for process in processes:
            self.assertIsNotNone(process.poll(), "an MCP child outlived the run")

    def test_every_child_is_reaped_when_the_block_raises(self):
        """A branch that raises must not leak a process: teardown is unconditional."""
        from orchestration.mcp_session import MCPSessionContext

        session = MCPSessionContext(self.tmp)
        processes = []
        with self.assertRaises(RuntimeError):
            with session:
                processes = [session.client(name)._process for name in ("fs", "exec")]
                raise RuntimeError("a branch blew up")

        for process in processes:
            self.assertIsNotNone(process.poll(), "a raise leaked an MCP child")

    def test_a_server_that_cannot_start_is_reported_not_raised(self):
        """One dead server must not take the run down; it is recorded and the rest proceed."""
        from orchestration.mcp_session import MCPSessionContext

        with MCPSessionContext(self.tmp, exec_command=["definitely-not-a-real-command-xyz"]) as session:
            self.assertFalse(session.is_live("exec"))
            self.assertIn("exec", session.failures)
            self.assertTrue(session.is_live("fs"))
            # Reaching the dead server is an explicit error, not a silent empty answer.
            with self.assertRaises(RuntimeError):
                session.execute("echo hi")


class MCPSessionRunnerTests(MCPServerTestCase):
    """The runner owns the session's lifetime: one per run, closed on every path."""

    def _run(self, action_type, message, params=None, **patches):
        import registry as registry_module
        from orchestration.mcp_session import MCPSessionContext

        registry_module.registry.create_new_plan_file("PLAN.md", "Demo roadmap")
        opened = {"count": 0}
        closed = {"count": 0}
        real_enter = MCPSessionContext.__enter__
        real_exit = MCPSessionContext.__exit__

        def enter(self):
            opened["count"] += 1
            return real_enter(self)

        def exit(self, *exc):
            closed["count"] += 1
            return real_exit(self, *exc)

        with mock.patch.object(MCPSessionContext, "__enter__", enter), \
                mock.patch.object(MCPSessionContext, "__exit__", exit):
            registry_module.registry.run_agent_workflow(
                message, [].append, action_type=action_type, action_params=params or {}
            )
        return opened["count"], closed["count"]

    def test_the_runner_opens_and_closes_one_session_per_run(self):
        opened, closed = self._run("analyze", "[ACTION: ANALYZE_CODEBASE]")
        self.assertEqual((opened, closed), (1, 1))

    def test_the_runner_closes_the_session_when_a_branch_raises(self):
        from orchestration.workflow import actions_impl

        registry_impl = mock.patch.object(
            actions_impl, "custom_action", side_effect=RuntimeError("boom")
        )
        with registry_impl:
            opened, closed = self._run("custom", "add a login endpoint")
        self.assertEqual((opened, closed), (1, 1))


    def test_the_architect_agent_can_bind_its_tools_from_a_live_session(self):
        """The tool manifest comes from the servers' ``tools/list``, not an import snapshot."""
        import agents.architect as architect_module
        from agents.architect import build_architect_agent
        from orchestration.mcp_session import MCPSessionContext

        with MCPSessionContext(self.tmp) as session:
            agent = build_architect_agent(mcp_session=session)
            names = sorted(tool.name for tool in agent.tools_list)

        self.assertEqual(names, [
            "append_to_file", "create_file", "edit_ast_node", "execute_command",
            "execute_restricted_command", "list_symbols", "read_file",
        ])
        # Nothing was cached: there is no module-level agent for it to be cached in.
        self.assertFalse(
            hasattr(architect_module, "architect_agent"),
            "the architect must not exist as module state",
        )

    def test_the_build_is_a_factory_with_no_module_state(self):
        """Every call returns a fresh instance; the prompt editor's contract is intact.

        ``orchestration.prompt_editor`` hot-reloads by calling this after rewriting the prompt
        constants as text, so the call has to keep working -- it simply builds rather than
        repointing a global, which is what makes the agent ephemeral.
        """
        import agents.architect as architect_module
        from agents.architect import build_architect_agent

        first = build_architect_agent()
        second = build_architect_agent()

        self.assertIsNot(first, second)
        self.assertFalse(hasattr(architect_module, "architect_agent"))
        # The catalog path no longer carries a shell tool: the Architect's restricted shell is
        # the exec server's tool now, bound per run from the session.
        self.assertNotIn("execute_restricted_command", [t.name for t in first.tools_list])


class SurgicalToolTests(MCPServerTestCase):
    """The surgical AST tools, over a live session.

    AST parsing is the orchestrator's; the disk access is the server's. These prove the two
    halves actually meet: every read and write below crosses the MCP boundary, and the
    structural work happens in Python between them.
    """

    SOURCE = (
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
    )

    def _tools(self, session):
        return {tool.name: tool for tool in session.get_bound_tools("coder")}

    def test_list_symbols_reports_each_nodes_exact_span(self):
        from orchestration.mcp_session import MCPSessionContext

        self._write("mod.py", self.SOURCE)
        with MCPSessionContext(self.tmp) as session:
            listing = self._tools(session)["list_symbols"].invoke({"filename": "mod.py"})

        self.assertIn("function alpha", listing)
        self.assertIn("function beta", listing)
        self.assertIn("bytes 0-25", listing)

    def test_edit_ast_node_replaces_exactly_one_span(self):
        """The regression this restores: an agent changes a node, not a file."""
        from orchestration.mcp_session import MCPSessionContext

        self._write("mod.py", self.SOURCE)
        with MCPSessionContext(self.tmp) as session:
            result = self._tools(session)["edit_ast_node"].invoke({
                "filename": "mod.py",
                "symbol": "alpha",
                "replacement": "def alpha():\n    return 99\n",
            })

        self.assertIn("Replaced alpha", result)
        written = self._read("mod.py")
        self.assertIn("return 99", written)
        # beta is untouched: that is the whole point of a span edit.
        self.assertIn("def beta():\n    return 2\n", written)

    def test_edit_ast_node_refuses_an_unknown_symbol(self):
        from orchestration.mcp_session import MCPSessionContext

        self._write("mod.py", self.SOURCE)
        with MCPSessionContext(self.tmp) as session:
            result = self._tools(session)["edit_ast_node"].invoke({
                "filename": "mod.py", "symbol": "ghost", "replacement": "x = 1\n",
            })

        self.assertIn("Edit refused", result)
        self.assertEqual(self._read("mod.py"), self.SOURCE)

    def test_edit_ast_node_refuses_a_splice_that_would_not_parse(self):
        from orchestration.mcp_session import MCPSessionContext

        self._write("mod.py", self.SOURCE)
        with MCPSessionContext(self.tmp) as session:
            result = self._tools(session)["edit_ast_node"].invoke({
                "filename": "mod.py",
                "symbol": "alpha",
                "replacement": "def alpha(:\n    return 1\n",
            })

        self.assertIn("Edit refused", result)
        self.assertEqual(self._read("mod.py"), self.SOURCE)

    def test_append_to_file_extends_without_replacing(self):
        from orchestration.mcp_session import MCPSessionContext

        self._write("mod.py", self.SOURCE)
        with MCPSessionContext(self.tmp) as session:
            self._tools(session)["append_to_file"].invoke({
                "filename": "mod.py", "content": "def gamma():\n    return 3\n",
            })

        written = self._read("mod.py")
        self.assertTrue(written.startswith(self.SOURCE))
        self.assertIn("def gamma()", written)

    def test_the_models_writer_refuses_an_existing_file(self):
        """The chokehold, as two tools with different callers rather than a flag."""
        from orchestration.mcp_session import MCPSessionContext

        self._write("mod.py", self.SOURCE)
        with MCPSessionContext(self.tmp) as session:
            tools = self._tools(session)
            # The model is not offered the engine's overwrite writer at all.
            self.assertNotIn("write_file", tools)
            with self.assertRaises(Exception) as caught:
                tools["create_file"].invoke({"path": "mod.py", "content": "dumped = True\n"})

        self.assertIn("edit_ast_node", str(caught.exception))
        self.assertEqual(self._read("mod.py"), self.SOURCE)

    def test_the_models_writer_creates_a_new_file(self):
        from orchestration.mcp_session import MCPSessionContext

        with MCPSessionContext(self.tmp) as session:
            self._tools(session)["create_file"].invoke({
                "path": "fresh.py", "content": "fresh = True\n",
            })

        self.assertEqual(self._read("fresh.py"), "fresh = True\n")

    def test_a_surgical_tool_cannot_reach_outside_the_workspace(self):
        """The AST tools have no disk access of their own: the server's cage is theirs."""
        from orchestration.mcp_session import MCPSessionContext

        with MCPSessionContext(self.tmp) as session:
            with self.assertRaises(Exception) as caught:
                self._tools(session)["list_symbols"].invoke({"filename": "../../escape.py"})

        self.assertIn("Path traversal denied", str(caught.exception))


class AtomicFilesystemWriteTests(MCPServerTestCase):
    """Phase 22: the engine's writer replaces its target, it never truncates one in place.

    The success path runs through the live session, like every other write in this file. The
    failure path is exercised on the server object directly, because the only honest way to test
    an interrupted write is to inject the fault into the process doing the writing.
    """

    def _scratch(self, root):
        """Scratch files left under ``root``: a temporary that never became its target."""
        found = []
        for current, _dirs, files in os.walk(root):
            for name in files:
                if name.startswith(".tmp."):
                    found.append(os.path.relpath(os.path.join(current, name), root))
        return sorted(found)

    def test_a_write_leaves_no_scratch_file_beside_it(self):
        with mcp_workspace_client(self.tmp) as client:
            client.write_file("pkg/mod.py", "value = 1\n")

        self.assertEqual(self._read(os.path.join("pkg", "mod.py")), "value = 1\n")
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, "pkg"))), ["mod.py"])

    def test_a_refused_replace_leaves_the_original_whole_and_no_litter(self):
        from tools.mcp_fs_server import FilesystemServer

        self._write("mod.py", "original = True\n")
        server = FilesystemServer(self.tmp)

        with mock.patch("tools.atomic_io.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                server.write_file("mod.py", "replacement = True\n")

        self.assertEqual(self._read("mod.py"), "original = True\n")
        self.assertEqual(self._scratch(self.tmp), [])

    def test_the_temporary_shares_the_directory_of_its_target(self):
        """Same directory, so ``os.replace`` is a rename inside one filesystem: a temporary in a
        system temp directory would be ``EXDEV`` the moment the workspace is a bind mount."""
        from tools.mcp_fs_server import FilesystemServer

        server = FilesystemServer(self.tmp)
        seen = []
        real_replace = os.replace

        def _record(source, target):
            seen.append((os.path.dirname(source), os.path.dirname(os.path.abspath(target))))
            return real_replace(source, target)

        with mock.patch("tools.atomic_io.os.replace", _record):
            server.write_file("deep/pkg/mod.py", "value = 1\n")

        self.assertTrue(seen)
        for source_dir, target_dir in seen:
            self.assertEqual(source_dir, target_dir)

    def test_create_file_is_still_create_never_replace(self):
        """The chokehold survives the atomic path: a refusal writes nothing at all."""
        from tools.mcp_fs_server import FilesystemServer

        self._write("mod.py", "original = True\n")
        with self.assertRaises(FileExistsError):
            FilesystemServer(self.tmp).create_file("mod.py", "replacement = True\n")

        self.assertEqual(self._read("mod.py"), "original = True\n")
        self.assertEqual(self._scratch(self.tmp), [])


class ToolLoopTests(MCPServerTestCase):
    """The agent execution loop: a model's tool call really reaches a server.

    The MCP transport is NOT mocked here. The session spawns real children, the bound tool
    routes over JSON-RPC, and the file on disk changes. Only the model's decision is
    scripted -- which is the only thing that can be, offline.
    """

    class _Completion:
        def __init__(self, text="", tool_calls=None):
            self.text = text
            self.tool_calls = tool_calls or []

    def _scripted(self, *turns):
        """A completer that answers with ``turns`` in order, recording what it was sent."""
        remaining = list(turns)
        seen = []

        def completer(*, system, messages, tools):
            seen.append({"messages": messages, "tools": tools})
            return remaining.pop(0) if remaining else self._Completion(text="done")

        completer.seen = seen
        return completer

    def test_a_tool_call_runs_through_the_live_session_and_returns_a_result(self):
        from orchestration.mcp_session import MCPSessionContext
        from orchestration.workflow.agent_loop import run_tool_loop

        self._write("mod.py", "def alpha():\n    return 1\n")
        completer = self._scripted(
            self._Completion(tool_calls=[{"name": "read_file", "arguments": {"path": "mod.py"}}]),
            self._Completion(text="read it"),
        )

        with MCPSessionContext(self.tmp) as session:
            text, transcript = run_tool_loop(
                session=session, role="coder", system_prompt="sys",
                user_message="inspect mod.py", completer=completer,
            )

        self.assertEqual(text, "read it")
        self.assertEqual(len(transcript), 1)
        self.assertEqual(transcript[0]["tool"], "read_file")
        # The result is the real file text, fetched over the pipe by the server.
        self.assertIn("def alpha()", transcript[0]["result"])

    def test_the_model_is_sent_the_bound_tool_schemas(self):
        from orchestration.mcp_session import MCPSessionContext
        from orchestration.workflow.agent_loop import run_tool_loop

        completer = self._scripted(self._Completion(text="nothing to do"))
        with MCPSessionContext(self.tmp) as session:
            run_tool_loop(
                session=session, role="coder", system_prompt="sys",
                user_message="hello", completer=completer,
            )

        names = sorted(t["function"]["name"] for t in completer.seen[0]["tools"])
        self.assertIn("edit_ast_node", names)
        self.assertIn("read_file", names)
        # The engine's overwrite writer is not offered to a model.
        self.assertNotIn("write_file", names)

    def test_a_surgical_edit_driven_by_the_model_changes_the_file(self):
        """The end-to-end routing proof: model -> loop -> bound tool -> server -> disk."""
        from orchestration.mcp_session import MCPSessionContext
        from orchestration.workflow.agent_loop import run_tool_loop

        self._write("mod.py", "def alpha():\n    return 1\n")
        completer = self._scripted(
            self._Completion(tool_calls=[{
                "name": "edit_ast_node",
                "arguments": {
                    "filename": "mod.py", "symbol": "alpha",
                    "replacement": "def alpha():\n    return 7\n",
                },
            }]),
            self._Completion(text="patched"),
        )

        with MCPSessionContext(self.tmp) as session:
            text, transcript = run_tool_loop(
                session=session, role="coder", system_prompt="sys",
                user_message="fix alpha", completer=completer,
            )

        self.assertEqual(text, "patched")
        self.assertIn("Replaced alpha", transcript[0]["result"])
        self.assertIn("return 7", self._read("mod.py"))

    def test_a_refused_tool_is_reported_to_the_model_not_raised(self):
        from orchestration.mcp_session import MCPSessionContext
        from orchestration.workflow.agent_loop import run_tool_loop

        self._write("mod.py", "def alpha():\n    return 1\n")
        completer = self._scripted(
            self._Completion(tool_calls=[{
                "name": "create_file",
                "arguments": {"path": "mod.py", "content": "dumped = True\n"},
            }]),
            self._Completion(text="understood"),
        )

        with MCPSessionContext(self.tmp) as session:
            text, transcript = run_tool_loop(
                session=session, role="coder", system_prompt="sys",
                user_message="rewrite mod.py", completer=completer,
            )

        self.assertEqual(text, "understood")
        self.assertIn("Error:", transcript[0]["result"])
        self.assertIn("edit_ast_node", transcript[0]["result"])
        self.assertEqual(self._read("mod.py"), "def alpha():\n    return 1\n")

    def test_the_turn_budget_is_a_hard_stop(self):
        from orchestration.mcp_session import MCPSessionContext
        from orchestration.workflow.agent_loop import run_tool_loop

        self._write("mod.py", "x = 1\n")
        forever = [self._Completion(tool_calls=[{"name": "read_file", "arguments": {"path": "mod.py"}}])] * 10
        completer = self._scripted(*forever)

        with MCPSessionContext(self.tmp) as session:
            text, transcript = run_tool_loop(
                session=session, role="coder", system_prompt="sys",
                user_message="loop", completer=completer, max_turns=3,
            )

        self.assertEqual(text, "")
        self.assertEqual(len(transcript), 3)


class PlannerAgentLoopTests(MCPServerTestCase):
    """The planner runs as an agent when a session is supplied.

    The live path this proves: ``plan_and_yield`` threads ``ctx.mcp_session`` into
    ``plan_task``, the model is sent the tools bound from the MCP servers, and a tool call it
    makes really reaches a server. Only the model's decision is scripted.
    """

    class _Completion:
        def __init__(self, text="", tool_calls=None):
            self.text = text
            self.tool_calls = tool_calls or []

    def test_the_planner_inspects_the_workspace_through_mcp_tools(self):
        from orchestration.mcp_session import MCPSessionContext
        from orchestration.workflow import planner

        self._write("mod.py", "def alpha():\n    return 1\n")
        payload = (
            '{"plan_id": "PLAN", "task_id": "task-1", "summary": "s",'
            ' "estimated_impact": "i", "complexity_score": 2,'
            ' "required_capabilities": [], "ast_targets": []}'
        )
        seen = []
        turns = [
            self._Completion(tool_calls=[{
                "name": "list_symbols", "arguments": {"filename": "mod.py"},
            }]),
            self._Completion(text=payload),
        ]

        def completer(*, system, messages, tools):
            seen.append({"tools": tools, "messages": messages})
            return turns.pop(0) if turns else self._Completion(text=payload)

        with mock.patch("orchestration.system2.is_enabled", return_value=True), \
                MCPSessionContext(self.tmp) as session:
            artifact = planner.plan_task(
                {"id": "task-1", "title": "Change alpha", "files": ["mod.py"]},
                plan_id="PLAN", workspace_dir=self.tmp, completer=completer, session=session,
            )

        self.assertEqual(artifact.task_id, "task-1")
        # The model was offered the bound tools, not a static catalog.
        names = sorted(tool["function"]["name"] for tool in seen[0]["tools"])
        self.assertIn("list_symbols", names)
        self.assertNotIn("write_file", names)
        # Its tool call reached a server, and the result came back into the conversation.
        tool_turns = [m for m in seen[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(tool_turns), 1)
        self.assertIn("function alpha", tool_turns[0]["content"])

    def test_the_planner_is_offered_only_the_declared_capabilities(self):
        """Phase 7.4: the session's scope is the model's manifest -- nothing more is offered."""
        from orchestration.mcp_session import MCPSessionContext
        from orchestration.workflow import planner

        self._write("mod.py", "def alpha():\n    return 1\n")
        payload = (
            '{"plan_id": "PLAN", "task_id": "task-1", "summary": "s",'
            ' "estimated_impact": "i", "complexity_score": 2,'
            ' "required_capabilities": [], "ast_targets": []}'
        )
        seen = []

        def completer(*, system, messages, tools):
            seen.append(tools)
            return self._Completion(text=payload)

        with mock.patch("orchestration.system2.is_enabled", return_value=True), \
                MCPSessionContext(self.tmp, capabilities=[]) as session:
            planner.plan_task(
                {"id": "task-1", "title": "Change alpha", "files": ["mod.py"]},
                plan_id="PLAN", workspace_dir=self.tmp, completer=completer, session=session,
            )

        self.assertEqual(seen[0], [], "a node that declared nothing was offered a tool")

    def test_a_declared_capability_reaches_the_model_as_a_tool(self):
        from orchestration.mcp_session import MCPSessionContext
        from orchestration.workflow import planner

        self._write("mod.py", "def alpha():\n    return 1\n")
        payload = (
            '{"plan_id": "PLAN", "task_id": "task-1", "summary": "s",'
            ' "estimated_impact": "i", "complexity_score": 2,'
            ' "required_capabilities": ["fs"], "ast_targets": []}'
        )
        seen = []

        def completer(*, system, messages, tools):
            seen.append(tools)
            return self._Completion(text=payload)

        with mock.patch("orchestration.system2.is_enabled", return_value=True), \
                MCPSessionContext(self.tmp, capabilities=["fs"]) as session:
            planner.plan_task(
                {"id": "task-1", "title": "Change alpha", "files": ["mod.py"]},
                plan_id="PLAN", workspace_dir=self.tmp, completer=completer, session=session,
            )

        names = sorted(tool["function"]["name"] for tool in seen[0])
        self.assertEqual(names, ["create_file", "read_file"])

    def test_the_planning_pass_asks_the_live_session_for_the_given_role(self):
        """The manifest is read live for the node's role; the planner assumes nothing."""
        from orchestration.workflow import planner

        payload = (
            '{"plan_id": "PLAN", "task_id": "task-1", "summary": "s",'
            ' "estimated_impact": "i", "complexity_score": 2,'
            ' "required_capabilities": [], "ast_targets": []}'
        )
        asked = []

        class _Session:
            def get_bound_tools(self, role):
                asked.append(role)
                return []

        def completer(*, system, messages, tools):
            return self._Completion(text=payload)

        with mock.patch("orchestration.system2.is_enabled", return_value=True):
            planner.plan_task(
                {"id": "task-1", "title": "Change alpha", "files": ["mod.py"]},
                plan_id="PLAN", workspace_dir=self.tmp, completer=completer,
                session=_Session(), role="coder-deep",
            )

        self.assertEqual(asked, ["coder-deep"], "the node's role did not reach the session")

    def test_the_planning_pass_defaults_to_the_engines_own_role(self):
        """A direct caller with no role gets the engine's planning role, not a random one."""
        from orchestration.workflow import planner

        payload = (
            '{"plan_id": "PLAN", "task_id": "task-1", "summary": "s",'
            ' "estimated_impact": "i", "complexity_score": 2,'
            ' "required_capabilities": [], "ast_targets": []}'
        )
        asked = []

        class _Session:
            def get_bound_tools(self, role):
                asked.append(role)
                return []

        def completer(*, system, messages, tools):
            return self._Completion(text=payload)

        with mock.patch("orchestration.system2.is_enabled", return_value=True):
            planner.plan_task(
                {"id": "task-1", "title": "Change alpha", "files": ["mod.py"]},
                plan_id="PLAN", workspace_dir=self.tmp, completer=completer,
                session=_Session(),
            )

        self.assertEqual(asked, [planner.DEFAULT_PLANNER_ROLE])

    def test_without_a_session_the_planner_is_single_shot(self):
        """The offline path is untouched: one call, no tools, no loop."""
        from orchestration.workflow import planner

        calls = []
        payload = (
            '{"plan_id": "PLAN", "task_id": "task-1", "summary": "s",'
            ' "estimated_impact": "i", "complexity_score": 2,'
            ' "required_capabilities": [], "ast_targets": []}'
        )

        def completer(*args, **kwargs):
            calls.append(kwargs)
            return self._Completion(text=payload)

        with mock.patch("orchestration.system2.is_enabled", return_value=True):
            planner.plan_task(
                {"id": "task-1", "title": "x", "files": ["mod.py"]},
                plan_id="PLAN", workspace_dir=self.tmp, completer=completer,
            )

        self.assertEqual(len(calls), 1)
        # The single-shot contract, not the loop's.
        self.assertIn("user", calls[0])


class RoutedModelTests(MCPServerTestCase):
    """The worker has no authority to pick a model: the route it is handed is the route it uses.

    This is the plumbing the dispatch loop depends on -- ``_descriptor_for`` routes a node, the
    descriptor carries the route across the process boundary, and the provider call is made with
    that route and no other. The provider seam itself is stubbed; everything up to it is real.
    """

    def test_the_injected_route_reaches_the_provider_call(self):
        from orchestration import system2
        from orchestration.mcp_session import MCPSessionContext
        from orchestration.workflow import planner

        self._write("mod.py", "def alpha():\n    return 1\n")
        payload = (
            '{"plan_id": "PLAN", "task_id": "task-1", "summary": "s",'
            ' "estimated_impact": "i", "complexity_score": 2,'
            ' "required_capabilities": [], "ast_targets": []}'
        )
        seen = {}

        def fake_complete_with_tools(*, model, system, messages, tools,
                                     base_url=None, api_key=None, skills=""):
            seen["model"] = model
            seen["base_url"] = base_url
            seen["api_key"] = api_key
            return system2.ToolCompletion(text=payload)

        with mock.patch("orchestration.system2.is_enabled", return_value=True), \
                mock.patch("orchestration.system2.complete_with_tools", fake_complete_with_tools), \
                MCPSessionContext(self.tmp) as session:
            planner.plan_task(
                {"id": "task-1", "title": "Change alpha", "files": ["mod.py"]},
                plan_id="PLAN", workspace_dir=self.tmp, session=session,
                model="openai:policy/coder-deep-test",
                base_url="https://tier-two.invalid/v1",
                api_key="sk-tier-two",
            )

        assert seen["model"] == "openai:policy/coder-deep-test"
        # The endpoint travels with the route: a tier that names a base_url is called there, with
        # that key, rather than with whatever the environment happens to hold.
        assert seen["base_url"] == "https://tier-two.invalid/v1"
        assert seen["api_key"] == "sk-tier-two"

    def test_a_route_less_worker_never_reaches_a_guessed_model(self):
        """No route means no call, not a silent default: the transport does not route."""
        from orchestration import system2

        calls = []

        def fake_create(**kwargs):
            calls.append(kwargs)
            raise AssertionError("the provider must not be called without a model")

        class _Completions:
            create = staticmethod(fake_create)

        class _Client:
            def __init__(self, **kwargs):
                self.chat = mock.Mock(completions=_Completions())

        result = system2.complete_with_tools(
            model="", system="s", messages=[{"role": "user", "content": "hi"}], tools=[],
            client=_Client(),
        )

        assert calls == []
        assert result.text == ""
        assert result.tool_calls == []


class CapabilityScopedToolsTests(MCPServerTestCase):
    """Phase 7.4: a session scoped to a node's capabilities binds only what was named.

    The servers are real children; only the *scope* is under test. ``capabilities=None`` is the
    engine's session and keeps the full role set; a sequence is a node's declaration.
    """

    def test_an_empty_declaration_binds_no_tools_and_spawns_no_servers(self):
        from orchestration.mcp_session import MCPSessionContext

        with MCPSessionContext(self.tmp, capabilities=[]) as session:
            self.assertEqual(session.get_bound_tools("architect"), [])
            self.assertEqual(session.tool_names("coder"), [])
            # Not merely unbound: nothing was started to bind in the first place.
            self.assertFalse(session.is_live("fs"))
            self.assertFalse(session.is_live("exec"))

    def test_the_fs_capability_binds_only_the_file_tools(self):
        from orchestration.mcp_session import MCPSessionContext

        with MCPSessionContext(self.tmp, capabilities=["fs"]) as session:
            names = set(session.tool_names("coder"))
            self.assertFalse(session.is_live("exec"))

        # ``write_file`` is the engine's overwrite and is never offered to a model, even when the
        # node asked for the filesystem.
        self.assertEqual(names, {"read_file", "create_file"})

    def test_the_exec_capability_binds_only_the_shell_tools(self):
        from orchestration.mcp_session import MCPSessionContext

        with MCPSessionContext(self.tmp, capabilities=["exec"]) as session:
            names = set(session.tool_names("coder"))
            self.assertFalse(session.is_live("fs"))

        self.assertEqual(names, {"execute_command", "execute_restricted_command"})

    def test_the_ast_capability_binds_the_surgical_tools_and_still_spawns_the_fs_server(self):
        from orchestration.mcp_session import MCPSessionContext

        self._write("mod.py", "def alpha():\n    return 1\n")
        with MCPSessionContext(self.tmp, capabilities=["ast"]) as session:
            names = set(session.tool_names("architect"))
            # The surgical tools read and write through the filesystem server, so it must be live
            # even though its own tools are not handed over.
            self.assertTrue(session.is_live("fs"))
            self.assertFalse(session.is_live("exec"))
            listing = next(t for t in session.get_bound_tools("architect") if t.name == "list_symbols")
            self.assertIn("alpha", listing.invoke({"filename": "mod.py"}))

        self.assertEqual(names, {"list_symbols", "edit_ast_node", "append_to_file"})

    def test_the_uri_spelling_the_planner_is_shown_is_understood(self):
        from orchestration.mcp_session import MCPSessionContext

        with MCPSessionContext(
            self.tmp, capabilities=["mcp://local-fs", "mcp://ast-parser"]
        ) as session:
            names = set(session.tool_names("architect"))

        self.assertEqual(
            names,
            {"read_file", "create_file", "list_symbols", "edit_ast_node", "append_to_file"},
        )

    def test_an_unknown_capability_is_refused_rather_than_ignored(self):
        from orchestration.mcp_session import MCPSessionContext, UnknownCapabilityError

        with self.assertRaises(UnknownCapabilityError):
            MCPSessionContext(self.tmp, capabilities=["browser"])

    def test_the_engine_session_still_binds_the_full_role_set(self):
        """``None`` is the run's own session: it keeps its I/O and the role's whole tool set."""
        from orchestration.mcp_session import MCPSessionContext

        with MCPSessionContext(self.tmp) as session:
            names = set(session.tool_names("architect"))

        self.assertIn("read_file", names)
        self.assertIn("execute_command", names)
        self.assertIn("list_symbols", names)
        self.assertNotIn("write_file", names)


class MCPCommandTests(MCPServerTestCase):
    def test_the_default_command_points_at_the_in_repo_server(self):
        command = default_command(self.tmp)

        self.assertTrue(command[1].endswith("mcp_fs_server.py"))
        self.assertIn("--root", command)
        self.assertEqual(command[command.index("--root") + 1], self.tmp)

    def test_a_client_that_cannot_start_reports_a_timeout_rather_than_hanging(self):
        client = MCPClient(["definitely-not-a-real-command-xyz"], cwd=self.tmp, timeout=2.0)
        with self.assertRaises((MCPError, OSError)):
            client.start().initialize()
        client.close()


if __name__ == "__main__":
    unittest.main()
