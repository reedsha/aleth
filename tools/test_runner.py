"""Run the regression tests a task wrote, and report what really happened.

The result view's pass/fail strip had nothing behind it: the Coder writes a test file, but
no code ever executed it, so the view could only say "not run". This supplies the missing
verdict by running the test file(s) a task wrote and reading the runner's own result -- its
exit status and its summary line -- rather than inferring a badge from prose.

Read-only with respect to the workspace. The test files already exist when this is called;
they are executed, not written, and byte-code writing and pytest's cache are disabled, so
merely opening a result view cannot leave new files in a workspace the user did not ask to
touch.

The answers stay distinct, because the caller draws each one differently: no test file was
recorded, the recorded file is gone, the tests passed, the tests failed, a collection error
stopped them from running, or the runner could not be reached at all. A missing runner is
never reported as a pass.
"""

import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List

from tools.workspace import BACKUP_SUBDIR, get_project_dir

# A runaway suite must not hold the result view; the bridge call is synchronous, so this
# is what keeps "open the diff" from becoming "hang the window".
TEST_TIMEOUT_SECONDS = 60

# Enough of the runner's output to explain a failure, not the whole traceback.
MAX_OUTPUT_CHARS = 4000

_TEST_NAME_RE = re.compile(r"test", re.IGNORECASE)

# pytest's one-line summary is the machine-readable part of its output. Counting the words
# survives -q, --no-header and --tb alike; scraping the glyph rows would not.
_SUMMARY_COUNTS = (
    ("passed", re.compile(r"(\d+) passed")),
    ("failed", re.compile(r"(\d+) failed")),
    ("errors", re.compile(r"(\d+) errors?")),
    ("skipped", re.compile(r"(\d+) skipped")),
)

_EMPTY_TOTALS = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}

# Worst answer wins: one failing file must not be hidden by a passing sibling.
_VERDICT_PRIORITY = ("failed", "error", "passed", "missing", "unavailable")


def _recorded_files(task_key: str) -> List[str]:
    """The workspace-relative files a task's backup metadata records, or an empty list.

    Reads ``.deepagents_backups/<task_key>/_meta.json`` -- the same record ``task_diff``
    reads -- so this names the files the task actually touched rather than guessing.
    """
    meta_path = os.path.join(get_project_dir(), BACKUP_SUBDIR, task_key, "_meta.json")
    if not os.path.isfile(meta_path):
        return []
    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(meta, dict):
        return []
    return [str(name).replace("\\", "/") for name in meta]


def _paired_test_path(path: str) -> str:
    """``a/b.py`` -> ``a/test_b.py``, by the same rule the Coder writes tests with.

    Imported lazily: ``tools`` is a lower layer than ``orchestration``, and a module-level
    import here would let ``orchestration -> tools -> orchestration`` close a cycle. The
    single source stays ``orchestration.workflow.generation.paired_test_path``.
    """
    from orchestration.workflow.generation import paired_test_path

    return paired_test_path(path)


def test_files_for_task(task_key: str) -> List[str]:
    """The test files a task is answerable for, most explicit first.

    A task that recorded its own test file (the ``next_step`` branch snapshots both the
    deliverable and its test) names it directly. A task that recorded only a source file
    (``fix_bug`` snapshots the patched file alone) still has a test beside it -- written by
    the same pairing rule -- so the paired path is derived and used when it exists on disk.
    Recorded test files are kept even when they have since been deleted, so the caller can
    say the file is gone instead of silently reporting "no tests".
    """
    recorded = _recorded_files(task_key)
    if not recorded:
        return []

    base_dir = get_project_dir()
    found: List[str] = []
    for name in recorded:
        if name.lower().endswith(".py") and _TEST_NAME_RE.search(os.path.basename(name)):
            if name not in found:
                found.append(name)
    for name in recorded:
        if not name.lower().endswith(".py"):
            continue
        if _TEST_NAME_RE.search(os.path.basename(name)):
            continue
        paired = _paired_test_path(name)
        if paired not in found and os.path.isfile(os.path.join(base_dir, paired)):
            found.append(paired)
    return found


def _counts(output: str) -> Dict[str, int]:
    """The pass/fail/error/skip tallies from the runner's summary line."""
    counts = dict(_EMPTY_TOTALS)
    for key, pattern in _SUMMARY_COUNTS:
        match = pattern.search(output)
        if match:
            counts[key] = int(match.group(1))
    return counts


