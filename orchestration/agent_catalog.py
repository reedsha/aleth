"""Agent introspection: reflection over the ``agents/`` packages.

``AgentRegistry.scan_agents`` used to inline all of this. The logic is pure with
respect to the registry -- it reads the agent modules and returns two grouped
dictionaries -- with the single exception of prompt-variable interpolation, which
is injected as a callable so this module never needs the registry back.

Keeping the reflection here makes the "what does the UI see about an agent?"
contract testable without standing up a registry, and keeps the registry focused
on workflow orchestration.
"""

import os
from typing import Any, Callable, Dict, Tuple

from langgraph.graph.state import CompiledStateGraph

import agents.architect as arch_mod
import agents.coders as coders_mod


def resolve_prompt_variables(prompt: str, active_plan: str) -> str:
    """Replaces dynamic template variables like {{ACTIVE_PLAN_FILE}} with current state."""
    if not prompt:
        return ""
    return prompt.replace("{{ACTIVE_PLAN_FILE}}", active_plan)


def build_catalog(resolve: Callable[[str], str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Dynamically inspects, tracks, and groups available agents into Main and Coder tiers.

    ``resolve`` interpolates template variables in the prompts that get surfaced to
    the UI; the registry passes its own bound ``resolve_prompt_variables``.

    Returns ``(main_agents, coder_agents)``, both keyed by agent id.
    """
    main_agents: Dict[str, Any] = {}
    coder_agents: Dict[str, Any] = {}

    # 1. Scan Main Agents from agents.architect
    for attr_name in dir(arch_mod):
        obj = getattr(arch_mod, attr_name)
        # Check for instances initialized via create_deep_agent (CompiledStateGraph)
        if isinstance(obj, CompiledStateGraph) or hasattr(obj, "nodes"):
            agent_id = getattr(obj, "name", None) or getattr(obj, "agent_name", "software-architect")
            display_name = getattr(obj, "display_name", "Lead Software Architect")
            system_prompt = getattr(arch_mod, "ARCHITECT_SYSTEM_PROMPT", "")
            core_prompt = getattr(arch_mod, "ARCHITECT_CORE_PROMPT", system_prompt)
            custom_instructions = getattr(arch_mod, "ARCHITECT_CUSTOM_INSTRUCTIONS", "")
            model = getattr(obj, "model_name", "openai:policy/architect")
            subagents = getattr(obj, "subagents_list", coders_mod.all_coders)
            subagent_names = [sub.get("name") for sub in subagents if isinstance(sub, dict)]

            tools_list = []
            for t in getattr(obj, "tools_list", []):
                tools_list.append(getattr(t, "name", str(t)))

            main_agents[agent_id] = {
                "id": agent_id,
                "name": agent_id,
                "display_name": display_name,
                "type": "main",
                "role": "Coordinator",
                "model": model,
                "system_prompt": resolve(system_prompt),
                "core_prompt": resolve(core_prompt),
                "custom_instructions": custom_instructions,
                "subagents": subagent_names,
                "tools": tools_list,
                "file_path": os.path.abspath(arch_mod.__file__),
                "status": "ready"
            }

            # 2. Scan Coder Agents passed into subagents array
            for sub in subagents:
                if isinstance(sub, dict) and not isinstance(sub, CompiledStateGraph):
                    sub_id = sub.get("name", "coder")
                    sub_tools = [getattr(t, "name", str(t)) for t in sub.get("tools", [])]

                    if sub_id == "coder-deep":
                        coder_core = getattr(coders_mod, "CODER_DEEP_CORE_PROMPT", sub.get("system_prompt", ""))
                        coder_custom = getattr(coders_mod, "CODER_DEEP_CUSTOM_INSTRUCTIONS", "")
                    elif sub_id == "coder-standard":
                        coder_core = getattr(coders_mod, "CODER_STANDARD_CORE_PROMPT", sub.get("system_prompt", ""))
                        coder_custom = getattr(coders_mod, "CODER_STANDARD_CUSTOM_INSTRUCTIONS", "")
                    else:
                        coder_core = sub.get("system_prompt", "")
                        coder_custom = ""

                    coder_agents[sub_id] = {
                        "id": sub_id,
                        "name": sub_id,
                        "display_name": sub.get("display_name", sub_id.replace("-", " ").title()),
                        "type": "coder",
                        "role": "Sub-Agent",
                        "model": sub.get("model", "openai:policy/coder"),
                        "description": sub.get("description", ""),
                        "system_prompt": resolve(sub.get("system_prompt", "")),
                        "core_prompt": resolve(coder_core),
                        "custom_instructions": coder_custom,
                        "tools": sub_tools,
                        "parent_agent": agent_id,
                        "file_path": os.path.abspath(coders_mod.__file__),
                        "status": "ready"
                    }

    return main_agents, coder_agents
