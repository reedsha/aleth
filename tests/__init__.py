"""Characterization test suite for Aleth Studio.

These tests pin down the *existing* observable behavior of the plan engine,
file operations, and workflow event contract. They are intentionally written
before refactoring so that any structural change can be proven behavior
preserving.

Run with:
    .\\venv\\Scripts\\python.exe -m unittest discover -s tests -t .
"""

import os

# The Gatekeeper reads ``LAYA_BACKEND`` at its first decision, so a developer whose
# environment -- or a stale .env -- selects the model backend would otherwise change
# what these tests observe, and whether they pass. The suite therefore pins the engine
# it characterizes. The model path has its own tests, which inject a router.
os.environ["LAYA_BACKEND"] = "heuristic"
