#!/bin/sh
# True immutability verification (Phase 11).
#
# An invariant assertion is not a substitute for the filesystem rejecting a write() syscall, so
# this does the brutal version: build the wheel, install it into an isolated virtualenv, strip the
# write bit from every installed byte, then boot the kernel and run a plan. If the engine writes a
# log, a .pyc or a SQLite journal anywhere inside the application directory, the OS denies it and
# this script fails.
#
# Run it from the repository root:
#
#     sh tools/verify_immutable_install.sh
#
# The heavy runtime dependencies are shared from an existing environment (``--system-site-packages``)
# so this stays a packaging test rather than a second torch download. The *installed aleth code* is
# always the wheel's own copy, which is the thing being locked.
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ALETH_IMMUTABLE_PYTHON:-python3}"
WORK="$(mktemp -d)"
VENV="$WORK/venv"

cleanup() {
    chmod -R u+w "$VENV" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

echo "=== 1. build the wheel ==="
cd "$REPO"
rm -rf dist build ./*.egg-info
"$PY" -m build --wheel --outdir "$WORK/dist"
WHEEL="$(ls "$WORK"/dist/*.whl)"
echo "built: $(basename "$WHEEL")"

echo "=== 2. the wheel carries no tests, no dev files ==="
"$PY" - "$WHEEL" <<'INSPECT'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
bad = [n for n in names
       if "/tests/" in n or n.startswith("tests/")
       or n.endswith((".pyc", ".pyo", ".db", ".log"))
       or "egg-info" in n and n.endswith(".txt") and "entry_points" not in n
       or n.startswith(("conftest", "pytest.ini", "playwright.config", "vite.config"))]
packages = sorted({n.split("/")[0] for n in names if "/" in n})
print("top-level entries:", packages)
if bad:
    print("UNWANTED IN WHEEL:", bad)
    raise SystemExit(1)
print("no tests, no bytecode, no state files in the wheel")
INSPECT

echo "=== 3. install into an isolated venv ==="
"$PY" -m venv "$VENV"
# ``--no-deps`` on purpose: the wheel's declared dependencies are the *runtime* fleet (torch,
# transformers), and re-downloading them would make this a packaging test that takes an hour. The
# driver below imports only the paths that write to disk, so it needs exactly one dependency.
"$VENV/bin/python" -m pip install --quiet --no-deps "$WHEEL"
"$VENV/bin/python" -m pip install --quiet pydantic
SITE="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
echo "site-packages: $SITE"

echo "=== 4. strip every write bit from the installed application ==="
APP=""
for name in agents core orchestration storage tools app.py bridge_bus.py env_boot.py main.py registry.py; do
    if [ -e "$SITE/$name" ]; then APP="$APP $SITE/$name"; fi
done
echo "locking:$APP"
chmod -R a-w $APP
# The permission *bits*, not an access test. ``test -w`` answers true for root whatever the mode,
# so a gate built on it passes inside a root container while proving nothing at all -- which is
# exactly how this check first failed, and it was right to fail.
if find $APP -perm -u+w | grep -q .; then
    echo "FATAL: the lock did not take"
    exit 1
fi

# Root also *writes* through the bits (CAP_DAC_OVERRIDE), so the boot must run as an ordinary user
# or the lock is decorative. That is how the application runs in production too.
RUN_USER=aleth
RUN=""
if [ "$(id -u)" = "0" ]; then
    id -u "$RUN_USER" >/dev/null 2>&1 || useradd -m -s /bin/sh "$RUN_USER"
    mkdir -p "$WORK/state" "$WORK/ws"
    chown -R "$RUN_USER" "$WORK"
    RUN="setpriv --reuid=$RUN_USER --regid=$RUN_USER --clear-groups"
fi

# And the same question from that user's side, which is the one that matters.
if [ -n "$RUN" ]; then
    if $RUN test -w "$SITE/tools/workspace.py"; then
        echo "FATAL: the lock is not effective for an unprivileged process"
        exit 1
    fi
fi

echo "=== 5. boot the kernel and run a plan, frozen ==="
# From a neutral directory: the repository must not be on ``sys.path``, or this would import the
# source tree and prove nothing about the installed wheel.
cd "$WORK"
ALETH_STATE_DIR="$WORK/state" ALETH_WORKSPACE_DIR="$WORK/ws" \
    $RUN "$VENV/bin/python" - "$SITE" <<'DRIVER'
import os, sys

site = os.path.realpath(sys.argv[1])
failures = []

app_names = ("agents", "core", "orchestration", "storage", "tools",
             "app.py", "bridge_bus.py", "env_boot.py", "main.py", "registry.py")
app_paths = [os.path.join(site, name) for name in app_names
             if os.path.exists(os.path.join(site, name))]
if len(app_paths) < len(app_names):
    failures.append(f"the wheel did not install every package: {app_paths}")


def snapshot():
    """Every file in the application's own tree, with its mtime."""
    state = {}
    for path in app_paths:
        if os.path.isfile(path):
            state[path] = os.stat(path).st_mtime_ns
            continue
        for dirpath, _dirnames, filenames in os.walk(path):
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    state[full] = os.stat(full).st_mtime_ns
                except OSError:
                    continue
    return state


# Before the imports, so the interpreter's own bytecode attempt is measured too. ``pip`` compiles
# .pyc files at install time, so *presence* proves nothing -- only a diff does.
before = snapshot()

# Boot: import the kernel from the frozen install.
import storage.db, tools.workspace, tools.mcp_exec_server  # noqa: E402
from tools import docker_sandbox  # noqa: E402

# The imports must have come from the wheel, not from a checkout that happens to be nearby.
for module in (storage.db, tools.workspace, tools.mcp_exec_server):
    origin = os.path.realpath(getattr(module, "__file__", ""))
    if not origin.startswith(site):
        failures.append(f"{module.__name__} was imported from {origin}, not the installed wheel")
print("imported from:", os.path.realpath(storage.db.__file__))

plan_dir = os.path.join(os.environ["ALETH_STATE_DIR"], "plan")
os.makedirs(plan_dir, exist_ok=True)
tools.workspace.PLAN_DIR = plan_dir
tools.workspace.ACTIVE_PLAN_FILE = "PLAN.md"
with open(os.path.join(plan_dir, "PLAN.md"), "w", encoding="utf-8") as handle:
    handle.write("# Immutable probe\n\n## 1. S\n- [ ] A dummy task\n")

storage.db.reset_stores()
store = storage.db.get_store()
print("store:", store.path)
if store.path.startswith(site):
    failures.append(f"the store was placed inside the application directory: {store.path}")

# A plan written to the store: the write path that used to land beside the source.
from storage.db import PlanDAG, TaskNode
store.save_dag(PlanDAG(
    plan_id="PLAN", title="Immutable probe", order=["task-1"],
    nodes={"task-1": TaskNode(id="task-1", plan_id="PLAN", title="A dummy task", status="pending")},
), sync_edges=True)
print("plan persisted; tasks:", len(store.get_dag("PLAN").nodes))

# And one execution, which is the other thing that touches disk.
root = os.environ["ALETH_WORKSPACE_DIR"]
os.makedirs(root, exist_ok=True)
if docker_sandbox.available():
    output = tools.mcp_exec_server.run_workspace_command("true", root=root)
    print("execution:", output.splitlines()[0])
    if not output.startswith("[Exit Code: 0]"):
        failures.append(f"the execution did not succeed: {output[:200]}")
else:
    # A refusal is the perimeter working, not a gap: an environment with no daemon must never run
    # the command on the host. Saying so is honest; claiming an execution happened would not be.
    print("execution: skipped (no Docker daemon reachable in this environment)")

# Nothing may have been added to or changed in the application's own tree.
app_names = ("agents", "core", "orchestration", "storage", "tools",
             "app.py", "bridge_bus.py", "env_boot.py", "main.py", "registry.py")
app_paths = [os.path.join(site, name) for name in app_names
             if os.path.exists(os.path.join(site, name))]
if len(app_paths) < len(app_names):
    failures.append(f"the wheel did not install every package: {app_paths}")

after = snapshot()
added = sorted(set(after) - set(before))
changed = sorted(p for p in set(after) & set(before) if after[p] != before[p])
if added:
    failures.append(f"the run created {len(added)} file(s) in the application: {added[:3]}")
if changed:
    failures.append(f"the run modified {len(changed)} file(s) in the application: {changed[:3]}")

if failures:
    for line in failures[:10]:
        print("FAIL:", line)
    raise SystemExit(1)
print(f"OK: the kernel booted and ran a plan from a read-only install "
      f"({len(after)} application files, none added, none changed)")
DRIVER

echo "=== 6. the lock held: nothing in the application tree is writable ==="
if find $APP -perm -u+w | grep -q .; then
    echo "FATAL: a file became writable again"
    exit 1
fi
echo "PASS: immutable distribution verified"
