# syntax=docker/dockerfile:1
#
# Phase 47: the single deployable artifact.
#
# The product is one process on one port. This image contains it whole: the built frontend, the
# Python engine, its compiled core, and the two host tools the engine shells out to (git for the
# shadow and the snapshots, the docker CLI for the execution perimeter). No Node.js, no pip, no
# npm, and no repository checkout are needed on the machine that runs it.
#
# Three stages, as directed: the UI is built by Node, the Python side is built by Python, and the
# runtime carries only the results -- no compiler, no Rust toolchain, no npm cache.
#
# Build:
#     docker build -t aleth .
#
# Run (the command is in README.md; the socket mount is not optional):
#     docker run --rm --network host --user "$(id -u):$(id -g)" \
#         -v /var/run/docker.sock:/var/run/docker.sock \
#         -v "$HOME/.aleth:$HOME/.aleth" -v "$PWD:$PWD" \
#         -e ALETH_STATE_DIR="$HOME/.aleth/state" -e ALETH_WORKSPACE_DIR="$PWD" \
#         aleth

# ---------------------------------------------------------------------------------------------
# Stage 1 -- the UI builder. Vite compiles ``ui/`` into ``dist/``; nothing here reaches runtime.
# ---------------------------------------------------------------------------------------------
FROM node:22-alpine AS ui

WORKDIR /build
# ``npm ci`` rather than ``npm install``: the lockfile is the contract, and a build that resolves
# its own versions is not reproducible.
COPY package.json package-lock.json vite.config.mjs ./
COPY ui ./ui
RUN npm ci && npm run build

# ---------------------------------------------------------------------------------------------
# Stage 2 -- the Python builder. A virtualenv with every dependency, plus the compiled core.
# ---------------------------------------------------------------------------------------------
FROM python:3.12-slim AS pybuild

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The Rust toolchain is needed to build ``crates/deepagents_core`` (maturin/pyo3), and
# ``build-essential`` for its linker. Both stay in this stage: the runtime gets a wheel, not a
# compiler.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /app
COPY pyproject.toml ./
COPY crates ./crates
COPY vendor ./vendor
COPY agents ./agents
COPY api ./api
COPY core ./core
COPY orchestration ./orchestration
COPY storage ./storage
COPY tools ./tools
COPY app.py bridge_bus.py cli.py env_boot.py main.py registry.py ./

# One virtualenv, built once: the compiled core first (it has no dependencies of its own), then the
# kernel with its declared dependencies. ``vendor/`` is present because ``pyproject.toml`` ships the
# tokenizer encoding as data.
#
# ``torch`` comes from PyTorch's CPU index first. The default PyPI wheel for Linux links CUDA and
# drags in several gigabytes of ``nvidia-*`` runtime libraries, and this engine has no use for them:
# the System 1 checkpoint and the semantic embedder both run on the CPU, and the GPU work -- if any
# -- happens inside a sandbox container on the host, not here. Installing the CPU wheel first means
# ``pip install .`` finds ``torch>=2.14`` already satisfied and does not pull the CUDA stack.
RUN python -m venv /opt/aleth \
    && /opt/aleth/bin/pip install --upgrade pip \
    && /opt/aleth/bin/pip install ./crates/deepagents_core \
    && /opt/aleth/bin/pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.14" \
    && /opt/aleth/bin/pip install .

# ---------------------------------------------------------------------------------------------
# Stage 3 -- the runtime. The venv, the built UI, and the two host tools the engine drives.
# ---------------------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PATH="/opt/aleth/bin:${PATH}"

# ``git`` and the ``docker`` CLI are not conveniences: the shadow workspace, the project identity
# and the Phase 35 snapshots shell out to git, and every agent command runs through the docker
# client against the daemon socket the operator mounts. The CLI comes from Docker's own repository
# so the daemon is not dragged in -- this container is a *client* of the host's engine.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends git docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# The application runs **from source**, exactly as a checkout does: ``frontend_root()``,
# ``MIGRATIONS_DIR`` and the vendored tokenizer all resolve relative to the code, so the tree has to
# be laid out the way the code expects. ``PYTHONPATH`` puts it ahead of the copy the install left in
# site-packages, so there is one authoritative set of modules.
WORKDIR /app
ENV PYTHONPATH=/app

COPY --from=pybuild /opt/aleth /opt/aleth
COPY --from=pybuild /app /app
COPY --from=ui /build/dist /app/dist

# The app's own port (``api.server.DEFAULT_PORT``). The UI and the API share it: the page is served
# from ``/`` and the typed surface from ``/api``, from one socket, so there is no second server.
EXPOSE 8765

# ``serve`` is the headless daemon: pre-flight, process lock, migrations, the maintenance sweeper,
# then the unified HTTP server -- in that order, before a single request is accepted.
ENTRYPOINT ["aleth", "serve"]
