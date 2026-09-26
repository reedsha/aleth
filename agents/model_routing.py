"""Which model answers for each role, in one place and overridable.

These names used to be literals in thirteen places: the two Coder definitions, the
architect's agent construction, the catalogue's fallbacks, three delegation branches and
the two spawn events. That is exactly the drift this module removes -- and the drift was
real: every agent was pinned to a policy that does not exist on the configured router
(``policy/architect``, ``policy/coder-deep``, ``policy/coder`` all 404), and nothing said
so, because a model name only reaches a provider when a workflow actually spawns.

Defaults are literal so a bare checkout still runs, but every one of them reads an
environment variable first, which is the seam a settings panel will write to. The
provider's key and base URL are already environment-driven (``env_boot`` loads ``.env``
over the shell), so overriding a route is the same kind of change as overriding a key.

Import-light on purpose: ``os`` and nothing else, so any layer may import it. ``agents``
is imported by ``orchestration``, which is imported by the entrypoints, so a module here
that pulled in a sibling would decide import order for the whole app.
"""

import os

# Mirrors the agent ids in ``agents/coders.py`` and ``agents/laya.py``; compared as
# strings rather than imported so this stays a leaf module.
CODER_DEEP = "coder-deep"
CODER_STANDARD = "coder-standard"

ENV_ARCHITECT = "DEEPAGENTS_ARCHITECT_MODEL"
ENV_CODER_DEEP = "DEEPAGENTS_CODER_DEEP_MODEL"
ENV_CODER_STANDARD = "DEEPAGENTS_CODER_STANDARD_MODEL"

# The routes this project is developed against. ``coder-deep-test`` is a high-reasoning
# free route, so the Gatekeeper runs on it too: planning is where reasoning pays, and the
# architect's own job -- deciding, verifying and delegating -- is not cheaper for being
# answered by a weaker model.
DEFAULT_ARCHITECT = "openai:policy/coder-deep-test"
DEFAULT_CODER_DEEP = "openai:policy/coder-deep-test"
DEFAULT_CODER_STANDARD = "openai:policy/coder-standard-test"


def _route(env_name: str, default: str) -> str:
    """The configured route, or the default. A blank value counts as unset."""
    return (os.environ.get(env_name) or "").strip() or default


def architect_model() -> str:
    """The model the Gatekeeper runs on. Env: ``DEEPAGENTS_ARCHITECT_MODEL``."""
    return _route(ENV_ARCHITECT, DEFAULT_ARCHITECT)


def coder_model(coder_id: str) -> str:
    """The model for a Coder id, defaulting to the standard route for anything else.

    Taking the id rather than exposing two constants is what lets the delegation branches
    name a route for whichever Coder the System 1 gate actually chose, instead of
    re-deriving that choice from a string.
    """
    if coder_id == CODER_DEEP:
        return _route(ENV_CODER_DEEP, DEFAULT_CODER_DEEP)
    return _route(ENV_CODER_STANDARD, DEFAULT_CODER_STANDARD)
