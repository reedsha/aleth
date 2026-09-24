"""Orchestration layer for DeepAgents.

Split out of the former monolithic ``registry.py``. The package is named
``orchestration`` rather than ``registry`` on purpose: ``registry.py`` is imported
as a top-level module by ``main.py`` and ``app.py``, so a package of that name
would shadow it.

Modules
-------
agent_catalog   reflection over ``agents/`` -> the catalogue the UI renders
prompt_editor   persists edited system prompts back into the ``agents/`` sources
plan_session    scaffolds a roadmap and reads the active plan
"""

from orchestration.agent_catalog import build_catalog, resolve_prompt_variables
from orchestration.plan_session import read_current_plan, scaffold_plan_file
from orchestration.prompt_editor import edit_architect_prompt, edit_coder_prompt

__all__ = [
    "build_catalog",
    "resolve_prompt_variables",
    "read_current_plan",
    "scaffold_plan_file",
    "edit_architect_prompt",
    "edit_coder_prompt",
]
