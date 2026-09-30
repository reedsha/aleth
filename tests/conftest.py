"""Session-wide isolation from the developer's own plan directory.

The suite must never read or write the roadmap in the repository root. Loading plan state
creates the SQLite store beside it, so a test that forgot to isolate itself would leave a
``aleth_state.db`` in the user's working tree (and could, in principle, rewrite their
``PLAN.md``). This points the plan directory at a throwaway directory for the whole
session, before any test runs, and restores it afterwards.

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
    sandbox = tempfile.mkdtemp(prefix="aleth_plan_session_")
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
        shutil.rmtree(sandbox, ignore_errors=True)
