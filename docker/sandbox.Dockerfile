# The sandbox image: the container every command the app did not author runs in.
#
# The orchestrator's own interpreter is NOT this image. This image is the *execution*
# environment for model-authored work, so it carries the toolchain a workspace's commands need:
# a Python interpreter, the test runner, and the utilities generated tests reach for. Without
# them a verification command would fail for a missing dependency rather than for a real fault.
#
# Build it once (the context is this directory -- the image copies nothing from the repo):
#
#     docker build -f docker/sandbox.Dockerfile -t aleth-sandbox:latest docker/
#
# `tools/docker_sandbox.py` defaults to the `aleth-sandbox:latest` tag and builds it from
# this file when it is absent. If the build cannot run, the perimeter **refuses** the command --
# it never executes on the host.

FROM python:3.12-slim

# Deterministic, quiet installs; no byte-code litter and no buffering in a bind-mounted
# workspace, and UTF-8 regardless of the host's locale.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

# The toolchains a workspace's own commands need. Node is here because a JavaScript project's
# setup and verification *are* `npm install` and `npm test` -- without it the two-phase execution
# this engine performs would be a no-op for half the projects it is pointed at, and an agent that
# cannot install its dependencies cannot test its own work (Phase 29).
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

# pytest is what `tools/test_runner.py` drives; pytest-xdist lets a workspace suite parallelise
# the way this repo's own suite does; pytest-timeout keeps a hung generated test from holding
# the verdict; the rest are the fundamental test utilities (fixtures, HTTP stubbing, clock
# control, an HTTP client) that generated suites most often import.
RUN pip install --no-cache-dir \
        pytest \
        pytest-xdist \
        pytest-timeout \
        pytest-mock \
        coverage \
        requests \
        responses \
        freezegun \
    && python -c "import pytest, xdist, coverage, responses, freezegun; print('sandbox toolchain ok:', pytest.__version__)"

# No fixed non-root user is created here on purpose. The container is launched with
# `--user <uid>:<gid>` mapped to the host caller, so payloads write through the bind mount with
# the caller's ownership; a user baked into the image would either be shadowed by `--user` or
# mangle the permissions of files it created.

# The workspace is bind-mounted here and is also the container's working directory, so a
# command's cwd is the workspace root.
WORKDIR /workspace

# The payload's shell. `docker run ... <image> /bin/sh -c <command>` is how the perimeter
# invokes a command; keeping a default entrypoint out of the way leaves that explicit.
CMD ["/bin/sh"]
