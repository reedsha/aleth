# MASTER CONTEXT — Ground Truth Architecture Manifest

## 1. System Identity & Core Rules

**Identity.** Aleth Studio — a local, single-machine AI IDE. A **micro-kernel** with a SQLite state kernel, a stateless Swarm worker dispatcher, and deterministic tier routing. The LLM plans; the engine executes what a human approved. No daemon, no server, no cloud state.

**Absolute constraints (violating one is a failed batch):**

1. **Zero native terminal fallbacks.** Execution is contained, not merely filtered. Every command the app did not author goes through `tools/docker_sandbox.py`: `tools/mcp_exec_server.py` (the model's shell) and `tools/test_runner.py` (pytest, which imports and executes workspace code). A container is launched with `--network=none`, `--user <uid>:<gid>`, the workspace as the only OCI bind mount and the container's cwd, CPU/memory/pids capped at a **bounded** default (512 MB, one CPU, 64 pids) that nothing can raise past a hard ceiling, `--cap-drop=ALL`, `no-new-privileges`. When the runtime cannot be reached the run is **refused**, never executed on the host: no host Python process may import or execute code inside the workspace. There is no unsandboxed fallback and no `shell=True` in any production module; the old in-process perimeter (`tools/sandbox.py`) is deleted.
2. **Pydantic schema strictness.** Every JSON boundary is a strict model (`extra="forbid"`). A field that is missing is an error, not a default. Softer-than-required defaults hide bugs.
3. **Process-isolated workers.** A Swarm worker is a plain picklable function crossing a `ProcessPoolExecutor` boundary. No live SQLite connection, no MCP session, no closure crosses it. Workers cannot emit UI events — the **parent's** `Future` callback does.
4. **No N+1 database queries.** Dispatch eligibility and every per-node input are decided in **one** SQL statement. Never a scalar `SELECT` inside a dispatch loop.
5. **SQL decides graph state.** `task_dependencies` is the single source of truth for edges. Eligibility is a `NOT EXISTS` query (`orchestration/scheduler.py::_EXECUTABLE_SQL`), never rebuilt in Python.
6. **One authority per decision.** The router picks the model and its endpoint. The worker applies them verbatim and never guesses. No hardcoded fallback model anywhere on the execution path.
7. **Code reads like the domain.** `max_retries` means *retries* (`rejection_attempts <= ?`). Do not bend a constant to mask an off-by-one in SQL.
8. **No dead fallbacks, no silent degradation.** A missing declaration fails loudly. A capability that cannot be resolved is an error, not an empty tool set.

---

## 2. Immutable Ground Truth (Current Codebase State)

**Status: Phases 5, 6, 6.5, 7, 7.5, 8, 8.1, 9, 10, 11, 12 and 13 complete and verified.**

**Test gate:** `python -m pytest` → **1014 passed, 17 skipped** on Windows (no daemon there, so the container-backed tests skip); **1026 passed, 5 skipped** on the Linux CI runner, where they actually run. `tools/verify_immutable_install.sh` passes: the wheel installs, the application tree is `chmod -R a-w`, and the kernel boots and runs a plan with nothing added or changed.
Run from the `aleth/` directory; `pytest.ini` has `addopts = -q -n auto` (16 xdist workers).
UI gate: `npm run lint` → depcruise clean (29 modules, 150 dependencies). Playwright tests exist under `ui/` and run against a built `dist/` — not exercised this phase.

**Database (`storage/db.py`, SQLite).** `tasks` carries, besides the DAG/projection columns: `complexity_score` (1–5), `rejection_attempts`, `rejection_feedback` (JSON), `system_failures`, `required_capabilities` (JSON). Added by guarded `_MIGRATIONS` (`PRAGMA table_info`), also present in the `CREATE TABLE` text. **`CREATE TABLE IF NOT EXISTS` never adds a column to an existing table** — new columns must go in `_MIGRATIONS`.

**Dependencies.** Locked in `pyproject.toml` (pydantic, openai, numpy, lancedb, pyarrow, torch, transformers, filelock, `deepagents`, laya, langchain-core, langchain-openai, astchunk, python-dotenv, pywebview, PyYAML; `test` extra: pytest, pytest-xdist). `deepagents_core` is the in-repo editable Rust crate (`crates/deepagents_core`, `pip install -e crates/deepagents_core`) — not on PyPI. Two names keep the `deepagents` spelling after the rebrand, and both are load-bearing: the `deepagents` PyPI framework (`from deepagents import create_deep_agent`) and the `deepagents_core` crate.

**Bootloader (`core/config.py`) — Phase 6.5.** Strict `TierConfig`/`BootloaderConfig` (tier_0 may omit `api_key`; tier_1/tier_2 must declare one). `boot_or_exit()` is called by `main.py` **before** anything imports `registry`/`app` (both open the store on import). It probes every configured tier with `GET {base_url}/models` and `sys.exit(1)` on a timeout, 401 or 404. `default_config()` derives the fleet from `agents/model_routing` + `OPENAI_*`, so the router has exactly one source. `aleth.config.example.json` is a valid, copyable example.

**DAG creation — nodes are born armed.** `required_capabilities` is a **birth** field, not something the worker discovers. `merge_dag_fields` enforces a strict **union-or-refuse strategy** on replans to prevent capability starvation.
Verified: the Rust canonicaliser (`deepagents_core.prepare_plan_state`) preserves the key. **Zero starvation pass: tick one is equipped.**

**Router (`orchestration/model_router.py`) — Phase 7 infrastructure.**
`route(complexity, rejections)` is pure and abstract. `resolve_endpoint(routed, config=None, *, refuse=True)` applies the fallback matrix: exact tier, else CRITICAL + strongest configured tier **at or below**; but tier_2 required with only tier_0 configured → `TaskUnroutable`. `Swarm.tick()` catches that and fails the node with the reason instead of dispatching it.

**Transport — the tier's endpoint is used.** `resolve_endpoint` routes to `execute_node` → `plan_task(base_url=, api_key=)` → `_from_llm` → `system2.complete` / `complete_with_tools`, which build the client from the **tier's** endpoint. A keyless local tier gets `LOCAL_ENDPOINT_KEY`.

**Swarm (`orchestration/swarm.py`) — reactive, no generator, no polling.**
`tick()` dispatches runnable nodes and `Future.add_done_callback` delivers completions on the parent thread. Rejection state lives in SQLite, never in the Swarm object. Failure segregation: `is_system_failure()` → counted in `system_failures`, node stays `pending` and is auto-retried up to `max_system_retries` (3). UI event emission is strictly isolated to the orchestration layer.

**Context diet (`orchestration/mcp_session.py`) — Phase 7.4.**
`MCPSessionContext(root, capabilities=…)` scopes servers spawned **and** tools bound. `fs`→read_file/create_file, `exec`→execute_command/execute_restricted_command, `ast`→list_symbols/edit_ast_node/append_to_file. `[]` → **no servers, no tools**. 

**Semantic lock (`tools/semantic_index.py`) — Phase 8.1 (done early).**
`MiniLmEmbedder._ensure_loaded` takes a per-model `filelock.FileLock` around the weights load, avoiding race conditions in the Hugging Face cache.

**Execution perimeter (`tools/docker_sandbox.py`) — Phase 8.** `tools/mcp_exec_server.py::run_workspace_command` and `tools/test_runner.py` run every command in a container: `--network=none`; `--user <uid>:<gid>` maps the **host** identity (`ALETH_SANDBOX_UID`/`_GID` override it where there is no POSIX uid); the workspace is the **only** host path, an OCI bind mount that is also the container's working directory; `--memory`/`--cpus`/`--pids-limit` cap resources; `--cap-drop=ALL` + `no-new-privileges`; the container is named and `--rm`, force-removed on timeout. Nothing is passed with `-e`/`--env-file`. The image is **`aleth-sandbox:latest`**, built from `docker/sandbox.Dockerfile` (Python 3.12 + pytest and the fundamental test utilities) and built once on first use when absent. An unreachable daemon or an image that cannot be provided raises `SandboxError` → a refusal, never a host run. `tools/sandbox.py` is deleted.

**The network boundary — Phase 12.** `--network=none` is the **default** for every container, and the only way to widen it is the `net` capability, declared by the plan: `docker_sandbox.build_command(allow_network=…)` → `--network=bridge`, set from `MCPSessionContext._allow_network()` into the exec server's **own argv** (`--allow-network`). It is deliberately not a tool argument — a model that could pass `network=True` would have no boundary — and the engine's own session never declares it. `tests/test_chaos.py::NetworkIsolationTests` proves both halves against a live daemon: a default container's outbound connect fails with a network error, and one with the capability connects.

**Resource bounding — Phase 13.** Every container runs under a **bounded** hardware budget: `--memory`/`--memory-swap` at 512 MB, `--cpus` at 1.0 and `--pids-limit` at 64 by default. The bound is enforced by construction, not convention -- `docker_sandbox.build_command` clamps `memory_mb`/`cpus` to `MAX_MEMORY_MB` (8192) and `MAX_CPUS` (4.0) itself, so no caller can ask for an unbounded container. The only way to widen it is the `heavy` capability (4096 MB / 2.0 CPU), declared by the plan and threaded to the exec server's own argv (`--resource-profile`) exactly like `net` -- never a tool argument, so a model cannot buy itself memory. A container the kernel's OOM killer ends returns 137; `run_isolated` marks it `oom_killed` and `mcp_exec_server` writes `outcome = 'OOM_KILLED'` into `execution_telemetry` (the ledger gained an `outcome` column via a guarded `ALTER`), so the engine can tell a memory leak from a test failure instead of re-queuing the node. `tests/test_chaos.py::OomResilienceTests` proves it against a live daemon: a 64 MB container running a 1 GB allocation dies in seconds, the host survives, and the ledger records 137/`OOM_KILLED`.

**The orchestrator runs where the daemon runs.** The perimeter reaches the daemon through the `docker` client and treats the workspace path as a path *in the daemon's filesystem*; both are only true in one namespace, one filesystem and one signal space. So `docker_bin()` raises `EnvironmentError` when the client is not on `PATH` (surfaced as a `SandboxError` refusal at the tool boundary), and **nothing** translates paths or proxies commands across an OS boundary — no WSL bridge, no `wslpath`, no `\\wsl$` rewriting. Proxying `docker run` through another OS's interpreter would break the process-tree guarantee as well: killing the proxy leaves the container orphaned and unreachable by the timeout path. A workspace on a Windows drive (9p DrvFs) is **not** usable — a container writing through one creates files with no Windows ACL (mode 0000) — so when the daemon lives on Linux the orchestrator and its workspace live there too.

**Workspace location — the native cutover.** `tools/workspace.py` resolves `PROJECT_DIR` from `ALETH_WORKSPACE_DIR`, defaulting to `~/workspaces/my_project` (expanded, made absolute, and resolved against the repo root when relative). The workspace is a path *in the daemon's filesystem* — a container bind-mounts it — so it cannot be a directory beside the repository.

**The plan pair is not a pair — Phase 11 state segregation.** `PLAN.md` stays in the user's repository: it is human-authored and git-tracked, and it belongs where it can be reviewed, diffed and reverted. The **machine state** does not: `aleth_state.db` lives in `~/.aleth/state/<project_id>/`, where `project_id` is `sha256` of the absolute plan directory (truncated to 16 hex chars) — so state follows the *location* of a project rather than a name two projects could share, and no binary file lands in the user's working tree. `plan.json` is legacy: read once for import, then removed, never written. `ALETH_STATE_DIR` overrides the state root (the suite points it at a throwaway directory).

**Immutable distribution — Phase 11.** `python -m build` produces `aleth-0.1.0-py3-none-any.whl` with the five packages and five top-level modules and **no** tests, bytecode or state files. `tools/verify_immutable_install.sh` proves the consequence rather than asserting it: it builds the wheel, installs it into an isolated venv, strips every write bit from the installed application, then boots the kernel and runs a plan — asserting the imports came from site-packages (not a nearby checkout), the store went to the state directory, and **no application file was added or changed**. A `chmod` is the only acceptable proof; an invariant assertion is not a substitute for the filesystem refusing a `write()`.

The native environment is `~/aleth-venv` (Python 3.12) plus `deepagents_core` built for Linux; the Windows venv still runs the suite where no daemon is needed.

**Declared dependency.** `pyproject.toml` declares `langchain-openai`: `create_deep_agent` constructs `ChatOpenAI` in every agent factory, so a clean environment cannot build an agent without it. It was previously satisfied only transitively, which a fresh Linux install exposed.

**Skill registry (`storage/db.py`, `orchestration/retriever.py`, `orchestration/system2.py`) — Phase 7.5.** A `skills` table (`id`, `name`, `target_capabilities` JSON, `markdown_content`), seeded idempotently with `TDD_Execution_Skill` for `["exec", "fs"]`. `retriever.skills_for_capabilities` matches a node's declaration with a **single** JSON1 set-intersection query (no N+1), and `system2.compose_system_prompt` prepends the block to the system prompt before the client payload is built. The planner resolves the block once per pass (`plan_task(capabilities=…)`) and passes it as text — the transport never looks a skill up.

**Engine verification gate (`orchestration/swarm.py`, `orchestration/workflow/execution.py`) — Phase 7.5.** `verification_refusal(verdict, required_capabilities)` is the policy: a node that required `exec` must carry a `tools/test_runner.py` receipt whose exit code is 0. No receipt, a failing receipt, or an uncollectable suite is a **refusal**, and the engine **intercepts the transition**: the node is re-queued (status `pending`, `rejection_attempts` incremented, so the retry is bounded by `max_retries` like a human's) with the message `Task unverified: Tests must be executed via test_runner and return exit_code 0`. It is never marked `completed` on the absence of evidence. A valid completion writes `tasks.verified = 1` in the same statement as the status (`update_task_status(..., verified=…)`).

**Swarm lifecycle (`orchestration/swarm.py`) — teardown hardening.** `drain(timeout)` waits for dispatched work to settle; `shutdown(timeout)` drains then closes; `BridgeAPI.shutdown_swarm()` is the app-side handle and the characterization harness drains it before unlinking a temporary workspace. A `Future` callback (`_on_worker_complete`) never raises — it reports to stderr, because nothing above a callback on the pool's thread can catch it.

**Swarm pool start method.** The pool does **not** inherit the platform default: `Swarm.tick()` builds its `ProcessPoolExecutor` with `mp_context=spawn`. `fork` (the POSIX default) copies the parent's native threads — lancedb's background loop, torch's pools — into the child as locks with no threads to release them, and a worker segfaulted on exactly that mid-suite. `forkserver` is thread-safe but its workers fork from a long-lived server whose environment is frozen when it starts, so a value set after the pool came up never reaches the child. `spawn` execs a fresh interpreter with the parent's *current* environment, which is the one that is both safe and correct.

**Planning-pass role binding — Phase 7 tail.** `planner.plan_task(role=…)` threads the node's role into `run_tool_loop`, and `worker.execute_node` passes its descriptor's role, so the planning pass asks the live `MCPSessionContext` for **that** role's tools instead of a constant. No import-time catalog exists anywhere; `tests/test_security_boundaries.py::NoImportTimeToolCatalogTests` pins the `@tool` catalogs and the native-shell shape at zero.

**Knowledge graph — Phase 5.** `kernel://` URIs, entities/synapses with FK cascade, bidirectional depth-bounded recursive-CTE traversal, LanceDB vectors + outbox sync, hybrid retriever.

**Known accepted debt.**
(a) `agents/model_routing.py` remains the env seam the fleet is derived from; the UI settings panel still writes those variables.
(b) The sandbox image defaults to `aleth-sandbox:latest` and must carry whatever toolchain the workspace's commands need — the repo's `docker/sandbox.Dockerfile` supplies Python 3.12, pytest and the fundamental test utilities (`ALETH_SANDBOX_IMAGE` overrides it). A missing runner yields the `unavailable` verdict, never a host run.
(c) The orchestrator must run in the same namespace as the Docker daemon: the client must be on `PATH` (a missing client is a fatal `EnvironmentError`, surfaced as a refusal), and the workspace must live on the daemon's filesystem (`ALETH_WORKSPACE_DIR`). A workspace on a Windows drive is not usable (9p DrvFs writes land with no ACL). Nothing bridges the two by design.
(d) The Windows-side suite cannot exercise the daemon-backed tests (there is no docker client there), so they skip; the native WSL run is the one that proves containment.

---

## 3. Finalized Master Phase Plan (The Roadmap)

Established and **done**:

| Phase | Scope | State |
|---|---|---|
| 5 | Knowledge graph: `kernel://` URIs, entities/synapses, recursive-CTE traversal, LanceDB hybrid retrieval | **Complete** |
| 6 | Swarm orchestration: static agent dicts annihilated, SQL eligibility, `ProcessPoolExecutor` dispatch, reactive `tick()`, human rejection loop | **Complete** |
| 6.5 | Deterministic bootloader: strict tier config, fail-fast endpoint proof, router fallback matrix, endpoint threaded | **Complete** |
| 7 | Cognitive routing + context diet: complexity/rejection routing, capability-scoped tool binding, dynamic tool binding in the swarm worker | **Complete** |
| 7.5 | Skill injection engine: `skills` table, single-query capability match, system-prompt injection, engine verification gate (`verified`) | **Complete** |
| 8 | Absolute sandbox isolation: Docker-only exec, OCI bind mount, `--user` mapping, no native fallback, `tools/sandbox.py` deleted | **Complete** |
| 9 | Failure segregation: `system_failures` split from `rejection_attempts`, auto-retry for machine faults | **Complete** |
| 8.1 | Cross-process lock around embedder weight loading | **Complete** |
| 10 | Chaos engineering & IPC brutality: bounded stream drain, process-group purge, env sanitization | **Complete** |
| 11 | Telemetry & immutable deployment: forensic ledger, plan-pair split, read-only wheel proof | **Complete** |
| 12 | Network egress & container hardening: `--network=none` default, plan-declared `net` capability | **Complete** |
| 13 | Resource bounding & OOM protection: 512 MB/1 CPU default, bounded profiles, `OOM_KILLED` telemetry | **Complete** |

#### Phase 7.5: Skill Injection Engine & Deterministic Test Verification Gate
*The Playbook and the Gatekeeper. Naked JSON schemas cause hallucinations; unverified text completions cause state corruption.*

1. **Schema Migration (`tasks.verified` & `skills` Table):**
   * **Mandate:** Update `storage/db.py` via guarded `_MIGRATIONS`:
     * Add `verified` (INTEGER DEFAULT 0) to `tasks`.
     * Create `skills` table: `id` (TEXT PK), `name` (TEXT), `target_capabilities` (JSON array), `markdown_content` (TEXT).
   * **Rule:** Never hardcode prompt strings or static catalogs in Python files.

2. **Hardcoded Engine Verification Gate (`orchestration/swarm.py`):**
   * **Mandate:** A node with code execution requirements (`exec` capability) **CANNOT** transition to `COMPLETED` based on LLM output alone.
   * **Rule:** Transitioning a task node to `COMPLETED` strictly requires:
     1. A valid tool receipt generated by `tools/test_runner.py` inside `tools/docker_sandbox.py` during that task's execution cycle.
     2. The tool receipt must explicitly confirm `exit_code == 0`.
     3. Setting `tasks.verified = 1` in the database upon state transition.
   * **Failure Handling:** If the LLM attempts to emit a completion signal without a valid `exit_code == 0` test receipt, the Swarm engine intercepts the transition, appends a rejection message ("Task unverified: Tests must be executed via test_runner and return exit_code 0"), increments `rejection_attempts`, and re-queues[cite: 4].

3. **Contextual Skill Retrieval & System Prompt Injection:**
   * **Mandate:** During Swarm node dispatch, `orchestration/retriever.py` executes **one** SQL query to fetch active Skills where `target_capabilities` intersects with the node's `required_capabilities`[cite: 4].
   * **Rule:** Inject seed skill `TDD_Execution_Skill` into the database:
     * `target_capabilities`: `["exec", "fs"]`
     * `markdown_content`: Declarative TDD standard operating procedure forcing test creation, sandboxed execution via `test_runner`, stack trace inspection, and iterative repair.
   * **Transport Ingestion:** Concatenate fetched Skill content and inject directly into the LLM's System Prompt before `system2.py` constructs the client client payload[cite: 4].

#### Phase 10: Chaos Engineering & IPC Brutality — sealed
*The Crucible. Proven: the system survives reality under load and unexpected process termination.*

* **Bounded streams.** `tools/stream_drain.py` keeps a head/tail window (1 MB) while draining both pipes as bytes arrive, so a child that floods stdio cannot block on a full pipe and the parent's memory stays bounded. `tests/test_chaos.py::PipeStufferTests` floods 20 MB through it.
* **Process groups.** Every child is spawned with `start_new_session=True`; a timeout kills the whole group (`os.killpg`) and reaps it. `ZombieEradicationTests` leaves zero survivors.
* **Environment.** `tools/env_sanitizer.py` builds the child's environment from an allow-list, so `.env` credentials never reach a payload; `test_environment_isolation` asserts `OPENAI_API_KEY` is undefined inside the execution context.
* **Descriptors.** `test_file_descriptor_leakage` proves 100 executions add no file descriptors.

#### Phase 11: Telemetry & Immutable Deployment — sealed
*The Finish Line. Complete visibility and zero environment drift.*

* **The forensic ledger.** `storage/telemetry.py` owns `execution_telemetry` (raw, indexed SQL): one append-only row per container execution -- `execution_id` (the container's own label), `session_id`, `target_tool`, `exit_code`, `duration_ms`, `outcome`, and a SHA-256 of each **full** stream, hashed as the bytes arrive (before truncation). Written by the exec server itself; a receipt that cannot be written is a critical fault, surfaced in the tool result and on stderr.
* **State segregation.** `PLAN.md` stays in the user's repository; `aleth_state.db` moves to `~/.aleth/state/<project_id>/`, with the identity resolved from `.aleth_id` → the canonicalised git remote → a minted UUID.
* **Immutability, proven.** `tools/verify_immutable_install.sh` builds the wheel, installs it into an isolated venv, strips every write bit, and boots the kernel as an unprivileged user -- the OS refusing a `write()` is the proof, not an assertion. Wired into CI as the `test-immutable` job.

#### Phase 13: Resource Bounding & OOM-Killer Protection — active
*The Cgroup. A container is isolated in software; without a hardware budget it can still take the host down.*

1. **Hard quotas.** Every `docker run` carries `--memory`/`--memory-swap` 512 MB, `--cpus` 1.0 and `--pids-limit` 64 by default. `build_command` clamps to `MAX_MEMORY_MB`/`MAX_CPUS` itself, so no caller can exceed the ceiling.
2. **Capability override, never unbounded.** The `heavy` capability (4096 MB / 2.0 CPU) is the only way to widen the budget; it is declared by the plan and threaded to the exec server's argv (`--resource-profile`), never a tool argument.
3. **OOM telemetry.** Exit 137 is marked `oom_killed` by `run_isolated` and recorded as `outcome = 'OOM_KILLED'` in the ledger, so a memory-leaking command is not retried as a transient fault.

---

## 4. The Active Immediate Execution Order

> **Phases 5 through 12 are sealed and verified in the native Linux and WSL environments. The active objective is Phase 13 — Resource Bounding & OOM-Killer Protection.**

**Phase 8 — Absolute Sandbox Isolation — sealed.** The exec server runs each command in a container (`tools/docker_sandbox.py`): the workspace is the only OCI bind mount and the container's working directory; `--network=none`; `--user <uid>:<gid>` maps the host identity so local file permissions are not mangled; CPU/memory/pids are capped; `--cap-drop=ALL` + `no-new-privileges`; and the process tree is owned by the named, `--rm` container, force-removed on timeout. The exec server's cwd is forced to the resolved workspace root, path traversal is refused before anything runs, and `execute_restricted_command` keeps the Architect's allow-list.
* **No native terminal fallback remains.** An unreachable daemon or a missing `docker` client is a **refusal** (`SandboxError`), never a host run. `tools/sandbox.py` is deleted.

**Phase 7 tail — dynamic tool binding in the swarm worker — closed.** The worker's planning pass asks the live `MCPSessionContext` for its **node's** role (`plan_task(role=…)` → `run_tool_loop`), and the agent factories bind from the same live `tools/list`. There is no import-time catalog anywhere; the legacy `@tool` catalogs are gone from disk and pinned at zero.

**Phase 7.5 — Skill Injection Engine — sealed.** The `skills` table is seeded with `TDD_Execution_Skill`; `retriever.skills_for_capabilities` selects a node's playbooks in one query; `system2.compose_system_prompt` prepends them before the payload is built; and the engine gate refuses to mark a task that required `exec` as `completed` without a zero-exit test receipt — it re-queues the node with `rejection_attempts` incremented and writes `verified = 1` only when it accepts.

**Phase 10 — Chaos Engineering & IPC Brutality — sealed.** Bounded stream draining (1 MB head/tail window, drained as bytes arrive), process-group purge on timeout (`os.killpg` + reap), an allow-list child environment (`tools/env_sanitizer.py`), and label-based container sweeps at boot and exit. `tests/test_chaos.py` proves each against a hostile child; the daemon-backed lifecycle tests pass natively in WSL.

**Phase 11 — Telemetry & Immutable Deployment — sealed.** The append-only `execution_telemetry` ledger (raw SQL; SHA-256 of the full streams, taken on the fly), the plan-pair split (`PLAN.md` in the repository, machine state under `~/.aleth/state/<project_id>/`), and a read-only distribution proven by `tools/verify_immutable_install.sh` under a real `chmod -R a-w` in an unprivileged context. The `test-immutable` CI job is green.

**Phase 12 — Network Egress & Container Hardening — sealed.** `--network=none` is the default for every container, and the only widening is the plan-declared `net` capability, threaded to the exec server's own argv. `tests/test_chaos.py::NetworkIsolationTests` proves both halves against a live daemon.

**Phase 13 — Resource Bounding & OOM-Killer Protection — active.** Hard quotas (512 MB / 1.0 CPU / 64 pids), a bounded `heavy` profile as the only widening, and `OOM_KILLED` written to the ledger on exit 137.

**Definition of done through Phase 13 — met.** `python -m pytest` → **1019 passed, 18 skipped, 232 subtests** on Windows; **1026 passed, 5 skipped** on the Linux CI runner, where the daemon-backed tests run; the container suites (isolation, chaos, telemetry, OOM) pass natively in WSL, including the live `OOM_KILLED` proof and the `--user` ownership proof. `npm run lint` clean (29 modules, 150 dependencies, 0 violations); the legacy-catalog grep at zero (pinned by `tests/test_security_boundaries.py::NoImportTimeToolCatalogTests`); no native-execution path (no `shell=True` in any production module; `tools/sandbox.py` deleted and `tools/test_runner.py` containerized).

#### Phase 7.5: Skill Injection Engine & Deterministic Test Verification Gate
*The Playbook and the Gatekeeper. Naked JSON schemas cause hallucinations; unverified text completions cause state corruption.*

1. **Schema Migration (`tasks.verified` & `skills` Table):**
   * **Mandate:** Update `storage/db.py` via guarded `_MIGRATIONS`:
     * Add `verified` (INTEGER DEFAULT 0) to `tasks`.
     * Create `skills` table: `id` (TEXT PK), `name` (TEXT), `target_capabilities` (JSON array), `markdown_content` (TEXT).
   * **Rule:** Never hardcode prompt strings or static catalogs in Python files.

2. **Hardcoded Engine Verification Gate (`orchestration/swarm.py`):**
   * **Mandate:** A node with code execution requirements (`exec` capability) **CANNOT** transition to `COMPLETED` based on LLM output alone.
   * **Rule:** Transitioning a task node to `COMPLETED` strictly requires:
     1. A valid tool receipt generated by `tools/test_runner.py` inside `tools/docker_sandbox.py` during that task's execution cycle.
     2. The tool receipt must explicitly confirm `exit_code == 0`.
     3. Setting `tasks.verified = 1` in the database upon state transition.
   * **Failure Handling:** If the LLM attempts to emit a completion signal without a valid `exit_code == 0` test receipt, the Swarm engine intercepts the transition, appends a rejection message ("Task unverified: Tests must be executed via test_runner and return exit_code 0"), increments `rejection_attempts`, and re-queues[cite: 4].

3. **Contextual Skill Retrieval & System Prompt Injection:**
   * **Mandate:** During Swarm node dispatch, `orchestration/retriever.py` executes **one** SQL query to fetch active Skills where `target_capabilities` intersects with the node's `required_capabilities`[cite: 4].
   * **Rule:** Inject seed skill `TDD_Execution_Skill` into the database:
     * `target_capabilities`: `["exec", "fs"]`
     * `markdown_content`: Declarative TDD standard operating procedure forcing test creation, sandboxed execution via `test_runner`, stack trace inspection, and iterative repair.
   * **Transport Ingestion:** Concatenate fetched Skill content and inject directly into the LLM's System Prompt before `system2.py` constructs the client client payload[cite: 4].

---

#### Phase 7.5: Skill Injection Engine (Cognitive Scaffolding)
*The Playbook. Naked JSON schemas cause hallucinations; models need explicit procedural guidance.*

1. **SQLite Skill Registry:**
   * **Mandate:** Introduce a `skills` table via guarded `_MIGRATIONS`[cite: 3]. Schema: `id` (TEXT PK), `name` (TEXT), `target_capabilities` (JSON array), and `markdown_content` (TEXT). Never hardcode prompt strings or static catalogs in Python files[cite: 3].
2. **Contextual Retrieval:**
   * **Mandate:** During Swarm node dispatch, execute exactly **one** SQL query to fetch active Skills where `target_capabilities` intersects with the node's `required_capabilities`[cite: 3]. Zero N+1 database query violations allowed[cite: 3].
3. **Prompt Injection:**
   * **Mandate:** Concatenate the retrieved `markdown_content` and inject it directly into the LLM's System Prompt before the transport layer builds the client[cite: 3]. The model must read the Skill as a declarative standard operating procedure dictating *how* and *when* to use the dynamically bound MCP tools.

**Definition of done — met.** Phase 7.5 is sealed: the `skills` table is seeded, the single-query retriever feeds `compose_system_prompt`, and the engine gate refuses an unverified `exec` completion. See §4 for the sealed ledger through Phase 13.
