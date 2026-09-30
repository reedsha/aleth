# MASTER CONTEXT — Ground Truth Architecture Manifest

## 1. System Identity & Core Rules

**Identity.** DeepAgents Studio — a local, single-machine AI IDE. A **micro-kernel** with a SQLite state kernel, a stateless Swarm worker dispatcher, and deterministic tier routing. The LLM plans; the engine executes what a human approved. No daemon, no server, no cloud state.

**Absolute constraints (violating one is a failed batch):**

1. **Zero native terminal fallbacks.** Execution is contained, not merely filtered. Every command the app did not author goes through `tools/docker_sandbox.py`: `tools/mcp_exec_server.py` (the model's shell) and `tools/test_runner.py` (pytest, which imports and executes workspace code). A container is launched with `--network=none`, `--user <uid>:<gid>`, the workspace as the only OCI bind mount and the container's cwd, capped CPU/memory/pids, `--cap-drop=ALL`, `no-new-privileges`. When the runtime cannot be reached the run is **refused**, never executed on the host: no host Python process may import or execute code inside the workspace. There is no unsandboxed fallback and no `shell=True` in any production module; the old in-process perimeter (`tools/sandbox.py`) is deleted.
2. **Pydantic schema strictness.** Every JSON boundary is a strict model (`extra="forbid"`). A field that is missing is an error, not a default. Softer-than-required defaults hide bugs.
3. **Process-isolated workers.** A Swarm worker is a plain picklable function crossing a `ProcessPoolExecutor` boundary. No live SQLite connection, no MCP session, no closure crosses it. Workers cannot emit UI events — the **parent's** `Future` callback does.
4. **No N+1 database queries.** Dispatch eligibility and every per-node input are decided in **one** SQL statement. Never a scalar `SELECT` inside a dispatch loop.
5. **SQL decides graph state.** `task_dependencies` is the single source of truth for edges. Eligibility is a `NOT EXISTS` query (`orchestration/scheduler.py::_EXECUTABLE_SQL`), never rebuilt in Python.
6. **One authority per decision.** The router picks the model and its endpoint. The worker applies them verbatim and never guesses. No hardcoded fallback model anywhere on the execution path.
7. **Code reads like the domain.** `max_retries` means *retries* (`rejection_attempts <= ?`). Do not bend a constant to mask an off-by-one in SQL.
8. **No dead fallbacks, no silent degradation.** A missing declaration fails loudly. A capability that cannot be resolved is an error, not an empty tool set.

---

## 2. Immutable Ground Truth (Current Codebase State)

**Status: Phases 5, 6, 6.5, 7, 7.5, 8, 8.1 and 9 complete and verified.**

**Test gate:** `python -m pytest` → **975 passed, 7 skipped, 218 subtests** on Windows; **1200 items, 0 failures, 0 errors, 5 skipped** natively in WSL (`~/deepagents-venv`), where the five daemon-backed isolation tests actually run.
Run from the `deepagents/` directory; `pytest.ini` has `addopts = -q -n auto` (16 xdist workers).
UI gate: `npm run lint` → depcruise clean (29 modules, 150 dependencies). Playwright tests exist under `ui/` and run against a built `dist/` — not exercised this phase.

**Database (`storage/db.py`, SQLite).** `tasks` carries, besides the DAG/projection columns: `complexity_score` (1–5), `rejection_attempts`, `rejection_feedback` (JSON), `system_failures`, `required_capabilities` (JSON). Added by guarded `_MIGRATIONS` (`PRAGMA table_info`), also present in the `CREATE TABLE` text. **`CREATE TABLE IF NOT EXISTS` never adds a column to an existing table** — new columns must go in `_MIGRATIONS`.

**Dependencies.** Locked in `pyproject.toml` (pydantic, openai, numpy, lancedb, pyarrow, torch, transformers, filelock, deepagents, laya, langchain-core, astchunk, python-dotenv, pywebview, PyYAML; `test` extra: pytest, pytest-xdist). `deepagents_core` is the in-repo editable Rust crate (`crates/deepagents_core`, `pip install -e crates/deepagents_core`) — not on PyPI.

**Bootloader (`core/config.py`) — Phase 6.5.** Strict `TierConfig`/`BootloaderConfig` (tier_0 may omit `api_key`; tier_1/tier_2 must declare one). `boot_or_exit()` is called by `main.py` **before** anything imports `registry`/`app` (both open the store on import). It probes every configured tier with `GET {base_url}/models` and `sys.exit(1)` on a timeout, 401 or 404. `default_config()` derives the fleet from `agents/model_routing` + `OPENAI_*`, so the router has exactly one source. `deepagents.config.example.json` is a valid, copyable example.

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

**Execution perimeter (`tools/docker_sandbox.py`) — Phase 8.** `tools/mcp_exec_server.py::run_workspace_command` and `tools/test_runner.py` run every command in a container: `--network=none`; `--user <uid>:<gid>` maps the **host** identity (`DEEPAGENTS_SANDBOX_UID`/`_GID` override it where there is no POSIX uid); the workspace is the **only** host path, an OCI bind mount that is also the container's working directory; `--memory`/`--cpus`/`--pids-limit` cap resources; `--cap-drop=ALL` + `no-new-privileges`; the container is named and `--rm`, force-removed on timeout. Nothing is passed with `-e`/`--env-file`. The image is **`deepagents-sandbox:latest`**, built from `docker/sandbox.Dockerfile` (Python 3.12 + pytest and the fundamental test utilities) and built once on first use when absent. An unreachable daemon or an image that cannot be provided raises `SandboxError` → a refusal, never a host run. `tools/sandbox.py` is deleted.

**The orchestrator runs where the daemon runs.** The perimeter reaches the daemon through the `docker` client and treats the workspace path as a path *in the daemon's filesystem*; both are only true in one namespace, one filesystem and one signal space. So `docker_bin()` raises `EnvironmentError` when the client is not on `PATH` (surfaced as a `SandboxError` refusal at the tool boundary), and **nothing** translates paths or proxies commands across an OS boundary — no WSL bridge, no `wslpath`, no `\\wsl$` rewriting. Proxying `docker run` through another OS's interpreter would break the process-tree guarantee as well: killing the proxy leaves the container orphaned and unreachable by the timeout path. A workspace on a Windows drive (9p DrvFs) is **not** usable — a container writing through one creates files with no Windows ACL (mode 0000) — so when the daemon lives on Linux the orchestrator and its workspace live there too.

**Workspace location — the native cutover.** `tools/workspace.py` resolves `PROJECT_DIR` from `DEEPAGENTS_WORKSPACE_DIR`, defaulting to `~/workspaces/my_project` (expanded, made absolute, and resolved against the repo root when relative). The workspace is a path *in the daemon's filesystem* — a container bind-mounts it — so it cannot be a directory beside the repository. `PLAN_DIR` stays the repository root: the plan is the user's tracked document and the orchestrator writes it, not a container. The native environment is `~/deepagents-venv` (Python 3.12) plus `deepagents_core` built for Linux; the Windows venv still runs the suite where no daemon is needed.

**Declared dependency.** `pyproject.toml` declares `langchain-openai`: `create_deep_agent` constructs `ChatOpenAI` in every agent factory, so a clean environment cannot build an agent without it. It was previously satisfied only transitively, which a fresh Linux install exposed.

**Skill registry (`storage/db.py`, `orchestration/retriever.py`, `orchestration/system2.py`) — Phase 7.5.** A `skills` table (`id`, `name`, `target_capabilities` JSON, `markdown_content`), seeded idempotently with `TDD_Execution_Skill` for `["exec", "fs"]`. `retriever.skills_for_capabilities` matches a node's declaration with a **single** JSON1 set-intersection query (no N+1), and `system2.compose_system_prompt` prepends the block to the system prompt before the client payload is built. The planner resolves the block once per pass (`plan_task(capabilities=…)`) and passes it as text — the transport never looks a skill up.

**Engine verification gate (`orchestration/swarm.py`, `orchestration/workflow/execution.py`) — Phase 7.5.** `verification_refusal(verdict, required_capabilities)` is the policy: a node that required `exec` must carry a `tools/test_runner.py` receipt whose exit code is 0. No receipt, a failing receipt, or an uncollectable suite is a **refusal**, and the engine **intercepts the transition**: the node is re-queued (status `pending`, `rejection_attempts` incremented, so the retry is bounded by `max_retries` like a human's) with the message `Task unverified: Tests must be executed via test_runner and return exit_code 0`. It is never marked `completed` on the absence of evidence. A valid completion writes `tasks.verified = 1` in the same statement as the status (`update_task_status(..., verified=…)`).

**Swarm lifecycle (`orchestration/swarm.py`) — teardown hardening.** `drain(timeout)` waits for dispatched work to settle; `shutdown(timeout)` drains then closes; `BridgeAPI.shutdown_swarm()` is the app-side handle and the characterization harness drains it before unlinking a temporary workspace. A `Future` callback (`_on_worker_complete`) never raises — it reports to stderr, because nothing above a callback on the pool's thread can catch it.

**Planning-pass role binding — Phase 7 tail.** `planner.plan_task(role=…)` threads the node's role into `run_tool_loop`, and `worker.execute_node` passes its descriptor's role, so the planning pass asks the live `MCPSessionContext` for **that** role's tools instead of a constant. No import-time catalog exists anywhere; `tests/test_security_boundaries.py::NoImportTimeToolCatalogTests` pins the `@tool` catalogs and the native-shell shape at zero.

**Knowledge graph — Phase 5.** `kernel://` URIs, entities/synapses with FK cascade, bidirectional depth-bounded recursive-CTE traversal, LanceDB vectors + outbox sync, hybrid retriever.

**Known accepted debt.**
(a) `agents/model_routing.py` remains the env seam the fleet is derived from; the UI settings panel still writes those variables.
(b) The sandbox image defaults to `deepagents-sandbox:latest` and must carry whatever toolchain the workspace's commands need — the repo's `docker/sandbox.Dockerfile` supplies Python 3.12, pytest and the fundamental test utilities (`DEEPAGENTS_SANDBOX_IMAGE` overrides it). A missing runner yields the `unavailable` verdict, never a host run.
(c) The orchestrator must run in the same namespace as the Docker daemon: the client must be on `PATH` (a missing client is a fatal `EnvironmentError`, surfaced as a refusal), and the workspace must live on the daemon's filesystem (`DEEPAGENTS_WORKSPACE_DIR`). A workspace on a Windows drive is not usable (9p DrvFs writes land with no ACL). Nothing bridges the two by design.
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

#### Phase 10: Chaos Engineering & IPC Brutality
*The Crucible. Prove the system survives reality under load and unexpected process termination.*

1. **Stdio Buffer Flood & Deadlock Prevention:**
   * MCP servers communicate over stdio. If a child process spews megabytes of raw logs, the OS pipe buffer fills, the child blocks on write(), and the parent hangs.
   * **Mandate:** MCPSessionContext must use non-blocking asynchronous readers (`asyncio.StreamReader`). Enforce a strict 1MB memory limit per stream using a **Head/Tail buffer** (preserve the first 500KB and last 500KB, drop the middle). Prove the parent dispatcher never deadlocks by flooding `stderr` with 5MB of garbage.
2. **Abrupt Worker Termination & Zombie Eradication:**
   * **Mandate:** Workers must run in isolated Process Groups (`os.setsid` on Unix). Inject `SIGKILL` directly into an active worker child process. The Swarm parent must catch the broken pipe, send `SIGKILL` to the entire process group (`-PID`) to eradicate zombie MCP servers, increment `system_failures`, and re-queue.
3. **Database Crash Recovery:**
   * **Mandate:** Terminate the parent process forcefully mid-SQLite transaction. On reboot, verify WAL mode cleanly rolls back or recovers state.

#### Phase 11: Telemetry & Immutable Deployment
*The Finish Line. Complete visibility and zero environment drift.*

1. **SQLite Execution Telemetry:**
   * **Mandate:** Track wall-clock execution time, token consumption, cost estimates, and routed model tiers per task node in `tasks.execution_metadata`.
2. **Immutable Distribution & Fork Bomb Prevention:**
   * **Mandate:** Freeze the Python orchestrator kernel into an immutable artifact. If compiling via PyInstaller, mandate `multiprocessing.freeze_support()` at the absolute top of `main.py` before any imports to prevent recursive fork bombs. Otherwise, default to a strict multi-stage Docker deployment.

---

## 4. The Active Immediate Execution Order

> **Phases 8, the Phase 7 tail and Phase 7.5 are sealed. The next batch is Phase 10 (Chaos Engineering & IPC Brutality).**

**Phase 8 — Absolute Sandbox Isolation — sealed.** The exec server runs each command in a container (`tools/docker_sandbox.py`): the workspace is the only OCI bind mount and the container's working directory; `--network=none`; `--user <uid>:<gid>` maps the host identity so local file permissions are not mangled; CPU/memory/pids are capped; `--cap-drop=ALL` + `no-new-privileges`; and the process tree is owned by the named, `--rm` container, force-removed on timeout. The exec server's cwd is forced to the resolved workspace root, path traversal is refused before anything runs, and `execute_restricted_command` keeps the Architect's allow-list.
* **No native terminal fallback remains.** An unreachable daemon or a missing `docker` client is a **refusal** (`SandboxError`), never a host run. `tools/sandbox.py` is deleted.

**Phase 7 tail — dynamic tool binding in the swarm worker — closed.** The worker's planning pass asks the live `MCPSessionContext` for its **node's** role (`plan_task(role=…)` → `run_tool_loop`), and the agent factories bind from the same live `tools/list`. There is no import-time catalog anywhere; the legacy `@tool` catalogs are gone from disk and pinned at zero.

**Phase 7.5 — Skill Injection Engine — sealed.** The `skills` table is seeded with `TDD_Execution_Skill`; `retriever.skills_for_capabilities` selects a node's playbooks in one query; `system2.compose_system_prompt` prepends them before the payload is built; and the engine gate refuses to mark a task that required `exec` as `completed` without a zero-exit test receipt — it re-queues the node with `rejection_attempts` incremented and writes `verified = 1` only when it accepts.

**Definition of done for the batch — met.** `python -m pytest` → **975 passed, 7 skipped, 218 subtests** on Windows and **1200 items, 0 failures, 0 errors, 5 skipped** natively in WSL; `npm run lint` clean (29 modules, 150 dependencies, 0 violations); the legacy-catalog grep at zero (pinned by `tests/test_security_boundaries.py::NoImportTimeToolCatalogTests`); no native-execution path (no `shell=True` in any production module; `tools/sandbox.py` deleted and `tools/test_runner.py` containerized); and the daemon-backed isolation tests **pass natively** in the daemon's own environment, including the `--user` ownership proof.

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

**Definition of done for the next batch:** `python -m pytest` green (927 and rising), `npm run lint` clean, the legacy catalog grep at zero, and no new native-execution path.
