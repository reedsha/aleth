"""Static source metrics for the workspace, so the Analyze view can show real numbers.

The analysis action could report only how many files it found and how many findings the
audit raised, because nothing measured the source itself. Complexity and security widgets
were left out of the dashboard rather than drawn as zeroes -- a zero reads as a clean bill
of health, which is a claim the app had no basis for. This computes the figures from the
code with the standard library's own parser: no third-party dependency, and no number that
is not actually derived from the source.

Read-only, and total: a module that does not parse lowers the coverage figure and is named
in ``unparsed`` instead of raising, so one broken file cannot blank the dashboard.
"""

import ast
import os
from typing import Any, Dict, List, Optional

from tools.workspace import get_project_dir, walk_workspace

# Cyclomatic complexity at or above which a function is reported as a hotspot. Ten is the
# conventional "this function is doing too much" line, and reporting every function would
# make the list an inventory rather than a place to look.
HOTSPOT_THRESHOLD = 10

# The dashboard is a summary, not a report: bound both lists so a large workspace cannot
# push the interesting entries off the end.
MAX_HOTSPOTS = 8
MAX_FLAGS = 12

# Nodes that each add one independent path through a function. Comprehensions carry their
# own `ifs`, handled separately below because a comprehension is one node for N conditions.
_BRANCH_NODES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
    ast.IfExp, ast.comprehension,
)

# The security review a static reader can actually stand behind: shaped calls whose danger
# is a property of the call, not of a name that merely contains "eval". A bare-name call is
# matched by name (``eval(...)``), a dotted call by its full path (``os.system``); a method
# that merely *shares* a name (``df.eval()``) is not flagged.
_RISKY_NAMES = ("eval", "exec", "__import__")
_RISKY_ATTRS = {
    "os.system": "os.system",
    "os.popen": "os.popen",
    "pickle.loads": "pickle.loads",
    "pickle.load": "pickle.load",
    "marshal.loads": "marshal.loads",
    "yaml.load": "yaml.load (unsafe loader)",
}
_RISKY_SUBPROCESS_ATTRS = ("run", "call", "check_call", "check_output", "Popen")


def _complexity_of(node: ast.AST) -> int:
    """Cyclomatic complexity of one function: 1 + the branch nodes it owns.

    Nested functions and classes are counted in their own pass, so their branches are
    skipped here rather than charged twice to the outer function.
    """
    score = 1
    stack = list(getattr(node, "body", []) or [])
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(current, _BRANCH_NODES):
            score += 1
            if isinstance(current, ast.comprehension):
                score += max(0, len(current.ifs) - 1)
        elif isinstance(current, ast.BoolOp):
            score += max(0, len(current.values) - 1)
        stack.extend(ast.iter_child_nodes(current))
    return score


def _call_name(node: ast.Call) -> Optional[str]:
    """The dotted name of a call's target (``os.system``), or ``None`` if it is not one."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return None


def _security_flags(tree: ast.AST) -> List[Dict[str, Any]]:
    """The risky calls in one module, with the line each one sits on."""
    flags: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            if node.func.id in _RISKY_NAMES:
                flags.append({"line": node.lineno, "kind": node.func.id})
            continue
        name = _call_name(node)
        if not name:
            continue
        if name in _RISKY_ATTRS:
            flags.append({"line": node.lineno, "kind": _RISKY_ATTRS[name]})
            continue
        # `subprocess.run(..., shell=True)` is the shell-injection shape; a subprocess call
        # without it is ordinary and is not flagged.
        if name.startswith("subprocess.") and name.split(".", 1)[1] in _RISKY_SUBPROCESS_ATTRS:
            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    flags.append({"line": node.lineno, "kind": f"{name} shell=True"})
                    break
    return flags


def analyze_source(text: str, filename: str = "<source>") -> Dict[str, Any]:
    """Metrics for one module's source, or ``{"error": ...}`` when it does not parse."""
    try:
        tree = ast.parse(text, filename=filename)
    except (SyntaxError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    functions: List[Dict[str, Any]] = []
    classes = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "line": node.lineno,
                "complexity": _complexity_of(node),
            })
        elif isinstance(node, ast.ClassDef):
            classes += 1

    lines = sum(1 for line in text.splitlines() if line.strip())
    return {
        "lines": lines,
        "classes": classes,
        "functions": functions,
        "security_flags": _security_flags(tree),
    }


def _source_files(base_dir: str):
    """Yields ``(relative_path, text)`` for every Python module in the workspace."""
    for root, _dirs, filenames in walk_workspace(base_dir):
        for filename in filenames:
            if not filename.endswith(".py") or filename.startswith("."):
                continue
            full = os.path.join(root, filename)
            rel = os.path.relpath(full, base_dir).replace("\\", "/")
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    yield rel, handle.read()
            except OSError:
                continue


def analyze_workspace_metrics(base_dir: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate metrics over the workspace's Python modules.

    Pure read: it parses what is on disk and never writes. ``coverage`` is the share of
    modules that parsed, so a syntax error is visible in the totals rather than hidden.
    """
    base = os.path.abspath(base_dir or get_project_dir())

    modules = 0
    lines = 0
    classes = 0
    complexities: List[int] = []
    hotspots: List[Dict[str, Any]] = []
    flags: List[Dict[str, Any]] = []
    unparsed: List[str] = []

    for rel, text in _source_files(base):
        report = analyze_source(text, filename=rel)
        if "error" in report:
            unparsed.append(rel)
            continue
        modules += 1
        lines += report["lines"]
        classes += report["classes"]
        for function in report["functions"]:
            complexities.append(function["complexity"])
            if function["complexity"] >= HOTSPOT_THRESHOLD:
                hotspots.append({"file": rel, **function})
        for flag in report["security_flags"]:
            flags.append({"file": rel, **flag})

    hotspots.sort(key=lambda item: item["complexity"], reverse=True)
    flags.sort(key=lambda item: (item["file"], item["line"]))
    average = round(sum(complexities) / len(complexities), 2) if complexities else 0.0

    return {
        "modules": modules,
        "functions": len(complexities),
        "classes": classes,
        "lines": lines,
        "average_complexity": average,
        "max_complexity": max(complexities) if complexities else 0,
        "hotspot_threshold": HOTSPOT_THRESHOLD,
        "hotspots": hotspots[:MAX_HOTSPOTS],
        "hotspot_count": len(hotspots),
        "security_flags": flags[:MAX_FLAGS],
        "security_flag_count": len(flags),
        "coverage": round(modules / (modules + len(unparsed)), 2) if modules or unparsed else 1.0,
        "unparsed": unparsed,
    }