def _truncate(output: str) -> str:
    if len(output) <= MAX_OUTPUT_CHARS:
        return output
    half = MAX_OUTPUT_CHARS // 2
    return f"{output[:half]}\n... [truncated] ...\n{output[-half:]}"


def _run_one(base_dir: str, rel_path: str) -> Dict[str, Any]:
    """Run one recorded test file and classify what happened."""
    result: Dict[str, Any] = {
        "filename": rel_path, **_EMPTY_TOTALS,
        "verdict": "missing", "returncode": None, "output": "", "error": None,
    }
    if not os.path.isfile(os.path.join(base_dir, rel_path)):
        result["error"] = "The recorded test file is no longer in the workspace."
        return result

    env = dict(os.environ)
    # A reader that only wanted the verdict must not litter the workspace: no .pyc files
    # and no .pytest_cache directory.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    command = [
        sys.executable, "-m", "pytest", rel_path,
        "-q", "--no-header", "--tb=short",
        "-p", "no:cacheprovider",
        # Ignore any addopts the workspace's own pytest config would inject: the verdict
        # must come from this run, not from a project's reporting plugins.
        "-o", "addopts=",
    ]
    try:
        completed = subprocess.run(
            command, cwd=base_dir, capture_output=True, text=True,
            timeout=TEST_TIMEOUT_SECONDS, encoding="utf-8", errors="replace", env=env,
        )
    except subprocess.TimeoutExpired:
        result.update({
            "verdict": "error",
            "error": f"Tests did not finish within {TEST_TIMEOUT_SECONDS} seconds.",
        })
        return result
    except OSError as exc:
        result.update({
            "verdict": "unavailable",
            "error": f"Could not start the test runner: {exc}",
        })
        return result

    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    counts = _counts(output)
    result.update(counts)
    result["returncode"] = completed.returncode
    result["output"] = _truncate(output)

    if completed.returncode == 0:
        result["verdict"] = "passed"
    elif counts["failed"]:
        result["verdict"] = "failed"
    else:
        # A non-zero exit with nothing counted: a collection error, or "no tests ran".
        result["verdict"] = "error"
        if not counts["errors"]:
            result["error"] = "The test file produced no runnable tests."
    return result


def _aggregate_verdict(verdicts: List[str]) -> str:
    for verdict in _VERDICT_PRIORITY:
        if verdict in verdicts:
            return verdict
    return "none"


def _summary_line(totals: Dict[str, int], verdict: str) -> str:
    if verdict == "passed":
        return f"{totals['passed']} passed"
    if verdict == "failed":
        parts = []
        if totals["passed"]:
            parts.append(f"{totals['passed']} passed")
        parts.append(f"{totals['failed']} failed")
        if totals["errors"]:
            plural = "s" if totals["errors"] != 1 else ""
            parts.append(f"{totals['errors']} error{plural}")
        return ", ".join(parts)
    if verdict == "error":
        if totals["errors"]:
            plural = "s" if totals["errors"] != 1 else ""
            return f"{totals['errors']} error{plural}"
        return "No runnable tests"
    if verdict == "missing":
        return "The recorded test file is gone"
    return "Tests could not be run"


def run_task_tests(task_id: str) -> Dict[str, Any]:
    """Execute the regression tests a task wrote and return the verdict.

    Called from two places: the result view, when it opens (the on-demand path), and the
    workflow's verification gate, where ``next_step_action``/``fix_bug_action`` run it to
    choose a task's status. The gate passes the task key the snapshot was recorded under, so
    the runner finds the test file from the backup metadata rather than a guessed path.
    """
    empty = {
        "found": False, "ran": False, "verdict": "none",
        "tests": [], "totals": dict(_EMPTY_TOTALS), "summary": "",
    }
    task_key = str(task_id or "").strip()
    if not task_key:
        return {"success": False, "error": "No task id given.", "task_id": task_id, **empty}

    files = test_files_for_task(task_key)
    if not files:
        return {
            "success": True, "task_id": task_key, **empty,
            "summary": "No test file was recorded for this task.",
        }

    base_dir = get_project_dir()
    tests = [_run_one(base_dir, rel) for rel in files]
    totals = {key: sum(test[key] for test in tests) for key in _EMPTY_TOTALS}
    verdict = _aggregate_verdict([test["verdict"] for test in tests])
    ran = any(test["verdict"] in ("passed", "failed", "error") for test in tests)
    return {
        "success": True, "found": True, "ran": ran, "verdict": verdict,
        "task_id": task_key, "tests": tests, "totals": totals,
        "summary": _summary_line(totals, verdict),
    }
