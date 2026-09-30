from typing import Any

from agents.model_routing import coder_model
from agents.roles import AgentRole

CODER_DEEP_CORE_PROMPT = """You are a Senior Backend Coder with full system shell access.
When given a task:
1. Always use `read_file` to inspect `{{ACTIVE_PLAN_FILE}}` first for context and assigned requirements.
2. Use `execute_shell_command` when you need to install packages, run linters, or scaffold dependencies.
3. Write new files with `write_file`. To change code that already exists, call `list_symbols`
   to see the file's AST nodes, then `edit_ast_node(filename, symbol, replacement)` to
   replace exactly the node you mean -- never rewrite a whole file to change one function.
4. When finished, record what you did in the active plan as a Living Behavioral Ledger:
   - Write ONE `🟢 Behavioral Log:` line directly beneath the task's `- [x]` checkbox, e.g.
     `- [x] Build the forecast endpoint`
     `  - 🟢 Behavioral Log: added app/api.py with the forecast route and a paired test.`
   - It is one line: what changed and where. It is the evidence the next task is read
     against, so keep it factual and specific -- never a status restatement.
   - Compress a finished milestone into one core fact bullet under `## 🌍 Global State Summary`."""

CODER_DEEP_CUSTOM_INSTRUCTIONS = """Ensure type annotations and comprehensive docstrings are provided for all public methods."""

CODER_DEEP_SYSTEM_PROMPT = f"{CODER_DEEP_CORE_PROMPT}\n\n### Custom Developer Directives:\n{CODER_DEEP_CUSTOM_INSTRUCTIONS}"

CODER_STANDARD_CORE_PROMPT = """You are a Junior Developer with full system shell access.
When given a task:
1. Use `read_file` to read `{{ACTIVE_PLAN_FILE}}` to understand current project state and task assignments.
2. Use `execute_shell_command` to run tests, format code, or check packages.
3. Write new boilerplate, tests and documentation with `write_file`. To change code that
   already exists, call `list_symbols` to see the file's AST nodes, then
   `edit_ast_node(filename, symbol, replacement)` to replace exactly the node you mean --
   never rewrite a whole file to change one function.
4. When finished, record what you did in the active plan as a Living Behavioral Ledger:
   - Write ONE `🟢 Behavioral Log:` line directly beneath the task's `- [x]` checkbox, e.g.
     `- [x] Write unit tests`
     `  - 🟢 Behavioral Log: added tests/test_api.py covering the forecast route.`
   - It is one line: what changed and where. It is the evidence the next task is read
     against, so keep it factual and specific -- never a status restatement.
   - Compress a finished milestone into one core fact bullet under `## 🌍 Global State Summary`."""

CODER_STANDARD_CUSTOM_INSTRUCTIONS = """Follow standard unittest or pytest conventions and generate clear test assertions."""

CODER_STANDARD_SYSTEM_PROMPT = f"{CODER_STANDARD_CORE_PROMPT}\n\n### Custom Developer Directives:\n{CODER_STANDARD_CUSTOM_INSTRUCTIONS}"

# The coder roles. These are *descriptions* of how to build an agent -- a name, a purpose, a
# prompt and a model route -- and they carry no tools, because a tool set is bound per node from
# the MCP session at spawn time. The dictionaries of live agent state that used to sit here are
# gone: an agent exists for one node of the DAG and for no longer than that.
coder_deep = AgentRole(
    name="coder-deep",
    display_name="Senior Backend Coder",
    description="Use this agent for complex architectural logic, core algorithm design, and security implementations.",
    system_prompt=CODER_DEEP_SYSTEM_PROMPT,
    model=coder_model("coder-deep"),
)

coder_standard = AgentRole(
    name="coder-standard",
    display_name="Junior Developer",
    description="Use this agent for writing tests, boilerplate, documentation, and simple CRUD endpoints.",
    system_prompt=CODER_STANDARD_SYSTEM_PROMPT,
    model=coder_model("coder-standard"),
)

CODER_ROLES = (coder_deep, coder_standard)

# Which role builds the agent for a node, by the coder id the router names.
ROLE_BY_NAME = {role.name: role for role in CODER_ROLES}


def build_coder_agent(role: AgentRole, context: Any = None) -> Any:
    """Build a coder agent for one node, with its tools bound from the live session.

    No catalog and no global: the tools come from ``context`` at the moment of construction, so
    the manifest is whatever the MCP servers advertise right now. Without a context the agent is
    built with no tools rather than a stale snapshot.
    """
    from deepagents import create_deep_agent

    tools = list(context.get_bound_tools("coder")) if context is not None else []
    return create_deep_agent(
        name=role.name,
        model=role.model,
        system_prompt=role.system_prompt,
        tools=tools,
    )
