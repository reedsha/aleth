# Aleth

A local, single-machine AI execution engine: it plans a DAG, runs each node's code inside a
disposable container, and only ever writes to a **shadow copy** of your repository — your working
tree changes when you approve a merge, and not before.

The product is **one process on one port**. The built frontend and the typed API are served from the
same socket: the page is at `/`, the API at `/api`. There is no separate frontend server, no Node.js
at runtime, and nothing to `pip install` on the machine that runs it.

---

## Run it

The engine drives containers, so it needs the host's Docker daemon — the socket mount is **not
optional**, and neither is the state mount (the shadow workspaces live there and the host's daemon
has to be able to see them at the same paths).

```sh
docker build -t aleth .

docker run --rm --network host \
    --user "$(id -u):$(id -g)" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$HOME/.aleth:$HOME/.aleth" \
    -v "$PWD:$PWD" \
    -e ALETH_STATE_DIR="$HOME/.aleth/state" \
    -e ALETH_WORKSPACE_DIR="$PWD" \
    aleth
```

Then open <http://127.0.0.1:8765/>.

### Why those flags

| Flag | Why it is there |
| --- | --- |
| `-v /var/run/docker.sock:...` | Every command the agent runs goes through a container. Without the socket the engine has no execution perimeter and **refuses to run anything** — it never falls back to the host. |
| `--network host` | The engine's security boundary is *loopback only*: it binds `127.0.0.1` and refuses a request whose peer is not loopback. `--network host` gives the container the host's loopback, so the boundary is unchanged and `127.0.0.1:8765` is reachable. Publishing a port with `-p` instead would make every request arrive from the bridge gateway and be refused. |
| `--user "$(id -u):$(id -g)"` | The agent's shadow containers run as the engine's uid, mapped onto the host. Running as the host user is what keeps files the agent writes owned by **you** rather than by root. |
| `-v "$HOME/.aleth:..."` + `ALETH_STATE_DIR` | State (the SQLite ledger, the staging area, the logs) lives on the host, at the same path inside the container, so the host daemon can bind-mount a shadow workspace into a sandbox container. |
| `-v "$PWD:$PWD"` + `ALETH_WORKSPACE_DIR` | The repository the agent works on. It is **read**, never written: execution happens in a shadow copy. |

The sandbox image (`aleth-sandbox:latest`) is built on the host daemon on first use, from
`docker/sandbox.Dockerfile`.

## Configuration

Everything has a working default. The knobs that matter:

| Variable | Default | What it does |
| --- | --- | --- |
| `ALETH_STATE_DIR` | `~/.aleth/state` | Where the per-project state lives (ledger, staging, logs). |
| `ALETH_WORKSPACE_DIR` | `~/workspaces/my_project` | The repository the engine reads. |
| `ALETH_API_PORT` | `8765` | The port the UI and the API share. |
| `ALETH_SYSTEM_ONE_MODEL` | `english` | The System 1 (Laya) checkpoint, read at runtime. |
| `ALETH_ARCHITECT_MODEL`, `ALETH_CODER_DEEP_MODEL`, `ALETH_CODER_STANDARD_MODEL` | see `agents/model_routing.py` | The System 2 models the planner and the Coders run on. |
| `LAYA_BACKEND` | `heuristic` | `model` enables the System 1 checkpoint; unset uses the word list. |
| `ALETH_RETENTION_DAYS` | `30` | How long a finished run's state is kept before the sweeper prunes it. |

Credentials live in the OS keyring (`aleth keys set <provider>`), never in a file. In a container
with no keyring backend, export them instead — the pre-flight says which.

## What happens on boot

In order, before a single request is accepted:

1. **Pre-flight** — the container runtime, `git`, the process lock and the port are checked, and a
   failure aborts with a fix for each one. No partial execution.
2. **Schema migration** — the SQLite ledger is brought to the current version, each patch in one
   transaction. A failure refuses the boot rather than serving a database of unknown shape.
3. **Maintenance sweeper** — a background thread that prunes expired runs and reclaims the file.
4. **The unified HTTP server** — the UI at `/`, the typed API at `/api`, on `127.0.0.1`.

## Running from a checkout (development)

```sh
python -m pip install -e crates/deepagents_core      # the compiled core (Rust)
python -m pip install -e ".[test]"                   # the engine
npm ci && npm run build                              # the frontend -> dist/
aleth serve                                          # headless, or:
aleth boot                                           # the desktop window
```

`aleth serve` is the same daemon the image runs; `aleth boot` adds the desktop window.
