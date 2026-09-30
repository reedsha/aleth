"""Agent *roles*: the description of an agent, not an agent.

Phase 6 removes the static agent registry. ``coder_deep`` and ``coder_standard`` used to be
dictionaries of live agent state -- a name, a prompt, a model, and a snapshot of the tools it
owned -- sitting in module scope from the moment the process started. That is what a swarm cannot
have: an agent is now an **ephemeral compute instance** spawned for one node of the DAG, with the
tools that node's domain requires bound at spawn time from the MCP servers.

So what remains here is data. An :class:`AgentRole` says how to build an agent -- what it is
called, what it is for, which prompt and model route it uses -- and deliberately says **nothing
about tools**. ``to_subagent()`` produces the dict shape ``create_deep_agent`` expects for a
subagent, which is a library contract; the role itself is not that dict, and no dict of agent
state lives at module scope any more.

The dataclass is **mutable on purpose**. ``orchestration.prompt_editor`` rewrites the prompt
constants in ``agents/coders.py`` as text and then hot-reloads by assignment, so
``coder_deep.system_prompt = new_prompt`` has to keep working exactly as
``coder_deep["system_prompt"] = new_prompt`` did. Freezing the field would break the editor's
documented contract to make a point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class AgentRole:
    """How to build one kind of agent. Not an agent, and owns no tools."""

    name: str
    display_name: str
    description: str
    system_prompt: str
    model: str

    def to_subagent(self) -> Dict[str, Any]:
        """The subagent dict ``create_deep_agent`` expects, built on demand.

        ``tools`` is empty by construction: a subagent's tool set is bound per node from the MCP
        session (Phase 4), so there is nothing to snapshot and nothing that can go stale.
        """
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "model": self.model,
            "tools": [],
        }

    def to_catalog_entry(self, *, parent_agent: str, file_path: str) -> Dict[str, Any]:
        """The shape the UI's agent catalog reports for this role."""
        return {
            "id": self.name,
            "name": self.name,
            "display_name": self.display_name,
            "type": "coder",
            "role": "Sub-Agent",
            "model": self.model,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "tools": [],
            "parent_agent": parent_agent,
            "file_path": file_path,
            "status": "ready",
        }
