"""System-prompt persistence: rewrites the ``agents/`` source files on disk.

This is the only place in the codebase that *edits Python source as text*. The
agent modules double as data files -- ``ARCHITECT_CUSTOM_INSTRUCTIONS``,
``CODER_DEEP_CUSTOM_INSTRUCTIONS`` and friends are rewritten in place by regex so
the customisation survives a restart. That makes this a genuinely separate
concern from workflow orchestration, and one worth isolating: it is the highest
blast-radius code in the project, and it is now testable without a live registry.

Two rules govern every function here:

* Prompt constant names and the ``build_architect_agent`` signature are part of a
  published contract with those data files. Do not rename or reformat them.
* The hot-reload side effects (rebuilding the in-memory graph) must run at exactly
  the point they used to, inside the same try/except, so a failure still surfaces
  as ``{"success": False, "error": ...}`` rather than an exception. Hence the
  injected ``rescan`` callable rather than a return-and-let-the-caller-do-it split.
"""

import os
import re
from typing import Any, Callable, Dict

import agents.architect as arch_mod
import agents.coders as coders_mod
from tools import atomic_io


def edit_architect_prompt(
    agent_id: str,
    new_prompt: str,
    is_custom_only: bool,
    rescan: Callable[[], Any],
) -> Dict[str, Any]:
    """Persists a new prompt for the Lead Architect.

    With ``is_custom_only`` the core system rules and routing schema are protected;
    only the custom developer directives block is replaced.
    """
    arch_file = os.path.abspath(arch_mod.__file__)
    try:
        with open(arch_file, "r", encoding="utf-8") as f:
            content = f.read()

        if is_custom_only:
            arch_mod.ARCHITECT_CUSTOM_INSTRUCTIONS = new_prompt
            pattern = r'ARCHITECT_CUSTOM_INSTRUCTIONS\s*=\s*""".*?"""'
            replacement = f'ARCHITECT_CUSTOM_INSTRUCTIONS = """{new_prompt}"""'
            updated_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
            full_prompt = f"{arch_mod.ARCHITECT_CORE_PROMPT}\n\n### Custom Developer Directives:\n{new_prompt}"
            arch_mod.ARCHITECT_SYSTEM_PROMPT = full_prompt
            arch_mod.build_architect_agent(system_prompt=full_prompt)
        else:
            arch_mod.ARCHITECT_SYSTEM_PROMPT = new_prompt
            arch_mod.build_architect_agent(system_prompt=new_prompt)
            pattern = r'ARCHITECT_SYSTEM_PROMPT\s*=\s*""".*?"""'
            replacement = f'ARCHITECT_SYSTEM_PROMPT = """{new_prompt}"""'
            if re.search(pattern, content, flags=re.DOTALL):
                updated_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
            else:
                updated_content = content

        atomic_io.write_text_atomic(arch_file, updated_content)

        rescan()
        return {"success": True, "message": f"Successfully updated system prompt for {agent_id}"}
    except Exception as e:
        return {"success": False, "error": f"Failed to persist to disk: {str(e)}"}


def edit_coder_prompt(
    agent_id: str,
    coder: Dict[str, Any],
    new_prompt: str,
    is_custom_only: bool,
    rescan: Callable[[], Any],
) -> Dict[str, Any]:
    """Persists a new prompt for a coder sub-agent.

    ``coder`` is the registry's catalogue record for the agent; only its ``name``
    is used, to pick the constant pair and the system-prompt global to patch.
    """
    coder_name = coder["name"]
    coder_file = os.path.abspath(coders_mod.__file__)

    if coder_name == "coder-deep":
        custom_var = "CODER_DEEP_CUSTOM_INSTRUCTIONS"
        core_val = coders_mod.CODER_DEEP_CORE_PROMPT
    elif coder_name == "coder-standard":
        custom_var = "CODER_STANDARD_CUSTOM_INSTRUCTIONS"
        core_val = coders_mod.CODER_STANDARD_CORE_PROMPT
    else:
        return {"success": False, "error": f"Unknown coder agent: {agent_id}"}

    try:
        with open(coder_file, "r", encoding="utf-8") as f:
            content = f.read()

        if is_custom_only:
            setattr(coders_mod, custom_var, new_prompt)
            pattern = rf'{custom_var}\s*=\s*""".*?"""'
            replacement = f'{custom_var} = """{new_prompt}"""'
            updated_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
            full_prompt = f"{core_val}\n\n### Custom Developer Directives:\n{new_prompt}"
            if coder_name == "coder-deep":
                coders_mod.CODER_DEEP_SYSTEM_PROMPT = full_prompt
                # The role descriptor, not a dict of agent state. The assignment contract is the
                # same one the dict had -- hot-reload is a mutation, and the dataclass is mutable
                # precisely so this line did not have to change shape.
                coders_mod.coder_deep.system_prompt = full_prompt
            else:
                coders_mod.CODER_STANDARD_SYSTEM_PROMPT = full_prompt
                coders_mod.coder_standard.system_prompt = full_prompt
        else:
            const_name = f"{coder_name.upper().replace('-', '_')}_SYSTEM_PROMPT"
            pattern = rf'{const_name}\s*=\s*""".*?"""'
            replacement = f'{const_name} = """{new_prompt}"""'
            updated_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
            # Update the in-memory state too, exactly as the architect branch does. Without
            # this, the rebuild below (build_architect_agent / rescan) would read the stale
            # system prompt and the UI's agents_updated event would report the old one, so
            # the edit would appear not to have taken effect until a restart.
            setattr(coders_mod, const_name, new_prompt)
            if coder_name == "coder-deep":
                coders_mod.coder_deep.system_prompt = new_prompt
            else:
                coders_mod.coder_standard.system_prompt = new_prompt

        atomic_io.write_text_atomic(coder_file, updated_content)

        arch_mod.build_architect_agent()
        rescan()
        return {"success": True, "message": f"Successfully updated system prompt for {agent_id}"}
    except Exception as e:
        return {"success": False, "error": f"Failed to persist to disk: {str(e)}"}
