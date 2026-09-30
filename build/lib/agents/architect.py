from deepagents import create_deep_agent
from agents.coders import CODER_ROLES, coder_deep, coder_standard
from agents.model_routing import architect_model
from agents.roles import AgentRole

ARCHITECT_CORE_PROMPT = """You are the Lead Software Architect and Gatekeeper.

### Gatekeeper Architecture & Smart Routing:
- You intercept all action commands.
- ADMINISTRATIVE BYPASS: If the action is a plan update, codebase analysis, architecture review, or recommendation, you execute it directly using file inspection and read/write tools. DO NOT spawn Coders for administrative or analytical tasks.
- IMPLEMENTATION DELEGATION: If physical application code (`.py`, `.js`, etc.) needs to be written or modified, you first analyze requirements and codebase context, formulate an atomic specification, and delegate implementation to specialized Coder sub-agents.
- You must NEVER write production application code directly.
- File writes are strictly limited to updating `{{ACTIVE_PLAN_FILE}}` for task formulation, roadmap tracking, and audit reconciliation.

### Workflow Directives:
1. Memory is state-anchored via `{{ACTIVE_PLAN_FILE}}`. The machine state lives in a SQLite store beside it; inspect and synchronize state on every run.
2. For Frontend / UI tasks: Tag tasks with `[UI]` in the plan.
3. For Bug Fixes: First analyze the workspace files to discover WHAT the bug is and WHERE it is located before drafting a fix spec and summoning a Coder.
4. When delegating:
   - Route complex algorithms, backend APIs, data models, or security to 'coder-deep'.
   - Route tests, boilerplate, documentation, or simple CRUD endpoints to 'coder-standard'.
5. You have restricted shell access (`execute_restricted_command`): strictly for running pytest, compile checks, or validation commands.
6. Once delegated tasks finish:
   - Audit completed work in `{{ACTIVE_PLAN_FILE}}`.
   - Run verification checks with `execute_restricted_command`.
   - Present a clear summary of work and actionable next steps.
7. Milestone Wrap-Up (Living Behavioral Ledger): when a milestone is finished, record it
   as a durable fact rather than as prose.
   - Append a single `🟢 Behavioral Log:` line directly beneath its `- [x]` checkbox: what
     changed and where, in one line.
   - Compress the finished milestone into ONE core fact bullet under
     `## 🌍 Global State Summary` (e.g. `[API] Forecast endpoint — delivered app/api.py`).
   - The summary is carried into every later prompt, so keep each fact to a single line and
     never restate a fact already there."""

ARCHITECT_CUSTOM_INSTRUCTIONS = """Follow standard production PEP8 style guidelines and keep solution components modular."""

ARCHITECT_SYSTEM_PROMPT = f"{ARCHITECT_CORE_PROMPT}\n\n### Custom Developer Directives:\n{ARCHITECT_CUSTOM_INSTRUCTIONS}"

# The Architect as data, exactly like the coders. It lives here rather than in ``agents/roles.py``
# because its prompt is a published contract with ``orchestration.prompt_editor``, which rewrites
# these constants as text -- importing the prompt into ``roles.py`` would invert the dependency and
# make the module graph circular.
ARCHITECT_ROLE = AgentRole(
    # ``software-architect`` is the agent's *id*, and it is load-bearing: the UI addresses the
    # architect by it (``registry.get_agent``, ``save_system_prompt``), and the DAG's agent field
    # carries it. The role's name is that id, not a new one -- renaming it would break every
    # caller to make a cosmetic point.
    name="software-architect",
    display_name="Lead Software Architect",
    description="Plans, verifies and delegates. Never writes application code directly.",
    system_prompt=ARCHITECT_SYSTEM_PROMPT,
    model=architect_model(),
)

def build_architect_agent(system_prompt=None, mcp_session=None):
    """Builds the architect deep agent.

    ``system_prompt`` and the zero-argument form are a **published contract** with
    ``orchestration.prompt_editor``, which rewrites this file's constants as text and then
    calls this to hot-reload. Neither may change shape.

    ``mcp_session`` binds the agent's tools from the run's live MCP servers instead of the
    static catalog, so the tool manifest is the server's ``tools/list`` response rather than
    an import-time snapshot. An agent built that way is deliberately **not** cached in the
    module global: its tools belong to a session that dies with the run, and a global holding
    them would hand a later run tools whose pipes are already closed.
    """
    global ARCHITECT_SYSTEM_PROMPT
    if system_prompt is not None:
        ARCHITECT_SYSTEM_PROMPT = system_prompt
        # The role descriptor is what the catalog reports, so hot-reload has to reach it as well
        # as the constant -- otherwise an edit would look like it had no effect until a restart,
        # which is the exact failure the in-memory update exists to prevent.
        ARCHITECT_ROLE.system_prompt = system_prompt

    if mcp_session is not None:
        architect_tools = mcp_session.get_bound_tools("architect")
    else:
        # No static catalog. Every tool this agent has is bound per run from the MCP servers
        # (``mcp_session.get_bound_tools``); the file and shell catalogs that used to be
        # snapshotted here are gone, so an agent built without a session has no tools rather
        # than a stale manifest.
        architect_tools = []

    agent = create_deep_agent(
        name="software-architect",
        model=architect_model(),
        system_prompt=ARCHITECT_SYSTEM_PROMPT,
        tools=architect_tools,
        # Built on demand from the role descriptors. There is no module-level list of subagent
        # dicts any more: a subagent is a shape the library asks for, not application state.
        subagents=[role.to_subagent() for role in CODER_ROLES]
    )
    # Introspection references
    agent.agent_name = "software-architect"
    agent.display_name = "Lead Software Architect"
    agent.model_name = architect_model()
    agent.system_prompt_text = ARCHITECT_SYSTEM_PROMPT
    # The roles themselves, not a snapshot of agent dicts -- the catalog reads the descriptors.
    agent.subagents_list = list(CODER_ROLES)
    agent.tools_list = architect_tools
    return agent

# There is no module-level agent instance. An agent is an ephemeral compute instance: it is built
# by :func:`build_architect_agent` for a specific run, with the tools that run's session provides,
# and it does not outlive that run. A global here would be shared mutable state in a process pool
# -- the exact hazard Phase 6 exists to remove.
