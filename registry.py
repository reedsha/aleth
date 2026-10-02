"""Dynamic Agent Registry: the application's public entry point to the agent system.

This module used to hold the catalogue, the prompt editing, the plan session helpers
and every workflow branch inline -- roughly 1500 lines. Those concerns now live in
``orchestration/``:

    agent_catalog   reflection over ``agents/`` -> the catalogue the UI renders
    prompt_editor   persists edited system prompts back into the ``agents/`` sources
    plan_session    plan scaffolding and reading the active plan
    workflow.runner workflow sequencing, intent dispatch and error handling

What remains here is the stateful object the rest of the app talks to. It stays
importable as a *top-level module* (``from registry import registry``), which is why
the package it delegates to is named ``orchestration`` rather than ``registry``.
"""

import threading
from typing import Any, Callable, Dict, Optional

from orchestration import agent_catalog, plan_session, prompt_editor
from orchestration.workflow import runner
from tools.file_tools import (
    get_active_plan_filename,
    get_project_dir,
    list_plan_files,
)


class AgentRegistry:
    """
    Dynamic Agent Registry module for Aleth.
    Categorizes agents into:
    1. Main Agents (Coordinators): Initialized via create_deep_agent(...) with graph state & subagents.
    2. Coder Agents (Sub-Agents): Worker dictionaries/specifications in subagents array (lacking create_deep_agent).

    Provides stateless prompt sessions anchored to dynamic .md plan file memory,
    system prompt hot-reloading, full & restricted shell execution, and plan tree sync.
    """

    def __init__(self):
        self.workspace_dir = get_project_dir()
        self.main_agents: Dict[str, Dict[str, Any]] = {}
        self.coder_agents: Dict[str, Dict[str, Any]] = {}
        self.active_task_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.scan_agents()

    def resolve_prompt_variables(self, prompt: str) -> str:
        """Replaces dynamic template variables like {{ACTIVE_PLAN_FILE}} with current state."""
        return agent_catalog.resolve_prompt_variables(prompt, get_active_plan_filename())

    def scan_agents(self) -> Dict[str, Any]:
        """
        Dynamically inspects, tracks, and groups available agents into Main and Coder tiers.
        """
        main_agents, coder_agents = agent_catalog.build_catalog(self.resolve_prompt_variables)

        self.main_agents.clear()
        self.coder_agents.clear()
        self.main_agents.update(main_agents)
        self.coder_agents.update(coder_agents)

        return self.get_agent_summary()

    def get_agent_summary(self) -> Dict[str, Any]:
        """Returns structured JSON-serializable list of all categorized agents."""
        return {
            "main_agents": list(self.main_agents.values()),
            "coder_agents": list(self.coder_agents.values()),
            "workspace_dir": get_project_dir(),
            "active_plan": get_active_plan_filename(),
            "plan_files": list_plan_files()
        }

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Fetch details for a specific agent by ID."""
        if agent_id in self.main_agents:
            return self.main_agents[agent_id]
        if agent_id in self.coder_agents:
            return self.coder_agents[agent_id]
        return None

    def save_system_prompt(self, agent_id: str, new_prompt: str, is_custom_only: bool = False) -> Dict[str, Any]:
        """
        Dynamically updates the agent's system prompt in memory and persists to disk.
        Supports Tiered Editing: if is_custom_only is True, updates only the custom developer
        instructions block while strictly protecting core system rules and routing schemas.
        """
        new_prompt = new_prompt.strip()

        # Case 1: Main Agent (Architect)
        if agent_id in self.main_agents or agent_id == "software-architect":
            return prompt_editor.edit_architect_prompt(
                agent_id, new_prompt, is_custom_only, self.scan_agents
            )

        # Case 2: Coder Sub-Agent (e.g. coder-deep, coder-standard)
        if agent_id in self.coder_agents:
            return prompt_editor.edit_coder_prompt(
                agent_id, self.coder_agents[agent_id], new_prompt, is_custom_only, self.scan_agents
            )

        return {"success": False, "error": f"Agent {agent_id} not found in registry."}

    # -------------------------------------------------------------
    # Dynamic Plan Management Methods
    # -------------------------------------------------------------
    def create_new_plan_file(self, filename: str, project_idea: str) -> Dict[str, Any]:
        """
        Creates a new structured .md plan file and plan.json machine state based on user's project idea,
        stores the file in the workspace, sets it as active, and returns parsed tree.
        """
        return plan_session.scaffold_plan_file(filename, project_idea)

    def get_current_plan_data(self) -> Dict[str, Any]:
        """Fetches the active plan state from plan.json and PLAN.md."""
        return plan_session.read_current_plan()

    def stop_workflow(self):
        """Signals the running workflow to halt immediately.

        Sets the *current* run's stop event (see ``run_agent_workflow``), so a stop
        can never be re-interpreted as belonging to a later run.
        """
        self.stop_event.set()

    def run_agent_workflow(
        self,
        user_message: str,
        emit_fn: Callable[[Dict[str, Any]], None],
        action_type: str = "custom",
        action_params: Optional[Dict[str, Any]] = None,
        intent_id: str = "",
    ):
        """Runs a workflow, handing the runner the registry state it needs.

        The sequencing -- intent resolution, the Architect preamble, action dispatch
        and error handling -- lives in ``orchestration.workflow.runner``. The signal
        and coder catalogue stay here because they are the registry's own state.

        A fresh stop event is created for every run and published as ``self.stop_event``.
        Reusing one event and clearing it at the start of each run let a new run clear
        the flag a previous, still-winding-down run was stopping on -- un-cancelling it,
        so two workflows then wrote the same plan and files concurrently.

        ``intent_id`` is the run's identity, threaded to the workflow layer so a fault can be
        joined to the intent that caused it and so the layer can tell when it has been aborted
        (Phase 27). It is not used for anything else here.
        """
        run_stop_event = threading.Event()
        self.stop_event = run_stop_event
        runner.run_agent_workflow(
            coder_agents=self.coder_agents,
            stop_event=run_stop_event,
            user_message=user_message,
            emit_fn=emit_fn,
            action_type=action_type,
            action_params=action_params,
            intent_id=intent_id,
        )


# Global singleton instance
registry = AgentRegistry()
