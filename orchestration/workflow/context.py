"""The ambient dependencies a workflow action runs against.

Each action branch used to reach its emitter, narrator and plan filename through
closures created inside `run_agent_workflow`. Bundling them here keeps an
extracted action's signature about what it does rather than how it reaches the
outside world, and gives the runner one object to build and hand down.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict


@dataclass
class WorkflowContext:
    """Per-run dependencies shared by every action branch."""

    emit_fn: Callable[[Dict[str, Any]], None]
    stream_text: Callable[..., None]
    should_stop: Callable[[], bool]
    plan_file: str
