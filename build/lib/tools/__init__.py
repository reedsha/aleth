"""Lazy facade for the workspace tooling package.

Importing a submodule such as ``tools.plan_parser`` executes this package ``__init__``
first. The flat names below used to be imported eagerly, which pulled in
``tools.file_tools`` -> a tool catalog -> ``langchain_core.tools`` on *every*
``import tools.*`` -- including the zero-token markdown parser, which has no business
loading an agent SDK. The names are therefore resolved on first access (PEP 562), so a
caller pays for a tool only when it actually uses one and the pure plan engine imports
in milliseconds rather than seconds.
"""

from importlib import import_module
from typing import Any


# name -> the module that defines it. Kept in one place so ``dir()`` and ``__all__`` stay
# accurate without importing anything.
_LAZY_EXPORTS = {
    "get_project_dir": "tools.file_tools",
    "set_project_dir": "tools.file_tools",
    "list_workspace_files": "tools.file_tools",
}

__all__ = sorted(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
