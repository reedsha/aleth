"""Characterization test suite for DeepAgents Studio.

These tests pin down the *existing* observable behavior of the plan engine,
file operations, and workflow event contract. They are intentionally written
before refactoring so that any structural change can be proven behavior
preserving.

Run with:
    .\\venv\\Scripts\\python.exe -m unittest discover -s tests -t .
"""
