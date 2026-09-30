"""Agent introspection: the explicit role catalog, and the factory that builds agents.

This module used to *discover* agents -- it iterated ``dir(agents.architect)`` looking for a
``CompiledStateGraph`` instance and read the coder roles off whatever it found. That is why the
architect could not be removed from module scope: the registry depended on an object being *there*
when the module was imported, so the global was load-bearing and the catalog was a reflection
routine rather than a definition.

Now the roles are explicit data and the catalog is a description. :func:`build_catalog` reports
what roles exist; :func:`build_agent` is the one place an agent is constructed, from a role and the
run's MCP session. No scanning, no globals, no import-time coupling to an instance.
"""

import os
from typing import Any, Callable, Dict, Optional, Tuple

import agents.architect as arch_mod
import agents.coders as coders_mod
from agents.model_routing import coder_model
from agents.roles import AgentRole

ARCHITECT_ROLE = arch_mod.ARCHITECT_ROLE


def resolve_prompt_variables(prompt: str, active_plan: str) -> str:
    """Replaces dynamic template variables like {{ACTIVE_PLAN_FILE}} with current state."""
    if not prompt:
        return ""
    return prompt.replace("{{ACTIVE_PLAN_FILE}}", active_plan)


def build_agent(role: AgentRole, context: Any = None) -> Any:
    """The deterministic factory: a role and a session in, a compiled agent out.

    ``context`` is the run's :class:`~orchestration.mcp_session.MCPSessionContext`, and it is what
    supplies the tools -- so an agent's manifest is whatever the MCP servers advertise at the
    moment it is built. With no context an agent is built with no tools rather than a snapshot.

    An unknown role is a refusal, not a fallback: silently building the wrong agent is how a swarm
    dispatches a node to a worker that cannot do its job.
    """
    if role.name == ARCHITECT_ROLE.name:
        from agents.architect import build_architect_agent

        return build_architect_agent(mcp_session=context)
    if role.name in coders_mod.ROLE_BY_NAME:
        return coders_mod.build_coder_agent(role, context)
    raise ValueError(f"unknown role requested: {role.name!r}")


def _coder_entry(role: AgentRole, *, parent_agent: str, resolve: Callable[[str], str]) -> Dict[str, Any]:
    """One coder role as the UI's catalog reports it."""
    if role.name == "coder-deep":
        core = getattr(coders_mod, "CODER_DEEP_CORE_PROMPT", role.system_prompt)
        custom = getattr(coders_mod, "CODER_DEEP_CUSTOM_INSTRUCTIONS", "")
    else:
        core = getattr(coders_mod, "CODER_STANDARD_CORE_PROMPT", role.system_prompt)
        custom = getattr(coders_mod, "CODER_STANDARD_CUSTOM_INSTRUCTIONS", "")

    return {
        "id": role.name,
        "name": role.name,
        "display_name": role.display_name,
        "type": "coder",
        "role": "Sub-Agent",
        "model": role.model or coder_model(role.name),
        "description": role.description,
        "system_prompt": resolve(role.system_prompt),
        "core_prompt": resolve(core),
        "custom_instructions": custom,
        # Bound per node from the MCP session, never snapshotted into the catalog.
        "tools": [],
        "parent_agent": parent_agent,
        "file_path": os.path.abspath(coders_mod.__file__),
        "status": "ready",
    }


def build_catalog(resolve: Callable[[str], str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The roles that exist, as ``(main_agents, coder_agents)`` keyed by agent id.

    A description, not a discovery: the architect is one explicit role and the coders are the
    explicit tuple, so nothing is inferred from what happens to be in a module's namespace. The
    ``tools`` list is empty by construction -- a tool set is bound per node at spawn time.
    """
    architect = ARCHITECT_ROLE
    main_agents: Dict[str, Any] = {
        architect.name: {
            "id": architect.name,
            "name": architect.name,
            "display_name": architect.display_name,
            "type": "main",
            "role": "Coordinator",
            "model": architect.model,
            "system_prompt": resolve(architect.system_prompt),
            "core_prompt": resolve(getattr(arch_mod, "ARCHITECT_CORE_PROMPT", architect.system_prompt)),
            "custom_instructions": getattr(arch_mod, "ARCHITECT_CUSTOM_INSTRUCTIONS", ""),
            "subagents": [role.name for role in coders_mod.CODER_ROLES],
            "tools": [],
            "file_path": os.path.abspath(arch_mod.__file__),
            "status": "ready",
        }
    }

    coder_agents: Dict[str, Any] = {
        role.name: _coder_entry(role, parent_agent=architect.name, resolve=resolve)
        for role in coders_mod.CODER_ROLES
    }
    return main_agents, coder_agents
