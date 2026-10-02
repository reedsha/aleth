"""Session-wide isolation from the developer's own plan directory and state root.

The suite must never read or write the roadmap in the repository root, and it must never write
the state store under the developer's own ``~/.aleth``. The plan directory is pointed at a
throwaway directory, and the *state root* at another: the store is keyed by the plan directory
but written under the state root, so isolating one without the other would leave a state
directory behind for every temp plan the suite creates.

The real roadmap is copied in, so the tests that deliberately measure the *real* plan
(``tests/test_context_slicer.py``) still have something to measure -- without touching the
original.
"""

import os
import shutil
import tempfile

import pytest

from tools import workspace

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The routing overrides a developer's ``.env`` may carry. ``env_boot.load_environment`` loads
# ``.env`` with ``override=True`` at the moment ``app`` is imported, so a single test that imports
# ``app`` arms these names for every test that runs *after* it in the same worker -- the same
# mechanism that made ``LAYA_BACKEND`` order-dependent, which ``_deterministic_system1`` fixes by
# injection. Injection is unavailable here (``coder_model`` reads the environment directly, by
# design: a settings panel writes these names), so they are cleared around every test instead. A
# test that asserts a committed routing default must not depend on which developer ran the suite.
_AMBIENT_ROUTING_ENV = (
    "ALETH_ARCHITECT_MODEL",
    "ALETH_CODER_DEEP_MODEL",
    "ALETH_CODER_STANDARD_MODEL",
)


def _never_loads():
    raise AssertionError("the deterministic System 1 must never load a checkpoint")


@pytest.fixture(autouse=True)
def _no_ambient_routing_overrides():
    """Run every test with the routing env vars cleared, then restore them.

    Cleared *before* the test and restored *after*, so the fixture holds even when the variables
    appear mid-session (when a test imports ``app`` and the bootloader re-arms them).
    """
    saved = {name: os.environ.get(name) for name in _AMBIENT_ROUTING_ENV}
    for name in _AMBIENT_ROUTING_ENV:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(autouse=True)
def _deterministic_system1():
    """Answer System 1 from the word list, by **injection**, for every test.

    The engine seam is a process global that ``agents.laya_model.classify`` builds once and
    caches. Left to the ambient environment it is a hidden input: ``env_boot.load_environment``
    loads ``.env`` with ``override=True``, so a test that imports ``app`` mid-process arms
    ``LAYA_BACKEND=model`` for every test that runs after it in the same worker. Whether the
    characterization suite observed the checkpoint or the word list then depended on test
    ordering -- the flake this replaces. Injecting a resolver here makes the environment
    irrelevant: a test that wants the model path constructs its own resolver with a fake
    router, as ``tests/test_laya_model.py`` does.
    """
    from agents import laya_model

    previous = laya_model.installed()
    laya_model.install(laya_model.Resolver(False, _never_loads))
    try:
        yield
    finally:
        laya_model.install(previous)


@pytest.fixture(scope="session", autouse=True)
def _isolate_plan_directory():
    original = workspace.PLAN_DIR
    original_state = os.environ.get(workspace.STATE_ENV)
    sandbox = tempfile.mkdtemp(prefix="aleth_plan_session_")
    state = tempfile.mkdtemp(prefix="aleth_state_session_")
    # A deterministic provider seam. No test makes a real call -- completers are scripted and the
    # transports are stubbed -- but several build a real agent (``create_deep_agent`` constructs
    # ``ChatOpenAI``), which requires *a* key to exist at all. Set unconditionally and pointed at
    # an unroutable host, so the suite can never spend the developer's credentials and a shell
    # without the key is not a suite failure.
    os.environ["OPENAI_API_KEY"] = "sk-test-placeholder"
    os.environ["OPENAI_BASE_URL"] = "https://example.invalid/v1"
    # The store is keyed by the plan directory and written to the *state root*, so pointing one at
    # a throwaway directory is not enough on its own.
    os.environ[workspace.STATE_ENV] = state
    # Seed it with the developer's own roadmap so the real-plan tests still run.
    source = os.path.join(_REPO_ROOT, "PLAN.md")
    if os.path.isfile(source):
        try:
            shutil.copy2(source, os.path.join(sandbox, "PLAN.md"))
        except OSError:
            pass
    workspace.PLAN_DIR = sandbox
    # The pointer moved, so the published absolute state root moves with it (Phase 29): a spawned
    # worker reads the environment, not the pointer.
    workspace.publish_state_root()
    try:
        yield
    finally:
        workspace.PLAN_DIR = original
        if original_state is None:
            os.environ.pop(workspace.STATE_ENV, None)
        else:
            os.environ[workspace.STATE_ENV] = original_state
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(state, ignore_errors=True)
