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
